"""Multi-user Russian/Turkmen bot. Passwords exist only in memory during login."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass
import http.cookiejar
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import threading
import time
import urllib.request
import uuid

from cryptography.fernet import Fernet, InvalidToken

from bot import (ROOT, PRIVATE, LOG, ServiceError, Store, Telegram, Teswirler,
                 read_json, single_instance, queued_message, timezone_label)
from i18n import tr
from features import FeatureStore
from features_ui import FeatureUI

INTERVAL = 30
INPUT_TTL = 300
WORKERS = 8


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != 'nt':
        path.chmod(0o700)
    return path


class Vault:
    def __init__(self, key_path):
        private_directory(key_path.parent)
        if not key_path.exists():
            if any(key_path.parent.glob('public-users/*/*/session.enc')):
                raise RuntimeError('Encryption key missing; restore it before starting')
            with os.fdopen(os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
                stream.write(Fernet.generate_key())
        self.cipher = Fernet(key_path.read_bytes())

    def save(self, path, value):
        private_directory(path.parent)
        temporary = path.with_suffix('.tmp')
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'wb') as stream:
            stream.write(self.cipher.encrypt(json.dumps(value, ensure_ascii=False).encode('utf-8')))
        temporary.replace(path)

    def load(self, path):
        if not path.exists():
            return {}
        return json.loads(self.cipher.decrypt(path.read_bytes()))


COOKIE_FIELDS = ('version', 'name', 'value', 'port', 'port_specified', 'domain', 'domain_specified',
                 'domain_initial_dot', 'path', 'path_specified', 'secure', 'expires', 'discard',
                 'comment', 'comment_url', 'rfc2109')


class EncryptedSite(Teswirler):
    def __init__(self, directory, vault):
        self.directory, self.vault = directory, vault
        self.session_file = directory / 'session.enc'
        saved = vault.load(self.session_file)
        self.session = saved.get('session', {})
        self.cookies = http.cookiejar.CookieJar()
        for cookie in saved.get('cookies', []):
            self.cookies.set_cookie(http.cookiejar.Cookie(**cookie))
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def save(self):
        cookies = [{**{key: getattr(cookie, key) for key in COOKIE_FIELDS}, 'rest': cookie._rest}
                   for cookie in self.cookies]
        self.vault.save(self.session_file, {'session': self.session, 'cookies': cookies})


class Catalog:
    """Only the main thread uses this connection; workers own their notification databases."""
    def __init__(self, path):
        private_directory(path.parent)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                chat_id INTEGER PRIMARY KEY, language TEXT NOT NULL DEFAULT 'ru',
                site_user TEXT UNIQUE, name TEXT, storage TEXT,
                enabled INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'none',
                timezone_minutes INTEGER NOT NULL DEFAULT 300,
                next_run REAL NOT NULL DEFAULT 0, next_poll REAL NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS attempts (chat_id INTEGER NOT NULL, at REAL NOT NULL);
        ''')
        self.added_timezone = 'timezone_minutes' not in {row[1] for row in self.db.execute('PRAGMA table_info(users)')}
        if self.added_timezone:
            self.db.execute('ALTER TABLE users ADD COLUMN timezone_minutes INTEGER NOT NULL DEFAULT 300')
            self.db.commit()
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(users)')}
        for name, declaration in {
            'muted': "TEXT NOT NULL DEFAULT '[]'", 'quiet_start': 'INTEGER NOT NULL DEFAULT -1',
            'quiet_end': 'INTEGER NOT NULL DEFAULT -1', 'grouping': 'INTEGER NOT NULL DEFAULT 1',
            'digest_enabled': 'INTEGER NOT NULL DEFAULT 1', 'digest_minute': 'INTEGER NOT NULL DEFAULT 1260',
            'digest_since': 'REAL NOT NULL DEFAULT 0', 'pending_manual': 'INTEGER NOT NULL DEFAULT 0',
        }.items():
            if name not in columns:
                self.db.execute(f'ALTER TABLE users ADD COLUMN {name} {declaration}')
        self.db.execute('UPDATE users SET digest_since=? WHERE digest_since=0', (time.time(),))
        self.db.commit()

    def user(self, chat_id, language='ru'):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO users(chat_id,language,digest_since) VALUES (?,?,?)', (chat_id, language, time.time()))
        return dict(self.db.execute('SELECT * FROM users WHERE chat_id=?', (chat_id,)).fetchone())

    def update(self, chat_id, **values):
        allowed = {'language', 'site_user', 'name', 'storage', 'enabled', 'status',
                   'next_run', 'next_poll', 'failures', 'timezone_minutes', 'muted', 'quiet_start', 'quiet_end',
                   'grouping', 'digest_enabled', 'digest_minute', 'digest_since', 'pending_manual'}
        if not values.keys() <= allowed:
            raise ValueError('Unknown catalog fields')
        with self.db:
            self.db.execute('UPDATE users SET ' + ','.join(f'{key}=?' for key in values) + ' WHERE chat_id=?',
                            (*values.values(), chat_id))

    def meta(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, str(value)))
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else None

    def attempt(self, chat_id):
        now = time.time()
        with self.db:
            self.db.execute('DELETE FROM attempts WHERE at<?', (now - 900,))
            own = self.db.execute('SELECT COUNT(*) FROM attempts WHERE chat_id=?', (chat_id,)).fetchone()[0]
            global_count = self.db.execute('SELECT COUNT(*) FROM attempts WHERE at>?', (now - 60,)).fetchone()[0]
            if own >= 5 or global_count >= 20:
                return False
            self.db.execute('INSERT INTO attempts VALUES (?,?)', (chat_id, now))
        return True

    def due(self, now):
        return [dict(row) for row in self.db.execute(
            "SELECT * FROM users WHERE (enabled=1 OR pending_manual=1) AND status='active' AND next_run<=? ORDER BY next_run LIMIT 100", (now,))]


class PacedTelegram(Telegram):
    def __init__(self, token):
        super().__init__(token)
        self.send_lock = threading.Lock()
        self.last_send = 0
        self.chat_sent = {}
        self.blocked_until = 0

    def call(self, method, body=None):
        if method != 'sendMessage':
            return super().call(method, body)
        with self.send_lock:
            now = time.monotonic()
            chat = body['chat_id']
            delay = max(self.last_send + .05, self.chat_sent.get(chat, 0) + 1.1, self.blocked_until) - now
            if delay > 0:
                # Keep pacing bounded; a Telegram retry_after is handled by the caller.
                if delay > 2:
                    raise ServiceError('Telegram', 429, int(delay) + 1)
                time.sleep(delay)
            self.last_send = self.chat_sent[chat] = time.monotonic()
            try:
                return super().call(method, body)
            except ServiceError as error:
                if error.status == 429:
                    self.blocked_until = time.monotonic() + max(1, error.retry_after)
                raise


@dataclass
class Job:
    future: object
    kind: str
    cancel: threading.Event
    language: str


class PublicBot(FeatureUI):
    def __init__(self, root=ROOT, telegram=None, site_factory=EncryptedSite):
        self.root = root
        self.private = private_directory(root / '.secrets')
        self.users_root = private_directory(self.private / 'public-users')
        self.pending_root = private_directory(self.private / 'pending-logins')
        self.vault = Vault(self.private / 'public.key')
        # The process-wide lock is held before production startup. Clean sessions left
        # by an interrupted login; unfinished conversations are deliberately not resumed.
        for abandoned in self.pending_root.glob('login-*'):
            if abandoned.is_dir() and not abandoned.is_symlink():
                shutil.rmtree(abandoned)
        self.catalog = Catalog(root / 'data' / 'public.sqlite3')
        if self.catalog.added_timezone:
            owner = read_json(self.private / 'telegram.json').get('chat_id')
            if owner:
                self.catalog.update(owner, timezone_minutes=480)
        self.telegram = telegram or PacedTelegram(read_json(self.private / 'telegram.json')['token'])
        self.site_factory = site_factory
        self.executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix='teswirler')
        self.states = {}
        self.jobs = {}
        self.pending_actions = {}
        self.removing = set()
        self.offset = int(self.catalog.meta('update_offset') or 0)

    def user_path(self, chat_id, storage):
        # Both identifiers are generated/verified by us; never accept a path from Telegram.
        if not str(chat_id).isdigit() or len(storage or '') != 32 or any(c not in '0123456789abcdef' for c in storage):
            raise ValueError('Invalid user storage identifier')
        return self.users_root / str(chat_id) / storage

    def menu(self, user):
        lang = user['language']
        def button(key):
            return {'text': tr(lang, key), 'callback_data': key}
        return {'inline_keyboard': [[button('recent'), button('summary')],
                                    [button('status'), button('pause' if user['enabled'] else 'resume')],
                                    [button('settings'), button('connect')],
                                    [button('language'), button('timezone')],
                                    [button('disconnect')]]}

    def send(self, chat_id, key, *, keyboard=None, **values):
        user = self.catalog.user(chat_id)
        return self.telegram.call('sendMessage', {
            'chat_id': chat_id, 'text': tr(user['language'], key, **values),
            'reply_markup': keyboard if keyboard is not None else self.menu(user),
            'link_preview_options': {'is_disabled': True},
        })

    def languages(self, chat_id):
        self.send(chat_id, 'choose', keyboard={'inline_keyboard': [[
            {'text': 'Русский', 'callback_data': 'lang_ru'},
            {'text': 'Türkmençe', 'callback_data': 'lang_tk'},
        ]]})

    def cancel_input(self, chat_id):
        self.states.pop(chat_id, None)
        job = self.jobs.get(chat_id)
        if job and job.kind in ('login', 'feature:prepare_reply'):
            job.cancel.set()
        pending = self.pending_actions.get(chat_id)
        if pending and pending[0] == 'prepare_reply':
            self.pending_actions.pop(chat_id, None)

    def submit_feature(self, chat_id, action, values):
        user = self.catalog.user(chat_id)
        if chat_id in self.removing:
            return self.send(chat_id, 'disconnecting')
        if not user['site_user']:
            return self.send(chat_id, 'not_connected')
        if user['status'] == 'expired':
            return self.send(chat_id, 'session_expired')
        if chat_id in self.pending_actions or chat_id in self.jobs and self.jobs[chat_id].kind != 'poll':
            return self.send(chat_id, 'request_busy')
        self.pending_actions[chat_id] = (action, dict(values), user['storage'])
        key = {'recent': 'recent_loading', 'summary': 'summary_loading',
               'prepare_reply': 'reply_preparing', 'send_reply': 'reply_sending'}[action]
        self.send(chat_id, key)

    def dispatch_features(self):
        for chat_id, (action, values, storage) in list(self.pending_actions.items()):
            if chat_id in self.jobs or len(self.jobs) >= WORKERS:
                continue
            del self.pending_actions[chat_id]
            user = self.catalog.user(chat_id)
            if user['storage'] != storage or chat_id in self.removing:
                self.safe_send(chat_id, 'reply_stale' if action.endswith('reply') else 'not_connected')
                continue
            cancel = threading.Event()
            future = self.executor.submit(self.run_feature, user, action, values, cancel)
            self.jobs[chat_id] = Job(future, 'feature:' + action, cancel, user['language'])

    def set_timezone(self, chat_id, value):
        self.cancel_input(chat_id)
        match = re.fullmatch(r'([+-])(\d{1,2})(?::([0-5]\d))?', value.strip())
        if not match:
            return self.send(chat_id, 'timezone_invalid')
        minutes = (int(match[2]) * 60 + int(match[3] or 0)) * (1 if match[1] == '+' else -1)
        if not -720 <= minutes <= 840:
            return self.send(chat_id, 'timezone_invalid')
        self.catalog.update(chat_id, timezone_minutes=minutes)
        return self.send(chat_id, 'timezone_saved', zone=timezone_label(minutes))

    def command(self, chat_id, command, argument=''):
        user = self.catalog.user(chat_id)
        if chat_id in self.removing:
            return self.send(chat_id, 'disconnecting')
        if command == 'cancel' and self.pending_actions.get(chat_id, (None,))[0] == 'send_reply':
            self.pending_actions.pop(chat_id, None)
            self.states.pop(chat_id, None)
            return self.send(chat_id, 'cancelled')
        if command == 'cancel' and chat_id in self.jobs and self.jobs[chat_id].kind == 'feature:send_reply':
            return self.send(chat_id, 'request_busy')
        if command in ('settings', 'filters', 'quiet', 'grouping', 'digest', 'recent', 'summary'):
            self.cancel_input(chat_id)
            return self.feature_command(chat_id, command, argument)
        if command in ('start', 'language', 'timezone', 'cancel'):
            self.cancel_input(chat_id)
        if command in ('start', 'language'):
            return self.languages(chat_id)
        if command == 'timezone':
            return self.send(chat_id, 'timezone_choose', keyboard={'inline_keyboard': [
                [{'text': 'UTC+5', 'callback_data': 'tz_300'}, {'text': 'UTC+8', 'callback_data': 'tz_480'}],
                [{'text': 'UTC+3', 'callback_data': 'tz_180'}, {'text': 'UTC', 'callback_data': 'tz_0'}],
            ]})
        if command == 'cancel':
            return self.send(chat_id, 'cancelled')
        if command == 'help':
            return self.send(chat_id, 'help')
        if command == 'connect':
            if chat_id in self.jobs and self.jobs[chat_id].kind == 'login':
                return self.send(chat_id, 'busy')
            if user['site_user'] and user['status'] != 'expired':
                return self.send(chat_id, 'already')
            self.states[chat_id] = {'step': 'login', 'expires': time.time() + INPUT_TTL}
            return self.send(chat_id, 'login', keyboard={'inline_keyboard': [[
                {'text': tr(user['language'], 'cancel'), 'callback_data': 'cancel'}]]})
        if command == 'status':
            if not user['site_user']:
                return self.send(chat_id, 'not_connected')
            key = 'expired_state' if user['status'] == 'expired' else 'active' if user['enabled'] else 'paused_state'
            return self.send(chat_id, 'account', name=user['name'], state=tr(user['language'], key))
        if command in ('pause', 'resume'):
            if not user['site_user']:
                return self.send(chat_id, 'not_connected')
            if user['status'] == 'expired':
                return self.send(chat_id, 'session_expired')
            self.catalog.update(chat_id, enabled=int(command == 'resume'), status='active', next_run=0)
            if command == 'pause' and chat_id in self.jobs:
                self.jobs[chat_id].cancel.set()
            return self.send(chat_id, 'paused' if command == 'pause' else 'resumed')
        if command == 'disconnect':
            self.cancel_input(chat_id)
            if not user['site_user'] and chat_id not in self.jobs:
                return self.send(chat_id, 'not_connected')
            return self.send(chat_id, 'confirm_disconnect', keyboard={'inline_keyboard': [[
                {'text': tr(user['language'], 'yes_disconnect'), 'callback_data': 'unlink_yes'},
                {'text': tr(user['language'], 'cancel'), 'callback_data': 'cancel'},
            ]]})

    def remove_account(self, chat_id):
        user = self.catalog.user(chat_id)
        if user['storage']:
            path = self.user_path(chat_id, user['storage'])
            if path.exists():
                shutil.rmtree(path)
        self.catalog.update(chat_id, site_user=None, name=None, storage=None, enabled=0,
                            status='none', next_poll=0, next_run=0, failures=0, pending_manual=0)
        self.states.pop(chat_id, None)
        self.removing.discard(chat_id)
        self.send(chat_id, 'disconnected')

    def unlink(self, chat_id):
        self.cancel_input(chat_id)
        self.pending_actions.pop(chat_id, None)
        self.catalog.update(chat_id, enabled=0)
        self.removing.add(chat_id)
        if chat_id in self.jobs:
            self.jobs[chat_id].cancel.set()
            self.send(chat_id, 'disconnecting')
        else:
            self.remove_account(chat_id)

    def delete_input(self, chat_id, message_id):
        self.telegram.call('deleteMessage', {'chat_id': chat_id, 'message_id': message_id})

    def handle(self, update):
        callback = update.get('callback_query')
        message = callback.get('message', {}) if callback else update.get('message', {})
        chat = message.get('chat', {})
        sender = (callback or message).get('from', {})
        chat_id = chat.get('id')
        if chat.get('type') != 'private' or chat_id != sender.get('id') or sender.get('is_bot'):
            return
        language = 'ru' if sender.get('language_code', '').startswith('ru') else 'tk'
        self.catalog.user(chat_id, language)
        if callback:
            self.telegram.call('answerCallbackQuery', {'callback_query_id': callback['id']})
            action = callback.get('data')
            if action in ('lang_ru', 'lang_tk'):
                self.cancel_input(chat_id)
                self.catalog.update(chat_id, language=action[-2:])
                if self.catalog.user(chat_id)['site_user']:
                    return self.command(chat_id, 'status')
                return self.send(chat_id, 'welcome')
            if action == 'unlink_yes':
                return self.unlink(chat_id)
            if action in ('tz_300', 'tz_480', 'tz_180', 'tz_0'):
                self.cancel_input(chat_id)
                minutes = int(action[3:])
                self.catalog.update(chat_id, timezone_minutes=minutes)
                return self.send(chat_id, 'timezone_saved', zone=timezone_label(minutes))
            if action in ('connect', 'status', 'pause', 'resume', 'language', 'timezone', 'disconnect', 'cancel'):
                return self.command(chat_id, action)
            self.feature_callback(chat_id, action or '', message.get('message_id'))
            return
        text = message.get('text', '')
        command = text.split(maxsplit=1)[0].split('@')[0][1:] if text.startswith('/') else ''
        commands = {'start', 'connect', 'status', 'pause', 'resume', 'language', 'timezone', 'disconnect', 'cancel', 'help',
                    'settings', 'filters', 'quiet', 'grouping', 'digest', 'recent', 'summary'}
        if command in commands:
            # Commands with arguments could accidentally contain credentials; delete them too.
            if len(text.split(maxsplit=1)) > 1:
                try:
                    self.delete_input(chat_id, message['message_id'])
                except ServiceError:
                    pass
            if command == 'timezone' and len(text.split(maxsplit=1)) > 1:
                return self.set_timezone(chat_id, text.split(maxsplit=1)[1])
            argument = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ''
            return self.command(chat_id, command, argument)
        if self.feature_input(chat_id, message):
            return
        state = self.states.get(chat_id)
        try:
            # Delete all non-command input, including credentials sent after a restart.
            self.delete_input(chat_id, message['message_id'])
        except ServiceError:
            if state and state['step'] == 'password':
                self.states.pop(chat_id, None)
                return self.send(chat_id, 'delete_failed')
        if not state and chat_id in self.jobs and self.jobs[chat_id].kind == 'login':
            return self.send(chat_id, 'busy')
        if not state or state['expires'] < time.time() or message.get('date', time.time()) < time.time() - INPUT_TTL:
            self.states.pop(chat_id, None)
            return self.send(chat_id, 'expired_input')
        if state['step'] == 'login':
            if not text.strip() or len(text.strip()) > 254:
                return self.send(chat_id, 'invalid_login')
            self.states[chat_id] = {'step': 'password', 'identifier': text.strip(), 'expires': time.time() + INPUT_TTL}
            return self.send(chat_id, 'password', keyboard={'inline_keyboard': [[
                {'text': tr(self.catalog.user(chat_id)['language'], 'cancel'), 'callback_data': 'cancel'}]]})
        if state['step'] == 'password':
            if not text or len(text) > 1024:
                return self.send(chat_id, 'invalid_password')
            if chat_id in self.jobs or len(self.jobs) >= WORKERS:
                return self.send(chat_id, 'busy')
            if not self.catalog.attempt(chat_id):
                self.states.pop(chat_id, None)
                return self.send(chat_id, 'rate_limit')
            self.states.pop(chat_id, None)
            lang = self.catalog.user(chat_id)['language']
            cancel = threading.Event()
            future = self.executor.submit(self.login_job, state['identifier'], text, lang, cancel)
            self.jobs[chat_id] = Job(future, 'login', cancel, lang)
            self.send(chat_id, 'checking')

    def login_job(self, identifier, password, language, cancel):
        directory = Path(tempfile.mkdtemp(prefix='login-', dir=self.pending_root))
        try:
            site = self.site_factory(directory, self.vault)
            user = site.login(identifier, password)
            password = identifier = None
            if cancel.is_set():
                raise InterruptedError()
            rows = site.notifications()
            store = FeatureStore(directory / 'notifications.sqlite3', user['id'])
            try:
                store.observe(rows, language)
            finally:
                store.db.close()
            return {'directory': directory, 'site_user': str(user['id']),
                    'name': str(user.get('username') or user.get('name') or user['id'])[:150]}
        except BaseException:
            shutil.rmtree(directory)
            raise
        finally:
            password = identifier = None

    def poll_job(self, user, cancel):
        directory = self.user_path(user['chat_id'], user['storage'])
        site = self.site_factory(directory, self.vault)
        store = FeatureStore(directory / 'notifications.sqlite3', user['site_user'])
        next_poll = user['next_poll']
        sent = 0
        try:
            if cancel.is_set():
                pending, manual, due = store.pending(user, time.time())
                return next_poll, sent, pending, manual, due
            if user['enabled'] and time.time() >= next_poll:
                store.observe(site.notifications(), user=user)
                next_poll = time.time() + INTERVAL
            if user['enabled']:
                store.schedule_digest(user, time.time())
            row = store.delivery(user, time.time())
            if row and not cancel.is_set():
                self.telegram.call('sendMessage', {'chat_id': user['chat_id'], **queued_message(row[1], row[2])})
                with store.db:
                    store.db.execute('DELETE FROM outbox WHERE seq=?', (row[0],))
                sent = 1
            pending, manual, due = store.pending(user, time.time())
            return next_poll, sent, pending, manual, due
        finally:
            store.db.close()

    def finish_login(self, chat_id, result, job):
        directory = result['directory']
        try:
            if job.cancel.is_set() or chat_id in self.removing:
                return
            old = self.catalog.user(chat_id)
            other = self.catalog.db.execute('SELECT chat_id FROM users WHERE site_user=? AND chat_id!=?',
                                            (result['site_user'], chat_id)).fetchone()
            if other:
                self.send(chat_id, 'account_used')
                return
            # Reauthentication may only restore the same account. Switching requires explicit unlink.
            if old['site_user'] and old['site_user'] != result['site_user']:
                self.send(chat_id, 'already')
                return
            storage = uuid.uuid4().hex
            target = self.user_path(chat_id, storage)
            private_directory(target.parent)
            # Preserve undelivered notifications and deduplication state on reauthentication.
            if old['storage']:
                old_db = self.user_path(chat_id, old['storage']) / 'notifications.sqlite3'
                with closing(sqlite3.connect(old_db)) as source, closing(sqlite3.connect(directory / 'notifications.sqlite3')) as dest:
                    source.backup(dest)
            directory.replace(target)
            try:
                self.catalog.update(chat_id, site_user=result['site_user'], name=result['name'], storage=storage,
                                    enabled=1, status='active', next_poll=0, next_run=0, failures=0)
            except BaseException:
                shutil.rmtree(target)
                raise
            if old['storage']:
                shutil.rmtree(self.user_path(chat_id, old['storage']))
            self.send(chat_id, 'connected', name=result['name'])
        finally:
            if directory.exists():
                shutil.rmtree(directory)

    def reap(self):
        for chat_id, job in list(self.jobs.items()):
            if not job.future.done():
                continue
            del self.jobs[chat_id]
            try:
                result = job.future.result()
                if job.kind == 'login':
                    self.finish_login(chat_id, result, job)
                elif job.kind.startswith('feature:'):
                    self.finish_feature(chat_id, result, job)
                else:
                    next_poll, sent, pending = result[:3]
                    manual, due = result[3:] if len(result) > 3 else (False, time.time() + 1.2)
                    user = self.catalog.user(chat_id)
                    next_run = min(next_poll, due) if user['enabled'] else due
                    self.catalog.update(chat_id, next_poll=next_poll,
                                        next_run=next_run if pending else next_poll, pending_manual=int(manual), failures=0)
                    if sent:
                        LOG.info('Notification delivered')
            except Exception as error:
                # Exception messages or tracebacks from libraries might contain secrets.
                LOG.warning('User operation failed: kind=%s error=%s status=%s',
                            job.kind, type(error).__name__, getattr(error, 'status', 0))
                if not job.cancel.is_set() and chat_id not in self.removing:
                    if job.kind == 'login':
                        key = 'login_failed' if isinstance(error, ServiceError) and error.status in (400, 401, 403) else 'unavailable'
                        self.safe_send(chat_id, key)
                    elif job.kind.startswith('feature:'):
                        self.feature_error(chat_id, job.kind.split(':', 1)[1], error)
                    else:
                        self.poll_error(chat_id, error)
            if chat_id in self.removing:
                self.remove_account(chat_id)

    def safe_send(self, chat_id, key, **values):
        try:
            self.send(chat_id, key, **values)
        except ServiceError:
            LOG.warning('Interface message could not be delivered')

    def poll_error(self, chat_id, error):
        user = self.catalog.user(chat_id)
        # Identify the failing service as well as status: Telegram 403 means bot blocked.
        if (isinstance(error, InvalidToken) or
                isinstance(error, ServiceError) and error.status in (401, 403) and str(error).startswith('Teswirler')):
            self.catalog.update(chat_id, enabled=0, status='expired')
            self.safe_send(chat_id, 'session_expired')
        elif isinstance(error, ServiceError) and error.status == 403:
            self.catalog.update(chat_id, enabled=0, status='blocked')
        else:
            failures = user['failures'] + 1
            delay = max(min(300, INTERVAL * 2 ** min(failures, 4)),
                        min(3600, getattr(error, 'retry_after', 0)))
            self.catalog.update(chat_id, failures=failures, next_run=time.time() + delay)

    def schedule(self):
        self.dispatch_features()
        for user in self.catalog.due(time.time()):
            chat_id = user['chat_id']
            # Reserve half the slots for new logins and keep at most one job per chat.
            if len(self.jobs) >= WORKERS // 2:
                break
            if chat_id in self.jobs or chat_id in self.removing or chat_id in self.pending_actions:
                continue
            cancel = threading.Event()
            self.jobs[chat_id] = Job(self.executor.submit(self.poll_job, user, cancel),
                                     'poll', cancel, user['language'])

    def import_legacy(self):
        if self.catalog.meta('legacy_imported'):
            return
        config = read_json(self.private / 'telegram.json')
        if config.get('chat_id') and (self.private / 'site-session.json').exists():
            legacy = Teswirler(self.private)
            owner = legacy.session.get('user_id')
            if owner:
                chat_id = int(config['chat_id'])
                current = self.catalog.user(chat_id)
                if not current['site_user']:
                    storage = uuid.uuid4().hex
                    target = self.user_path(chat_id, storage)
                    site = EncryptedSite(target, self.vault)
                    site.session, site.cookies = dict(legacy.session), legacy.cookies
                    site.save()
                    source_path = self.root / 'data' / 'notifications.sqlite3'
                    if source_path.exists():
                        with closing(sqlite3.connect(source_path)) as source, closing(sqlite3.connect(target / 'notifications.sqlite3')) as dest:
                            source.backup(dest)
                    self.catalog.update(chat_id, site_user=str(owner), name='Teswirler', storage=storage,
                                        language='ru', enabled=1, status='active', timezone_minutes=480)
                    LOG.info('Existing personal subscription migrated')
        self.catalog.meta('legacy_imported', '1')

    def configure(self):
        if self.catalog.meta('interface_version') == '3':
            return
        for language in ('', 'ru', 'tk'):
            lang = language or 'ru'
            commands = [{'command': command, 'description': tr(lang, 'command_' + command)} for command in
                        ('start', 'connect', 'recent', 'summary', 'settings', 'filters', 'quiet', 'grouping', 'digest',
                         'status', 'pause', 'resume', 'language', 'timezone', 'disconnect', 'cancel', 'help')]
            self.telegram.call('setMyCommands', {'commands': commands, 'language_code': language})
            self.telegram.call('setMyDescription', {'description': tr(lang, 'description'), 'language_code': language})
            self.telegram.call('setMyShortDescription', {'short_description': tr(lang, 'short_description'), 'language_code': language})
        self.catalog.meta('interface_version', '3')

    def loop(self):
        config = read_json(self.private / 'telegram.json')
        identity = self.telegram.call('getMe')
        if identity['id'] != config['bot_id']:
            raise RuntimeError('Telegram bot identity mismatch')
        if self.telegram.call('getWebhookInfo').get('url'):
            raise RuntimeError('Existing Telegram webhook was preserved; long polling cannot start')
        self.import_legacy()
        try:
            self.configure()
        except ServiceError:
            LOG.warning('Telegram profile setup deferred until next restart')
        LOG.info('Public bilingual bot started')
        while True:
            self.reap()
            self.schedule()
            # Expire usernames held during abandoned login conversations.
            now = time.time()
            self.states = {key: state for key, state in self.states.items() if state['expires'] >= now}
            try:
                updates = self.telegram.call('getUpdates', {
                    'offset': self.offset, 'timeout': 1, 'limit': 50,
                    'allowed_updates': ['message', 'callback_query'],
                })
            except ServiceError as error:
                LOG.warning('Telegram update polling failed: status=%s', error.status)
                time.sleep(min(30, max(2, error.retry_after)))
                continue
            for update in updates:
                try:
                    self.handle(update)
                except Exception as error:
                    LOG.warning('Update handling failed: %s', type(error).__name__)
                finally:
                    self.offset = max(self.offset, update['update_id'] + 1)
                    self.catalog.meta('update_offset', self.offset)
                    update.clear()
            # Never store or log Telegram updates (they can contain passwords).
            updates.clear()
            if not updates:
                time.sleep(.1)

    def close(self):
        self.states.clear()
        self.pending_actions.clear()
        for job in self.jobs.values():
            job.cancel.set()
        self.executor.shutdown(wait=True, cancel_futures=True)
        for job in self.jobs.values():
            if job.kind == 'login' and not job.future.cancelled():
                try:
                    result = job.future.result()
                    shutil.rmtree(result['directory'])
                except Exception:
                    pass
        self.catalog.db.close()


def main():
    os.umask(0o077)
    private_directory(ROOT / 'logs')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=[
        logging.StreamHandler(), RotatingFileHandler(ROOT / 'logs' / 'public.log', maxBytes=1_000_000,
                                                    backupCount=3, encoding='utf-8')])
    app = None
    try:
        with single_instance():
            app = PublicBot()
            app.loop()
    except KeyboardInterrupt:
        LOG.info('Public bot stopped')
    except Exception as error:
        LOG.error('Public bot stopped: %s', type(error).__name__)
        return 1
    finally:
        if app:
            app.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
