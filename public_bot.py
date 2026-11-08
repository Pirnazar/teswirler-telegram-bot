"""Multi-user Russian/Turkmen bot. Passwords exist only in memory during login."""
from __future__ import annotations

import asyncio
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
from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import Message, CallbackQuery, Update
from telegram_runtime import AiogramGateway, COMMANDS, Conversation, InitialOffset, dispatcher_for

from bot import (ROOT, PRIVATE, LOG, ServiceError, Teswirler,
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
        self.telegram = telegram or AiogramGateway(read_json(self.private / 'telegram.json')['token'])
        self.bot = self.telegram.bot
        self.site_factory = site_factory
        self.executor = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix='teswirler')
        self.active_conversations = set()
        self.jobs = {}
        self.pending_actions = {}
        self.removing = set()
        self.offset = int(self.catalog.meta('update_offset') or read_json(self.private / 'telegram.json').get('update_offset', 0))
        self.dispatcher = dispatcher_for(self)
        self.bot.session.middleware(InitialOffset(self.offset))
        self.closed = False

    def user_path(self, chat_id, storage):
        # Both identifiers are generated/verified by us; never accept a path from Telegram.
        if not str(chat_id).isdigit() or len(storage or '') != 32 or any(c not in '0123456789abcdef' for c in storage):
            raise ValueError('Invalid user storage identifier')
        return self.users_root / str(chat_id) / storage

    def menu(self, user):
        lang = user['language']
        control = ('pause' if user['enabled'] else 'resume') if user['site_user'] and user['status'] != 'expired' else 'connect'
        return self.reply_keyboard(lang, [('recent', 'summary'), ('status', 'settings'), (control, 'language')])

    @staticmethod
    def reply_keyboard(language, rows):
        return {'keyboard': [[{'text': tr(language, 'nav_' + key)} for key in row] for row in rows],
                'resize_keyboard': True, 'is_persistent': True, 'one_time_keyboard': False,
                'input_field_placeholder': tr(language, 'input_hint')}

    def dialog_keyboard(self, user):
        return self.reply_keyboard(user['language'], [('cancel', 'menu')])

    @staticmethod
    def navigation_command(text):
        for lang in ('ru', 'tk'):
            for command in ('recent', 'summary', 'status', 'settings', 'pause', 'resume', 'connect', 'language', 'cancel', 'menu'):
                if text == tr(lang, 'nav_' + command):
                    return command
        return None

    async def send(self, chat_id, key, *, keyboard=None, **values):
        user = self.catalog.user(chat_id)
        if keyboard is None:
            keyboard = self.dialog_keyboard(user) if await self.get_conversation(chat_id) else self.menu(user)
        return await self.telegram.call('sendMessage', {
            'chat_id': chat_id, 'text': tr(user['language'], key, **values),
            'reply_markup': keyboard,
            'link_preview_options': {'is_disabled': True},
        })

    async def languages(self, chat_id):
        await self.send(chat_id, 'choose', keyboard={'inline_keyboard': [[
            {'text': 'Русский', 'callback_data': 'lang_ru'},
            {'text': 'Türkmençe', 'callback_data': 'lang_tk'},
        ]]})

    async def cancel_input(self, chat_id):
        await self.clear_conversation(chat_id)
        job = self.jobs.get(chat_id)
        if job and job.kind in ('login', 'feature:prepare_reply'):
            job.cancel.set()
        pending = self.pending_actions.get(chat_id)
        if pending and pending[0] == 'prepare_reply':
            self.pending_actions.pop(chat_id, None)

    async def submit_feature(self, chat_id, action, values):
        user = self.catalog.user(chat_id)
        if chat_id in self.removing:
            return await self.send(chat_id, 'disconnecting')
        if not user['site_user']:
            return await self.send(chat_id, 'not_connected')
        if user['status'] == 'expired':
            return await self.send(chat_id, 'session_expired')
        if chat_id in self.pending_actions or chat_id in self.jobs and self.jobs[chat_id].kind != 'poll':
            return await self.send(chat_id, 'request_busy')
        self.pending_actions[chat_id] = (action, dict(values), user['storage'])
        key = {'recent': 'recent_loading', 'summary': 'summary_loading',
               'prepare_reply': 'reply_preparing', 'send_reply': 'reply_sending'}[action]
        await self.send(chat_id, key, keyboard=self.dialog_keyboard(user) if action == 'prepare_reply' else None)

    async def dispatch_features(self):
        for chat_id, (action, values, storage) in list(self.pending_actions.items()):
            if chat_id in self.jobs or len(self.jobs) >= WORKERS:
                continue
            del self.pending_actions[chat_id]
            user = self.catalog.user(chat_id)
            if user['storage'] != storage or chat_id in self.removing:
                await self.safe_send(chat_id, 'reply_stale' if action.endswith('reply') else 'not_connected')
                continue
            cancel = threading.Event()
            future = asyncio.get_running_loop().run_in_executor(self.executor, self.run_feature, user, action, values, cancel)
            self.jobs[chat_id] = Job(future, 'feature:' + action, cancel, user['language'])

    async def set_timezone(self, chat_id, value):
        await self.cancel_input(chat_id)
        match = re.fullmatch(r'([+-])(\d{1,2})(?::([0-5]\d))?', value.strip())
        if not match:
            return await self.send(chat_id, 'timezone_invalid')
        minutes = (int(match[2]) * 60 + int(match[3] or 0)) * (1 if match[1] == '+' else -1)
        if not -720 <= minutes <= 840:
            return await self.send(chat_id, 'timezone_invalid')
        self.catalog.update(chat_id, timezone_minutes=minutes)
        return await self.send(chat_id, 'timezone_saved', zone=timezone_label(minutes))

    async def command(self, chat_id, command, argument=''):
        user = self.catalog.user(chat_id)
        if chat_id in self.removing:
            return await self.send(chat_id, 'disconnecting')
        if command in ('cancel', 'menu') and self.pending_actions.get(chat_id, (None,))[0] == 'send_reply':
            self.pending_actions.pop(chat_id, None)
            await self.clear_conversation(chat_id)
            return await self.send(chat_id, 'menu_text' if command == 'menu' else 'cancelled')
        if command in ('cancel', 'menu') and chat_id in self.jobs and self.jobs[chat_id].kind == 'feature:send_reply':
            return await self.send(chat_id, 'request_busy')
        if command in ('settings', 'filters', 'quiet', 'grouping', 'digest', 'recent', 'summary'):
            await self.cancel_input(chat_id)
            return await self.feature_command(chat_id, command, argument)
        if command in ('start', 'menu', 'language', 'timezone', 'cancel'):
            await self.cancel_input(chat_id)
        if command == 'menu':
            return await self.send(chat_id, 'menu_text')
        if command in ('start', 'language'):
            return await self.languages(chat_id)
        if command == 'timezone':
            return await self.send(chat_id, 'timezone_choose', keyboard={'inline_keyboard': [
                [{'text': 'UTC+5', 'callback_data': 'tz_300'}, {'text': 'UTC+8', 'callback_data': 'tz_480'}],
                [{'text': 'UTC+3', 'callback_data': 'tz_180'}, {'text': 'UTC', 'callback_data': 'tz_0'}],
            ]})
        if command == 'cancel':
            return await self.send(chat_id, 'cancelled')
        if command == 'help':
            return await self.send(chat_id, 'help')
        if command == 'connect':
            if chat_id in self.jobs and self.jobs[chat_id].kind == 'login':
                return await self.send(chat_id, 'busy')
            if user['site_user'] and user['status'] != 'expired':
                return await self.send(chat_id, 'already')
            await self.set_conversation(chat_id, {'step': 'login', 'expires': time.time() + INPUT_TTL})
            return await self.send(chat_id, 'login')
        if command == 'status':
            if not user['site_user']:
                return await self.send(chat_id, 'not_connected')
            key = 'expired_state' if user['status'] == 'expired' else 'active' if user['enabled'] else 'paused_state'
            return await self.send(chat_id, 'account', name=user['name'], state=tr(user['language'], key))
        if command in ('pause', 'resume'):
            if not user['site_user']:
                return await self.send(chat_id, 'not_connected')
            if user['status'] == 'expired':
                return await self.send(chat_id, 'session_expired')
            self.catalog.update(chat_id, enabled=int(command == 'resume'), status='active', next_run=0)
            if command == 'pause' and chat_id in self.jobs:
                self.jobs[chat_id].cancel.set()
            return await self.send(chat_id, 'paused' if command == 'pause' else 'resumed')
        if command == 'disconnect':
            await self.cancel_input(chat_id)
            if not user['site_user'] and chat_id not in self.jobs:
                return await self.send(chat_id, 'not_connected')
            return await self.send(chat_id, 'confirm_disconnect', keyboard={'inline_keyboard': [[
                {'text': tr(user['language'], 'yes_disconnect'), 'callback_data': 'unlink_yes'},
                {'text': tr(user['language'], 'cancel'), 'callback_data': 'cancel'},
            ]]})

    async def remove_account(self, chat_id):
        user = self.catalog.user(chat_id)
        if user['storage']:
            path = self.user_path(chat_id, user['storage'])
            if path.exists():
                shutil.rmtree(path)
        self.catalog.update(chat_id, site_user=None, name=None, storage=None, enabled=0,
                            status='none', next_poll=0, next_run=0, failures=0, pending_manual=0)
        await self.clear_conversation(chat_id)
        self.removing.discard(chat_id)
        await self.send(chat_id, 'disconnected')

    async def unlink(self, chat_id):
        await self.cancel_input(chat_id)
        self.pending_actions.pop(chat_id, None)
        self.catalog.update(chat_id, enabled=0)
        self.removing.add(chat_id)
        if chat_id in self.jobs:
            self.jobs[chat_id].cancel.set()
            await self.send(chat_id, 'disconnecting')
        else:
            await self.remove_account(chat_id)

    async def delete_input(self, chat_id, message_id):
        await self.telegram.call('deleteMessage', {'chat_id': chat_id, 'message_id': message_id})

    def state(self, chat_id):
        return FSMContext(self.dispatcher.storage, StorageKey(bot_id=self.bot.id, chat_id=chat_id, user_id=chat_id))

    async def get_conversation(self, chat_id):
        context = self.state(chat_id)
        if await context.get_state() is None:
            return None
        data = await context.get_data()
        if data.get('expires', 0) < time.time():
            await self.clear_conversation(chat_id)
            return None
        return data

    async def set_conversation(self, chat_id, data):
        context = self.state(chat_id)
        await context.set_state(getattr(Conversation, data['step']))
        await context.set_data(data)
        self.active_conversations.add(chat_id)

    async def clear_conversation(self, chat_id):
        await self.state(chat_id).clear()
        self.active_conversations.discard(chat_id)

    async def handle(self, update):
        """Feed an update through aiogram; useful for isolated router integration tests."""
        parsed = update if isinstance(update, Update) else Update.model_validate(update, context={'bot': self.bot})
        return await self.dispatcher.feed_update(self.bot, parsed)

    def register_sender(self, chat_id, sender):
        self.catalog.user(chat_id, 'ru' if (sender.language_code or '').startswith('ru') else 'tk')

    async def on_command(self, message: Message, command: CommandObject):
        chat_id = message.chat.id
        self.register_sender(chat_id, message.from_user)
        if command.args:
            try:
                await self.delete_input(chat_id, message.message_id)
            except ServiceError:
                pass
        if command.command == 'timezone' and command.args:
            return await self.set_timezone(chat_id, command.args)
        return await self.command(chat_id, command.command, (command.args or '').strip())

    async def on_navigation(self, message: Message, navigation: str):
        chat_id = message.chat.id
        self.register_sender(chat_id, message.from_user)
        if navigation not in ('cancel', 'menu'):
            await self.cancel_input(chat_id)
        return await self.command(chat_id, navigation)

    async def on_native_reply(self, message: Message):
        chat_id = message.chat.id
        self.register_sender(chat_id, message.from_user)
        original = message.reply_to_message
        if (not original.from_user or not original.from_user.is_bot or original.from_user.id != self.bot.id
                or original.chat.id != chat_id or original.forward_origin or original.is_automatic_forward):
            return await self.send(chat_id, 'reply_unavailable')
        if original.text in {tr(lang, key) for lang in ('ru', 'tk') for key in ('login', 'password')}:
            # A reply to an expired login prompt can still contain a password.
            await self.clear_conversation(chat_id)
            return await self.on_text(message, self.state(chat_id))
        if original.date.timestamp() < time.time() - 7 * 86400:
            return await self.send(chat_id, 'reply_unavailable')
        state = await self.get_conversation(chat_id)
        if state and original.message_id in state.get('prompt_message_ids', []):
            return await self.feature_input(chat_id, message)
        text = message.text or ''
        if not text.strip() or len(text.encode('utf-16-le')) // 2 > 4000:
            return await self.send(chat_id, 'reply_invalid')
        if chat_id in self.pending_actions or chat_id in self.jobs and self.jobs[chat_id].kind != 'poll':
            return await self.send(chat_id, 'request_busy')
        # Older cards have no persisted delivery mapping. Only accept their own
        # opaque reply token, never infer a recipient from quoted content or URLs.
        token = None
        if original.reply_markup:
            for row in original.reply_markup.inline_keyboard:
                for button in row:
                    if re.fullmatch(r'reply:[0-9a-f]{24}', button.callback_data or ''):
                        token = button.callback_data[6:]
        await self.cancel_input(chat_id)
        return await self.submit_feature(chat_id, 'prepare_reply', {
            'telegram_message_id': original.message_id, 'fallback_token': token, 'draft_text': text})

    async def on_callback(self, callback: CallbackQuery):
        message = callback.message
        chat_id = message.chat.id
        self.register_sender(chat_id, callback.from_user)
        await self.telegram.call('answerCallbackQuery', {'callback_query_id': callback.id})
        action = callback.data or ''
        if action in ('lang_ru', 'lang_tk'):
            await self.cancel_input(chat_id)
            self.catalog.update(chat_id, language=action[-2:])
            if self.catalog.user(chat_id)['site_user']:
                return await self.command(chat_id, 'status')
            return await self.send(chat_id, 'welcome')
        if action == 'unlink_yes':
            return await self.unlink(chat_id)
        if action in ('tz_300', 'tz_480', 'tz_180', 'tz_0'):
            await self.cancel_input(chat_id)
            minutes = int(action[3:])
            self.catalog.update(chat_id, timezone_minutes=minutes)
            return await self.send(chat_id, 'timezone_saved', zone=timezone_label(minutes))
        if action in ('connect', 'status', 'pause', 'resume', 'language', 'timezone', 'disconnect', 'cancel'):
            return await self.command(chat_id, action)
        return await self.feature_callback(chat_id, action, message.message_id)

    async def on_text(self, message: Message, state: FSMContext):
        chat_id = message.chat.id
        self.register_sender(chat_id, message.from_user)
        if await self.feature_input(chat_id, message):
            return
        text = message.text or ''
        conversation = await self.get_conversation(chat_id)
        try:
            # Password deletion precedes authentication, even after an interrupted conversation.
            await self.delete_input(chat_id, message.message_id)
        except ServiceError:
            if conversation and conversation['step'] == 'password':
                await self.clear_conversation(chat_id)
                return await self.send(chat_id, 'delete_failed')
        if not conversation and chat_id in self.jobs and self.jobs[chat_id].kind == 'login':
            return await self.send(chat_id, 'busy')
        if not conversation or message.date.timestamp() < time.time() - INPUT_TTL:
            await self.clear_conversation(chat_id)
            return await self.send(chat_id, 'expired_input')
        if conversation['step'] == 'login':
            if not text.strip() or len(text.strip()) > 254:
                return await self.send(chat_id, 'invalid_login')
            await self.set_conversation(chat_id, {'step': 'password', 'identifier': text.strip(), 'expires': time.time() + INPUT_TTL})
            return await self.send(chat_id, 'password')
        if conversation['step'] == 'password':
            if not text or len(text) > 1024:
                return await self.send(chat_id, 'invalid_password')
            if chat_id in self.jobs or len(self.jobs) >= WORKERS:
                return await self.send(chat_id, 'busy')
            if not self.catalog.attempt(chat_id):
                await self.clear_conversation(chat_id)
                return await self.send(chat_id, 'rate_limit')
            await self.clear_conversation(chat_id)
            language = self.catalog.user(chat_id)['language']
            cancel = threading.Event()
            future = asyncio.get_running_loop().run_in_executor(
                self.executor, self.login_job, conversation['identifier'], text, language, cancel)
            self.jobs[chat_id] = Job(future, 'login', cancel, language)
            text = conversation = None
            await self.send(chat_id, 'checking')

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

    def prepare_poll(self, user, cancel):
        directory = self.user_path(user['chat_id'], user['storage'])
        site = self.site_factory(directory, self.vault)
        store = FeatureStore(directory / 'notifications.sqlite3', user['site_user'])
        next_poll = user['next_poll']
        try:
            if cancel.is_set():
                return next_poll, None
            if user['enabled'] and time.time() >= next_poll:
                store.observe(site.notifications(), user=user)
                next_poll = time.time() + INTERVAL
            if user['enabled']:
                store.schedule_digest(user, time.time())
            return next_poll, store.delivery(user, time.time())
        finally:
            store.db.close()

    def complete_poll(self, user, next_poll, sent_seq, telegram_message_id=None):
        store = self.feature_store(user)
        try:
            if sent_seq is not None:
                with store.db:
                    store.remember_delivery(sent_seq, telegram_message_id, time.time())
                    store.db.execute('DELETE FROM outbox WHERE seq=?', (sent_seq,))
            pending, manual, due = store.pending(user, time.time())
            return next_poll, int(sent_seq is not None), pending, manual, due
        finally:
            store.db.close()

    async def poll_job(self, user, cancel):
        loop = asyncio.get_running_loop()
        next_poll, row = await loop.run_in_executor(self.executor, self.prepare_poll, user, cancel)
        sent_seq = None
        telegram_message_id = None
        if row and not cancel.is_set():
            delivered = await self.telegram.call('sendMessage', {'chat_id': user['chat_id'], **queued_message(row[1], row[2])})
            sent_seq = row[0]
            telegram_message_id = delivered['message_id']
        return await loop.run_in_executor(self.executor, self.complete_poll, user, next_poll, sent_seq, telegram_message_id)

    async def finish_login(self, chat_id, result, job):
        directory = result['directory']
        try:
            if job.cancel.is_set() or chat_id in self.removing:
                return
            old = self.catalog.user(chat_id)
            other = self.catalog.db.execute('SELECT chat_id FROM users WHERE site_user=? AND chat_id!=?',
                                            (result['site_user'], chat_id)).fetchone()
            if other:
                await self.send(chat_id, 'account_used')
                return
            # Reauthentication may only restore the same account. Switching requires explicit unlink.
            if old['site_user'] and old['site_user'] != result['site_user']:
                await self.send(chat_id, 'already')
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
            await self.send(chat_id, 'connected', name=result['name'])
        finally:
            if directory.exists():
                shutil.rmtree(directory)

    async def reap(self):
        for chat_id, job in list(self.jobs.items()):
            if not job.future.done():
                continue
            del self.jobs[chat_id]
            try:
                result = await job.future
                if job.kind == 'login':
                    await self.finish_login(chat_id, result, job)
                elif job.kind.startswith('feature:'):
                    await self.finish_feature(chat_id, result, job)
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
                        await self.safe_send(chat_id, key)
                    elif job.kind.startswith('feature:'):
                        await self.feature_error(chat_id, job.kind.split(':', 1)[1], error)
                    else:
                        await self.poll_error(chat_id, error)
            if chat_id in self.removing:
                await self.remove_account(chat_id)

    async def safe_send(self, chat_id, key, **values):
        try:
            return await self.send(chat_id, key, **values)
        except ServiceError:
            LOG.warning('Interface message could not be delivered')

    async def poll_error(self, chat_id, error):
        user = self.catalog.user(chat_id)
        # Identify the failing service as well as status: Telegram 403 means bot blocked.
        if (isinstance(error, InvalidToken) or
                isinstance(error, ServiceError) and error.status in (401, 403) and str(error).startswith('Teswirler')):
            self.catalog.update(chat_id, enabled=0, status='expired')
            await self.safe_send(chat_id, 'session_expired')
        elif isinstance(error, ServiceError) and error.status == 403:
            self.catalog.update(chat_id, enabled=0, status='blocked')
        else:
            failures = user['failures'] + 1
            delay = max(min(300, INTERVAL * 2 ** min(failures, 4)),
                        min(3600, getattr(error, 'retry_after', 0)))
            self.catalog.update(chat_id, failures=failures, next_run=time.time() + delay)

    async def schedule(self):
        await self.dispatch_features()
        for user in self.catalog.due(time.time()):
            chat_id = user['chat_id']
            # Reserve half the slots for new logins and keep at most one job per chat.
            if len(self.jobs) >= WORKERS // 2:
                break
            if chat_id in self.jobs or chat_id in self.removing or chat_id in self.pending_actions:
                continue
            cancel = threading.Event()
            self.jobs[chat_id] = Job(asyncio.create_task(self.poll_job(user, cancel)),
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

    async def configure(self):
        if self.catalog.meta('interface_version') == '4':
            return
        for language in ('', 'ru', 'tk'):
            lang = language or 'ru'
            commands = [{'command': command, 'description': tr(lang, 'command_' + command)} for command in COMMANDS]
            await self.telegram.call('setMyCommands', {'commands': commands, 'language_code': language})
            await self.telegram.call('setMyDescription', {'description': tr(lang, 'description'), 'language_code': language})
            await self.telegram.call('setMyShortDescription', {'short_description': tr(lang, 'short_description'), 'language_code': language})
        self.catalog.meta('interface_version', '4')

    async def worker_loop(self):
        while True:
            await self.reap()
            await self.schedule()
            for chat_id in list(self.active_conversations):
                await self.get_conversation(chat_id)
            await asyncio.sleep(.25)

    async def run(self):
        config = read_json(self.private / 'telegram.json')
        identity = await self.telegram.call('getMe')
        if identity['id'] != config['bot_id']:
            raise RuntimeError('Telegram bot identity mismatch')
        if (await self.telegram.call('getWebhookInfo')).get('url'):
            raise RuntimeError('Existing Telegram webhook was preserved; polling cannot start')
        self.import_legacy()
        try:
            await self.configure()
        except ServiceError:
            LOG.warning('Telegram profile setup deferred until next restart')
        LOG.info('Public bilingual bot started with aiogram 3')
        worker = asyncio.create_task(self.worker_loop(), name='notification-worker')
        poller = asyncio.create_task(self.dispatcher.start_polling(
            self.bot, allowed_updates=self.dispatcher.resolve_used_update_types(), polling_timeout=20,
            handle_as_tasks=False, close_bot_session=False), name='aiogram-polling')
        try:
            done, _ = await asyncio.wait((worker, poller), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                await task
        finally:
            for task in (worker, poller):
                if not task.done():
                    task.cancel()
            await asyncio.gather(worker, poller, return_exceptions=True)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        for chat_id in list(self.active_conversations):
            await self.clear_conversation(chat_id)
        self.pending_actions.clear()
        for job in self.jobs.values():
            job.cancel.set()
        # Let in-flight site requests finish before closing sessions and files.
        await asyncio.gather(*(job.future for job in self.jobs.values()), return_exceptions=True)
        for job in self.jobs.values():
            if job.kind == 'login' and not job.future.cancelled():
                try:
                    result = job.future.result()
                    shutil.rmtree(result['directory'])
                except Exception:
                    pass
        await asyncio.to_thread(self.executor.shutdown, wait=True, cancel_futures=True)
        await self.dispatcher.storage.close()
        await self.dispatcher.fsm.events_isolation.close()
        await self.telegram.close()
        self.catalog.db.close()


async def main():
    os.umask(0o077)
    private_directory(ROOT / 'logs')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', handlers=[
        logging.StreamHandler(), RotatingFileHandler(ROOT / 'logs' / 'public.log', maxBytes=1_000_000,
                                                    backupCount=3, encoding='utf-8')])
    # Poller errors from third-party libraries may contain URLs or update bodies.
    for name in ('aiogram', 'aiogram.event', 'aiogram.dispatcher', 'aiohttp'):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    app = None
    try:
        with single_instance():
            app = PublicBot()
            try:
                await app.run()
            finally:
                await app.close()
    except Exception as error:
        LOG.error('Public bot stopped: %s', type(error).__name__)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
