"""Per-account history, notification preferences, aggregation and durable digests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import html
import json
import secrets
import time

from bot import (Store, SITE, actors, escaped_excerpt, fingerprint, format_notification,
                 notification_message, notification_url, queued_message, timezone_label, MONTHS)
from i18n import tr


CATEGORIES = ('messages', 'answers', 'mentions', 'follows', 'likes', 'posts', 'payments', 'other')
TYPES = {'MESSAGE': 'messages', 'NEW_MESSAGE': 'messages', 'ANSWER': 'answers', 'MENTION': 'mentions',
         'FOLLOW': 'follows', 'LIKE': 'likes', 'NEW_BLOG': 'posts', 'NEW_TESWIR': 'posts',
         'DONATION_RECEIVED': 'payments', 'DONATION_CONFIRMED': 'payments', 'DONATION_REJECTED': 'payments',
         'WITHDRAWAL_PAID': 'payments', 'WITHDRAWAL_REJECTED': 'payments'}


def category(notification):
    return TYPES.get(notification.get('type'), 'other')


def muted_categories(user):
    try:
        value = json.loads(user.get('muted', '[]'))
        return set(value) & set(CATEGORIES) if isinstance(value, list) else set()
    except (ValueError, TypeError):
        return set()


def local_time(user, now):
    return datetime.fromtimestamp(now, timezone(timedelta(minutes=user.get('timezone_minutes', 0))))


def quiet_until(user, now):
    start, end = user.get('quiet_start', -1), user.get('quiet_end', -1)
    if start < 0 or end < 0 or start == end:
        return now
    local = local_time(user, now)
    minute = local.hour * 60 + local.minute
    quiet = start <= minute < end if start < end else minute >= start or minute < end
    if not quiet:
        return now
    target = local.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return target.timestamp()


def source_timestamp(notification, fallback):
    try:
        stamp = datetime.fromisoformat(str(notification.get('createdAt', '')).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return min(stamp.timestamp(), fallback)
    except (ValueError, TypeError, OverflowError):
        return fallback


def today_range(user, now):
    local = local_time(user, now)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp(), now


def digest_due(user, now):
    minute = user.get('digest_minute', 1260)
    local = local_time(user, now)
    target = local.replace(hour=minute // 60, minute=minute % 60, second=0, microsecond=0)
    if target.timestamp() > now:
        target -= timedelta(days=1)
    return target.timestamp()


class FeatureStore(Store):
    def __init__(self, path, user_id):
        super().__init__(path, user_id)
        self.db.execute('PRAGMA busy_timeout=5000')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(outbox)')}
        for name, declaration in {
            'kind': "TEXT NOT NULL DEFAULT 'auto'", 'category': 'TEXT',
            'due': 'REAL NOT NULL DEFAULT 0', 'group_key': 'TEXT', 'source': 'TEXT',
        }.items():
            if name not in columns:
                self.db.execute(f'ALTER TABLE outbox ADD COLUMN {name} {declaration}')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS history (
                id TEXT PRIMARY KEY, revision TEXT NOT NULL, source TEXT NOT NULL, updated REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS activity (
                id INTEGER PRIMARY KEY, notification_id TEXT NOT NULL, revision TEXT NOT NULL,
                at REAL NOT NULL, category TEXT NOT NULL, source TEXT NOT NULL,
                UNIQUE(notification_id,revision)
            );
            CREATE INDEX IF NOT EXISTS activity_date ON activity(at);
            CREATE TABLE IF NOT EXISTS reply_targets (
                token TEXT PRIMARY KEY, conversation_id TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL, expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS reply_sends (
                nonce TEXT PRIMARY KEY, status TEXT NOT NULL, created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notification_revisions (
                notification_id TEXT NOT NULL, revision TEXT NOT NULL,
                PRIMARY KEY(notification_id,revision)
            );
            CREATE TABLE IF NOT EXISTS like_state (
                notification_id TEXT PRIMARY KEY, max_count INTEGER NOT NULL,
                last_count INTEGER NOT NULL, actors TEXT NOT NULL
            );
        ''')
        if 'is_event' not in {row[1] for row in self.db.execute('PRAGMA table_info(activity)')}:
            self.db.execute('ALTER TABLE activity ADD COLUMN is_event INTEGER NOT NULL DEFAULT 1')
        self.db.commit()
        self._migrate_event_detection()

    def _track_like(self, notification):
        """A shrinking/returning aggregate is not evidence of a new like."""
        key = str(notification['id'])
        people = {str(person['id']) for person in actors(notification) if person.get('id') is not None}
        raw_count = notification.get('actorCount')
        count = max(0, int(raw_count)) if raw_count is not None else max(1, len(people))
        old = self.db.execute('SELECT max_count,last_count,actors FROM like_state WHERE notification_id=?', (key,)).fetchone()
        if old:
            maximum, last_count, encoded = old
            known = set(json.loads(encoded))
            new_people = bool(known and people - known)
            positive = count > maximum or (count >= last_count and new_people)
            maximum, people = max(maximum, count), known | people
        else:
            maximum, positive = count, count > 0
        self.db.execute('INSERT OR REPLACE INTO like_state VALUES (?,?,?,?)',
                        (key, maximum, count, json.dumps(sorted(people))))
        return positive

    def _migrate_event_detection(self):
        if self.db.execute("SELECT 1 FROM meta WHERE key='event_detection_v1'").fetchone():
            return
        with self.db:
            # Preserve all known versions, including already-sent states that later reappear.
            self.db.execute('INSERT OR IGNORE INTO notification_revisions SELECT notification_id,revision FROM activity')
            self.db.execute('INSERT OR IGNORE INTO notification_revisions SELECT id,revision FROM history')
            self.db.execute('INSERT OR IGNORE INTO notification_revisions SELECT id,fingerprint FROM seen')
            for row_id, source in self.db.execute("SELECT id,source FROM activity WHERE category='likes' ORDER BY at,id").fetchall():
                positive = self._track_like(json.loads(source))
                self.db.execute('UPDATE activity SET is_event=? WHERE id=?', (int(positive), row_id))
            for (source,) in self.db.execute('SELECT source FROM history').fetchall():
                notification = json.loads(source)
                if category(notification) == 'likes':
                    self._track_like(notification)
            # Old false alarms may still be pending; explicit /recent requests are retained.
            for seq, source in self.db.execute("SELECT seq,source FROM outbox WHERE kind='auto' AND category='likes' AND source IS NOT NULL").fetchall():
                members = json.loads(source).values()
                flags = [self.db.execute('SELECT is_event FROM activity WHERE notification_id=? AND revision=?',
                         (str(n['id']), fingerprint(n))).fetchone() for n in members]
                if flags and all(flag is not None and not flag[0] for flag in flags):
                    self.db.execute('DELETE FROM outbox WHERE seq=?', (seq,))
            self.db.execute("INSERT INTO meta VALUES ('event_detection_v1','1')")

    def archive(self, notifications, now):
        events = {}
        for n in notifications:
            key, revision = str(n['id']), fingerprint(n)
            old = self.db.execute('SELECT revision FROM history WHERE id=?', (key,)).fetchone()
            source = json.dumps(n, ensure_ascii=False)
            when = source_timestamp(n, now) if not old else now
            events[key] = False
            if not old or old[0] != revision:
                known = bool(self.db.execute('SELECT 1 FROM notification_revisions WHERE notification_id=? AND revision=?',
                                            (key, revision)).fetchone())
                positive = self._track_like(n) if category(n) == 'likes' else True
                events[key] = positive and not known
                countable = positive and (not known or old is None)
                self.db.execute('INSERT OR IGNORE INTO activity(notification_id,revision,at,category,source,is_event) VALUES (?,?,?,?,?,?)',
                                (key, revision, when, category(n), source, int(countable)))
                self.db.execute('INSERT OR REPLACE INTO history VALUES (?,?,?,?)', (key, revision, source, now))
                self.db.execute('INSERT OR IGNORE INTO notification_revisions VALUES (?,?)', (key, revision))
        self.db.execute('DELETE FROM activity WHERE at<?', (now - 90 * 86400,))
        self.db.execute('DELETE FROM reply_targets WHERE expires<?', (now,))
        self.db.execute('DELETE FROM reply_sends WHERE created<?', (now - 30 * 86400,))
        return events

    def card(self, notification, user, now):
        message = notification_message(notification, user.get('language', 'ru'), user.get('timezone_minutes', 0))
        if category(notification) == 'messages' and notification.get('conversationId'):
            conversation = str(notification['conversationId'])
            people = actors(notification)
            name = str(people[0].get('name') or people[0].get('username') or '')[:150] if people else ''
            old = self.db.execute('SELECT token FROM reply_targets WHERE conversation_id=?', (conversation,)).fetchone()
            token = old[0] if old else secrets.token_hex(12)
            self.db.execute('INSERT OR REPLACE INTO reply_targets VALUES (?,?,?,?)',
                            (token, conversation, name, now + 7 * 86400))
            message['reply_markup']['inline_keyboard'].append([
                {'text': tr(user.get('language', 'ru'), 'reply_button'), 'callback_data': 'reply:' + token}])
        return message

    def enqueue_card(self, notification, user, now, *, kind='auto', due=None, group_key=None, sources=None):
        message = self.card(notification, user, now)
        return self.db.execute('INSERT INTO outbox(text,payload,kind,category,due,group_key,source) VALUES (?,?,?,?,?,?,?)',
            (format_notification(notification, user.get('language', 'ru')), json.dumps(message, ensure_ascii=False),
             kind, category(notification), now if due is None else due, group_key,
             json.dumps(sources or {str(notification['id']): notification}, ensure_ascii=False))).lastrowid

    def observe(self, notifications, language='ru', timezone_minutes=0, *, user=None, now=None):
        now = time.time() if now is None else now
        user = user or {'language': language, 'timezone_minutes': timezone_minutes}
        initialized = bool(self.db.execute("SELECT 1 FROM meta WHERE key='initialized'").fetchone())
        queued = 0
        with self.db:
            events = self.archive(notifications, now)
            for n in sorted(notifications, key=lambda row: (row.get('createdAt') or '', str(row['id']))):
                key, revision = str(n['id']), fingerprint(n)
                old = self.db.execute('SELECT fingerprint FROM seen WHERE id=?', (key,)).fetchone()
                if old and old[0] == revision:
                    continue
                self.db.execute('INSERT OR REPLACE INTO seen VALUES (?,?)', (key, revision))
                if not initialized or not events[key] or category(n) in muted_categories(user):
                    continue
                group_key = self.like_key(n) if user.get('grouping', 1) else None
                previous = self.db.execute("SELECT seq,source FROM outbox WHERE kind='auto' AND group_key=? ORDER BY seq LIMIT 1",
                                           (group_key,)).fetchone() if group_key else None
                if previous:
                    sources = json.loads(previous[1])
                    sources[key] = n
                    merged = self.merge_likes(list(sources.values()))
                    message = self.card(merged, user, now)
                    self.db.execute('UPDATE outbox SET text=?,payload=?,source=? WHERE seq=?',
                        (format_notification(merged, user.get('language', 'ru')),
                         json.dumps(message, ensure_ascii=False), json.dumps(sources, ensure_ascii=False), previous[0]))
                else:
                    self.enqueue_card(n, user, now, due=now + (20 if group_key else 0), group_key=group_key)
                    queued += 1
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('initialized','1')")
        return initialized, queued

    @staticmethod
    def like_key(n):
        if category(n) != 'likes':
            return None
        target = n.get('blogId') or (n.get('blog') or {}).get('slug') or n.get('teswirId')
        if not target:
            return None
        return json.dumps([target, n.get('commentId'), n.get('teswirCommentId')], ensure_ascii=False)

    @staticmethod
    def merge_likes(rows):
        latest = max(rows, key=lambda n: n.get('createdAt') or '')
        merged = dict(latest)
        people = {}
        for row in rows:
            for person in actors(row):
                people[str(person.get('id') or person.get('username') or person.get('name'))] = person
        merged['actors'] = list(people.values())
        # Partial actor lists cannot be summed without counting the same people twice.
        merged['actorCount'] = max([len(people), 1] + [int(row.get('actorCount') or 1) for row in rows])
        return merged

    def queue_recent(self, rows, user, now):
        with self.db:
            self.archive(rows, now)
            for row in reversed(rows):
                self.enqueue_card(row, user, now, kind='manual')

    def reply_target(self, token, now):
        row = self.db.execute('SELECT conversation_id,name FROM reply_targets WHERE token=? AND expires>?', (token, now)).fetchone()
        return {'conversation_id': row[0], 'name': row[1]} if row else None

    def summary(self, user, start, end, *, respect_filters=False):
        language = user.get('language', 'ru')
        local = local_time(user, end)
        date = f'{local.day} {MONTHS[language][local.month - 1]}'
        rows = self.db.execute('SELECT category,COUNT(*) FROM activity WHERE is_event=1 AND at>=? AND at<? GROUP BY category', (start, end)).fetchall()
        muted = muted_categories(user) if respect_filters else set()
        counts = {key: count for key, count in rows if key not in muted}
        lines = [f'📊 <b>{html.escape(tr(language,"summary_title", date=date))}</b>', '']
        total = sum(counts.values())
        if total:
            for key in CATEGORIES:
                if counts.get(key):
                    lines.append(f'{html.escape(tr(language,"cat_" + key))}: <b>{counts[key]}</b>')
            lines.extend(('', '<b>' + html.escape(tr(language, 'summary_total', count=total)) + '</b>'))
        else:
            lines.append(html.escape(tr(language, 'summary_empty')))
        links = self.db.execute('SELECT source FROM activity WHERE is_event=1 AND at>=? AND at<? ORDER BY at DESC LIMIT 30', (start, end)).fetchall()
        if links:
            lines.extend(('', html.escape(tr(language, 'summary_links'))))
            used = set()
            for (source,) in links:
                row = json.loads(source)
                if category(row) in muted or len(used) >= 3:
                    continue
                url = notification_url(row)
                if url in used:
                    continue
                used.add(url)
                people = actors(row)
                label = people[0].get('name') or people[0].get('username') if people else None
                label = label or (row.get('blog') or {}).get('title') or tr(language, 'cat_' + category(row))
                lines.append(f'• <a href="{html.escape(url, quote=True)}">{escaped_excerpt(label, 150)}</a>')
        first, last = local_time(user, start), local_time(user, end)
        lines.extend(('', f'<i>{first:%d.%m %H:%M} — {last:%d.%m %H:%M} · {timezone_label(user.get("timezone_minutes",0))}</i>',
                      '<i>' + html.escape(tr(language, 'summary_basis')) + '</i>'))
        return {'text': '\n'.join(lines), 'parse_mode': 'HTML', 'link_preview_options': {'is_disabled': True},
                'reply_markup': {'inline_keyboard': [[{'text': tr(language, 'recent_10'), 'callback_data': 'recent_10'},
                    {'text': tr(language, 'open_notifications'), 'url': SITE + '/bildirisler'}]]}}

    def queue_summary(self, user, start, end, *, kind='manual'):
        message = self.summary(user, start, end, respect_filters=kind == 'digest')
        return self.db.execute('INSERT INTO outbox(text,payload,kind,category,due,source) VALUES (?,?,?,?,?,?)',
                              (tr(user['language'], 'summary_plain'), json.dumps(message, ensure_ascii=False),
                               kind, 'digest', end, json.dumps({'summary_start': start, 'summary_end': end}))).lastrowid

    def schedule_digest(self, user, now):
        if not user.get('digest_enabled', 1):
            return False
        due = digest_due(user, now)
        old = self.db.execute("SELECT value FROM meta WHERE key='digest_last_due'").fetchone()
        if due <= user.get('digest_since', now) or old and float(old[0]) >= due:
            return False
        with self.db:
            self.queue_summary(user, due - 86400, due, kind='digest')
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('digest_last_due',?)", (str(due),))
        return True

    def delivery(self, user, now):
        """Manual requests bypass quiet hours, pause and automatic type filters."""
        with self.db:
            if not user.get('digest_enabled', 1):
                self.db.execute("DELETE FROM outbox WHERE kind='digest'")
            else:
                self.db.execute("DELETE FROM outbox WHERE kind='digest' AND due<=?", (user.get('digest_since', 0),))
            muted = muted_categories(user)
            for key in muted:
                self.db.execute("DELETE FROM outbox WHERE kind='auto' AND category=?", (key,))
        manual = self.db.execute("SELECT seq,text,payload FROM outbox WHERE kind='manual' AND due<=? ORDER BY seq LIMIT 1", (now,)).fetchone()
        if manual:
            return manual
        if not user.get('enabled', 1) or quiet_until(user, now) > now:
            return None
        row = self.db.execute("SELECT seq,text,payload,kind,source FROM outbox WHERE kind!='manual' AND due<=? ORDER BY seq LIMIT 1", (now,)).fetchone()
        if row and row[3] == 'digest' and row[4]:
            period = json.loads(row[4])
            payload = json.dumps(self.summary(user, period['summary_start'], period['summary_end'], respect_filters=True), ensure_ascii=False)
            with self.db:
                self.db.execute('UPDATE outbox SET payload=? WHERE seq=?', (payload, row[0]))
            return row[0], row[1], payload
        return row[:3] if row else None

    def pending(self, user, now):
        manual = bool(self.db.execute("SELECT 1 FROM outbox WHERE kind='manual' LIMIT 1").fetchone())
        if manual:
            return True, True, now
        row = self.db.execute("SELECT MIN(due) FROM outbox WHERE kind!='manual'").fetchone()
        due = row[0] if row else None
        if due is not None and user.get('enabled', 1):
            return True, False, max(now + 1.2, due, quiet_until(user, now))
        return False, False, now + 30
