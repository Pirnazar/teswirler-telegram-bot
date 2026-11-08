"""Teswirler API helpers, notification formatting and administrator setup CLI."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
import getpass
import hashlib
import html
import http.cookiejar
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
PRIVATE = ROOT / '.secrets'
API = 'https://api.teswirler.com'
SITE = 'https://teswirler.com'
LOG = logging.getLogger('teswirler')


class ServiceError(Exception):
    def __init__(self, service, status=0, retry_after=0):
        self.status = status
        self.retry_after = retry_after
        super().__init__(f'{service}: HTTP {status}' if status else f'{service}: connection/response error')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def request_json(opener, url, *, body=None, headers=None, timeout=35, service='API'):
    payload = None if body is None else json.dumps(body).encode('utf-8')
    request = urllib.request.Request(url, data=payload, headers={
        'User-Agent': 'TeswirlerTelegram/1.0',
        **({'Content-Type': 'application/json'} if payload is not None else {}),
        **(headers or {}),
    })
    try:
        with opener.open(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        delay = 0
        try:
            delay = int(json.load(error).get('parameters', {}).get('retry_after', 0))
        except (ValueError, TypeError):
            pass
        raise ServiceError(service, error.code, delay) from None
    except (OSError, ValueError):
        # Do not log request URLs or response bodies containing credentials.
        raise ServiceError(service) from None


class Teswirler:
    def __init__(self, directory=PRIVATE):
        self.directory = directory
        self.session_file = directory / 'site-session.json'
        self.cookie_file = directory / 'site-cookies.txt'
        self.session = read_json(self.session_file)
        self.cookies = http.cookiejar.MozillaCookieJar(str(self.cookie_file))
        if self.cookie_file.exists():
            self.cookies.load(ignore_discard=True, ignore_expires=True)
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def save(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        write_json(self.session_file, self.session)
        self.cookies.save(ignore_discard=True, ignore_expires=True)

    def auth(self, endpoint, body):
        response = request_json(self.opener, API + endpoint, body=body,
                                headers={'X-Auth-Transport': 'cookie', 'Origin': SITE}, service='Teswirler')
        data = response.get('data', {})
        token = data.get('token', {}).get('accessToken')
        if not token:
            raise ServiceError('Teswirler authentication response')
        self.session['access_token'] = token
        refresh = data.get('token', {}).get('refreshToken')
        if refresh:
            self.session['refresh_token'] = refresh
        self.save()

    def login(self, identifier, password):
        self.auth('/auth/login', {'identifier': identifier, 'password': password})
        user = self.get('/auth/me')
        self.session['user_id'] = str(user['id'])
        self.save()
        return user

    def get(self, path):
        for attempt in range(2):
            try:
                token = self.session.get('access_token')
                if not token:
                    raise RuntimeError('First run: python bot.py login')
                result = request_json(self.opener, API + path,
                                      headers={'Authorization': f'Bearer {token}'}, service='Teswirler')
                if 'data' not in result:
                    raise ServiceError('Teswirler response')
                return result['data']
            except ServiceError as error:
                if error.status != 401 or attempt:
                    raise
                refresh = self.session.get('refresh_token')
                self.auth('/auth/refresh', {'refreshToken': refresh} if refresh else {})

    def post(self, path, body):
        """Retry an authenticated write only after an explicit unauthenticated rejection."""
        for attempt in range(2):
            try:
                result = request_json(self.opener, API + path, body=body,
                    headers={'Authorization': 'Bearer ' + self.session['access_token']}, service='Teswirler')
                if 'data' not in result:
                    raise ServiceError('Teswirler response')
                return result['data']
            except ServiceError as error:
                if error.status != 401 or attempt:
                    raise
                refresh = self.session.get('refresh_token')
                self.auth('/auth/refresh', {'refreshToken': refresh} if refresh else {})

    def conversation(self, conversation_id):
        """Resolve only conversations the authenticated account is a member of."""
        for page in range(20):
            data = self.get(f'/conversations?skip={page * 50}&take=50')
            rows = data if isinstance(data, list) else data.get('items', [])
            for row in rows:
                if str(row.get('id')) == str(conversation_id):
                    return row
            more = len(rows) >= 50 if isinstance(data, list) else data.get('hasMore', False)
            if not more or not rows:
                break
        return None

    def notifications(self):
        items = {}
        for page in range(100):
            data = self.get(f'/notifications?skip={page * 50}&take=50')
            rows = data if isinstance(data, list) else data.get('items')
            if not isinstance(rows, list) or any(not isinstance(n, dict) or not n.get('id') for n in rows):
                raise ServiceError('Teswirler notification format')
            for row in rows:
                items[str(row['id'])] = row
            more = len(rows) >= 50 if isinstance(data, list) else data.get('hasMore', False)
            if not more:
                return list(items.values())
            if not rows:
                raise ServiceError('Teswirler pagination')
        raise RuntimeError('More than 5000 notifications: increase pagination limit before continuing')


PAYMENTS = {'DONATION_RECEIVED', 'DONATION_CONFIRMED', 'DONATION_REJECTED',
            'WITHDRAWAL_PAID', 'WITHDRAWAL_REJECTED'}
LABELS = {
    'LIKE': 'Новая отметка «Нравится»', 'ANSWER': 'Новый комментарий или ответ',
    'FOLLOW': 'Новый подписчик', 'MENTION': 'Вас упомянули',
    'NEW_TESWIR': 'Новая запись', 'NEW_BLOG': 'Новая статья',
    'MESSAGE': 'Личное сообщение', 'NEW_MESSAGE': 'Личное сообщение',
    'DONATION_RECEIVED': 'Получена поддержка', 'DONATION_CONFIRMED': 'Поддержка подтверждена',
    'DONATION_REJECTED': 'Поддержка отклонена', 'WITHDRAWAL_PAID': 'Выплата отправлена',
    'WITHDRAWAL_REJECTED': 'Выплата отклонена',
}
LABELS_TK = {
    'LIKE': 'Täze halanma', 'ANSWER': 'Täze teswir ýa-da jogap',
    'FOLLOW': 'Täze yzarlaýjy', 'MENTION': 'Siz bellendiňiz',
    'NEW_TESWIR': 'Täze pikir', 'NEW_BLOG': 'Täze makala',
    'MESSAGE': 'Şahsy hat', 'NEW_MESSAGE': 'Şahsy hat',
    'DONATION_RECEIVED': 'Goldaw alyndy', 'DONATION_CONFIRMED': 'Goldaw tassyklandy',
    'DONATION_REJECTED': 'Goldaw ret edildi', 'WITHDRAWAL_PAID': 'Töleg iberildi',
    'WITHDRAWAL_REJECTED': 'Pul çykarmak ret edildi',
}


def actors(notification):
    return notification.get('actors') or ([notification['actor']] if notification.get('actor') else [])


def fingerprint(notification):
    # Read flags and profile/avatar changes are not new activity. Group growth is.
    fields = {k: notification.get(k) for k in (
        'type', 'createdAt', 'message', 'preview', 'actorCount', 'amount',
        'commentId', 'teswirCommentId', 'conversationId', 'teswirId', 'blogId')}
    fields['actors'] = sorted(str(a.get('id', a.get('username', ''))) for a in actors(notification))
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def notification_url(n):
    def part(value):
        return urllib.parse.quote(str(value), safe='')
    kind = n.get('type')
    if kind in ('MESSAGE', 'NEW_MESSAGE'):
        path = '/sahsy-hat' + ('/' + part(n['conversationId']) if n.get('conversationId') else '')
    elif kind in PAYMENTS:
        path = '/tolegler'
    elif kind == 'FOLLOW':
        people = actors(n)
        path = '/@' + part(people[0]['username']) if len(people) == 1 and people[0].get('username') else '/bildirisler'
    elif (n.get('blog') or {}).get('slug'):
        path = '/' + part(n['blog']['slug'])
        if n.get('commentId'):
            path += '#comment-' + part(n['commentId'])
    elif n.get('teswirId'):
        path = '/pikirler/' + part(n['teswirId'])
        if n.get('teswirCommentId'):
            path += '#comment-' + part(n['teswirCommentId'])
    else:
        path = '/bildirisler'
    return SITE + path


def format_notification(n, language='ru'):
    tk = language == 'tk'
    labels = LABELS_TK if tk else LABELS
    lines = ['Teswirler · ' + labels.get(n.get('type'), 'Täze bildiriş' if tk else 'Новое уведомление')]
    people = actors(n)
    if people:
        name = str(people[0].get('name') or people[0].get('username') or '')[:150]
        count = n.get('actorCount') or len(people)
        lines.append(name + ((f' we ýene {count - 1} adam' if tk else f' и ещё {count - 1}') if count > 1 else ''))
    for value in (n.get('message'), n.get('preview') or (n.get('blog') or {}).get('title')):
        if value:
            lines.append(' '.join(str(value).split())[:1000])
    if n.get('amount') is not None:
        lines.append(f"{str(n['amount'])[:50]} TMT")
    if n.get('createdAt'):
        lines.append(str(n['createdAt'])[:60])
    lines.append(notification_url(n))
    return '\n'.join(lines)[:4000]


MONTHS = {
    'ru': ('января', 'февраля', 'марта', 'апреля', 'мая', 'июня',
           'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'),
    'tk': ('ýanwar', 'fewral', 'mart', 'aprel', 'maý', 'iýun',
           'iýul', 'awgust', 'sentýabr', 'oktýabr', 'noýabr', 'dekabr'),
}


def timezone_label(minutes):
    if not minutes:
        return 'UTC'
    hours, remainder = divmod(abs(minutes), 60)
    return f"UTC{'+' if minutes > 0 else '-'}{hours}" + (f':{remainder:02d}' if remainder else '')


def notification_date(value, language='ru', timezone_minutes=0, now=None):
    if not value:
        return ''
    try:
        date = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        zone = timezone(timedelta(minutes=timezone_minutes))
        local = date.astimezone(zone)
        current = (now or datetime.now(timezone.utc)).astimezone(zone)
        year = f' {local.year}' if local.year != current.year else ''
        month = MONTHS.get(language, MONTHS['ru'])[local.month - 1]
        return f'{local.day} {month}{year} · {local:%H:%M} · {timezone_label(timezone_minutes)}'
    except (ValueError, TypeError, OverflowError):
        return ''


def escaped_excerpt(value, budget):
    """Bound serialized HTML/UTF-16 length without splitting an entity or character."""
    text = str(value).replace('\r\n', '\n').replace('\r', '\n').strip()
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', text)
    parts, used = [], 0
    for character in text:
        escaped = html.escape(character, quote=False)
        size = len(escaped.encode('utf-16-le')) // 2
        if used + size > budget - 1:
            return ''.join(parts).rstrip() + '…'
        parts.append(escaped)
        used += size
    return ''.join(parts)


def notification_message(n, language='ru', timezone_minutes=0):
    """Telegram HTML card with a single contextual navigation button."""
    tk = language == 'tk'
    kind = n.get('type')
    labels = LABELS_TK if tk else LABELS
    icon = ('💬' if kind in ('MESSAGE', 'NEW_MESSAGE', 'ANSWER') else
            '♡' if kind == 'LIKE' else '👤' if kind == 'FOLLOW' else
            '↗' if kind == 'MENTION' else '💳' if kind in PAYMENTS else '📝')
    label = labels.get(kind, 'Täze bildiriş' if tk else 'Новое уведомление')
    lines = [f'{icon} <b>{html.escape(label)}</b>']
    people = actors(n)
    if people:
        name = people[0].get('name') or people[0].get('username') or ''
        count = max(1, int(n.get('actorCount') or len(people)))
        others = (f' we ýene {count - 1} adam' if tk else f' и ещё {count - 1}') if count > 1 else ''
        lines.append(f'<b>{escaped_excerpt(name, 300)}</b>{html.escape(others)}')
    content = []
    for value in (n.get('message'), n.get('preview') or (n.get('blog') or {}).get('title')):
        if value and str(value).strip() not in content:
            content.append(str(value).strip())
    for value in content:
        lines.extend(('', f'<blockquote>{escaped_excerpt(value, 1200)}</blockquote>'))
    if n.get('amount') is not None:
        lines.extend(('', f'<b>{escaped_excerpt(n["amount"], 60)} TMT</b>'))
    date = notification_date(n.get('createdAt'), language, timezone_minutes)
    if date:
        lines.extend(('', f'<i>{html.escape(date)}</i>'))
    if kind in ('MESSAGE', 'NEW_MESSAGE'):
        button = 'Haty açmak' if tk else 'Открыть переписку'
    elif kind == 'ANSWER':
        button = 'Jogaby görmek' if tk else 'Посмотреть ответ'
    elif kind == 'FOLLOW':
        button = 'Profili açmak' if tk else 'Открыть профиль'
    elif kind in PAYMENTS:
        button = 'Tölegleri açmak' if tk else 'Открыть платежи'
    elif kind in ('NEW_BLOG', 'NEW_TESWIR'):
        button = 'Saýtda okamak' if tk else 'Читать на сайте'
    else:
        button = 'Saýtda görmek' if tk else 'Посмотреть на сайте'
    return {'text': '\n'.join(lines), 'parse_mode': 'HTML',
            'reply_markup': {'inline_keyboard': [[{'text': button + ' ↗', 'url': notification_url(n)}]]},
            'link_preview_options': {'is_disabled': True}}


def queued_message(text, payload=None):
    """Old queued text must remain plain text; only new rows opt into HTML."""
    if payload:
        try:
            message = json.loads(payload)
            if (isinstance(message, dict) and isinstance(message.get('text'), str)
                    and message.get('parse_mode') == 'HTML'):
                allowed = {'text', 'parse_mode', 'reply_markup', 'link_preview_options'}
                return {key: value for key, value in message.items() if key in allowed}
        except (ValueError, TypeError):
            pass
    return {'text': text, 'link_preview_options': {'is_disabled': True}}


class Store:
    def __init__(self, path, user_id):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS seen (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (seq INTEGER PRIMARY KEY, text TEXT NOT NULL);
        ''')
        if 'payload' not in {row[1] for row in self.db.execute('PRAGMA table_info(outbox)')}:
            self.db.execute('ALTER TABLE outbox ADD COLUMN payload TEXT')
            self.db.commit()
        owner = self.db.execute("SELECT value FROM meta WHERE key='user_id'").fetchone()
        if owner and owner[0] != str(user_id):
            self.db.close()
            raise RuntimeError('Account changed: use a separate data directory')
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES ('user_id', ?)", (str(user_id),))

    def observe(self, notifications, language='ru', timezone_minutes=0):
        initialized = bool(self.db.execute("SELECT 1 FROM meta WHERE key='initialized'").fetchone())
        queued = 0
        with self.db:
            for n in sorted(notifications, key=lambda n: (n.get('createdAt') or '', str(n['id']))):
                key, revision = str(n['id']), fingerprint(n)
                old = self.db.execute('SELECT fingerprint FROM seen WHERE id=?', (key,)).fetchone()
                if old and old[0] == revision:
                    continue
                self.db.execute('INSERT OR REPLACE INTO seen VALUES (?, ?)', (key, revision))
                if initialized:
                    self.db.execute('INSERT INTO outbox(text,payload) VALUES (?,?)', (
                        format_notification(n, language),
                        json.dumps(notification_message(n, language, timezone_minutes), ensure_ascii=False)))
                    queued += 1
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('initialized', '1')")
        return initialized, queued

    async def flush(self, telegram, chat_id):
        count = 0
        for seq, text, payload in self.db.execute('SELECT seq,text,payload FROM outbox ORDER BY seq').fetchall():
            await telegram.call('sendMessage', {'chat_id': chat_id, **queued_message(text, payload)})
            with self.db:
                self.db.execute('DELETE FROM outbox WHERE seq=?', (seq,))
            count += 1
        return count


def telegram_setup():
    import asyncio
    from telegram_runtime import pair_bot
    with single_instance():
        asyncio.run(pair_bot(ROOT))


@contextlib.contextmanager
def single_instance():
    PRIVATE.mkdir(parents=True, exist_ok=True)
    handle = (PRIVATE / 'runner.lock').open('a+b')
    handle.seek(0)
    if not handle.read(1):
        handle.write(b'0')
        handle.flush()
    handle.seek(0)
    try:
        if __import__('os').name == 'nt':
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError('The bot is already running') from None
    try:
        yield
    finally:
        handle.close()


async def run(once=False, interval=30):
    with single_instance():
        await _run_personal(once, interval)


async def _run_personal(once=False, interval=30):
    """Compatibility CLI for the original personal account, using aiogram transport."""
    from telegram_runtime import AiogramGateway
    site = Teswirler()
    user = await asyncio.to_thread(site.get, '/auth/me')
    config = read_json(PRIVATE / 'telegram.json')
    if not config.get('chat_id'):
        raise RuntimeError('First run: python bot.py pair')
    telegram = AiogramGateway(config['token'])
    store = None
    try:
        if (await telegram.call('getMe'))['id'] != config['bot_id']:
            raise RuntimeError('Bot identity changed. Pair again.')
        store = Store(ROOT / 'data' / 'notifications.sqlite3', user['id'])
        failures = 0
        while True:
            try:
                await store.flush(telegram, config['chat_id'])
                initialized, queued = store.observe(await asyncio.to_thread(site.notifications))
                sent = await store.flush(telegram, config['chat_id'])
                LOG.info('Check complete: queued=%s sent=%s baseline=%s', queued, sent, not initialized)
                failures = 0
                if once:
                    return
                await asyncio.sleep(interval)
            except (ServiceError, RuntimeError) as error:
                LOG.error('%s', type(error).__name__)
                if once:
                    raise
                failures += 1
                await asyncio.sleep(max(min(300, interval * 2 ** min(failures, 4)),
                                        min(3600, getattr(error, 'retry_after', 0))))
    finally:
        if store:
            store.db.close()
        await telegram.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['login', 'prime', 'pair', 'run', 'once', 'check'])
    parser.add_argument('--interval', type=int, default=30)
    args = parser.parse_args()
    if args.interval < 10:
        parser.error('--interval must be at least 10 seconds')
    (ROOT / 'logs').mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=[
        logging.StreamHandler(), RotatingFileHandler(ROOT / 'logs' / 'bot.log', maxBytes=1_000_000,
                                                    backupCount=3, encoding='utf-8')])
    try:
        if args.command == 'login':
            Teswirler().login(input('Teswirler email or username: ').strip(), getpass.getpass('Password: '))
            print('Site session saved. Password was not saved.')
        elif args.command == 'pair':
            telegram_setup()
        elif args.command in ('prime', 'check'):
            site = Teswirler()
            user = site.get('/auth/me')
            notifications = site.notifications()
            if args.command == 'prime':
                with single_instance():
                    Store(ROOT / 'data' / 'notifications.sqlite3', user['id']).observe(notifications)
            print(f'Site connection OK. Notifications available: {len(notifications)}.')
        else:
            asyncio.run(run(args.command == 'once', args.interval))
    except (ServiceError, RuntimeError, ValueError, OSError, KeyError) as error:
        LOG.error('%s', error if isinstance(error, (ServiceError, RuntimeError)) else type(error).__name__)
        return 1
    except KeyboardInterrupt:
        LOG.info('Stopped')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
