from concurrent.futures import Future
import copy
import http.cookiejar
import json
from pathlib import Path
import sqlite3
from string import Formatter
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from bot import ServiceError, Store, Teswirler, format_notification, read_json, write_json
from i18n import TEXT, tr
from public_bot import Catalog, EncryptedSite, Job, PublicBot, Vault
from test_bot import notification


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.fail_delete = False

    def call(self, method, body=None):
        self.calls.append((method, copy.deepcopy(body)))
        if method == 'deleteMessage' and self.fail_delete:
            raise ServiceError('Telegram', 400)
        return {'message_id': len(self.calls)}


class FakeSite(EncryptedSite):
    def login(self, identifier, password):
        if password == 'wrong':
            raise ServiceError('Teswirler', 401)
        self.session = {'user_id': identifier, 'access_token': 'access-SECRET', 'refresh_token': 'refresh-SECRET'}
        self.save()
        return {'id': identifier, 'username': identifier}

    def notifications(self):
        return [notification('old')]


def message(chat, text, date=None):
    return {'update_id': 1, 'message': {'message_id': 10, 'date': date or time.time(),
            'chat': {'id': chat, 'type': 'private'}, 'from': {'id': chat, 'language_code': 'ru'}, 'text': text}}


def callback(chat, action):
    return {'update_id': 1, 'callback_query': {'id': 'callback', 'from': {'id': chat},
            'message': {'chat': {'id': chat, 'type': 'private'}}, 'data': action}}


class PublicTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.telegram = FakeTelegram()
        self.app = PublicBot(self.root, self.telegram, FakeSite)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.app.close)

    def finish(self, chat):
        self.app.jobs[chat].future.result(timeout=5)
        self.app.reap()

    def connect(self, chat=111, account='one'):
        self.app.handle(message(chat, '/connect'))
        self.app.handle(message(chat, account))
        self.app.handle(message(chat, 'my-PASSWORD-secret'))
        self.finish(chat)
        return self.app.catalog.user(chat)

    def test_self_service_login_deletes_password_and_never_saves_it(self):
        user = self.connect()
        self.assertEqual(user['site_user'], 'one')
        self.assertTrue(user['enabled'])
        self.assertNotIn(111, self.app.states)
        self.assertEqual(len([c for c in self.telegram.calls if c[0] == 'deleteMessage']), 2)
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'my-PASSWORD-secret', path.read_bytes(), str(path))
                if path.name == 'session.enc':
                    self.assertNotIn(b'access-SECRET', path.read_bytes())
                    self.assertNotIn(b'refresh-SECRET', path.read_bytes())
        self.assertFalse(any('my-PASSWORD-secret' in str(body) for _, body in self.telegram.calls))

    def test_password_is_not_used_if_message_deletion_fails(self):
        self.app.handle(message(111, '/connect'))
        self.app.handle(message(111, 'one'))
        self.telegram.fail_delete = True
        self.app.handle(message(111, 'my-PASSWORD-secret'))
        self.assertNotIn(111, self.app.jobs)
        self.assertIsNone(self.app.catalog.user(111)['site_user'])
        self.assertEqual(self.telegram.calls[-1][1]['text'], tr('ru', 'delete_failed'))

    def test_two_accounts_are_isolated_and_new_alerts_go_to_correct_chat(self):
        first, second = self.connect(), self.connect(222, 'two')
        for user in (first, second):
            path = self.app.user_path(user['chat_id'], user['storage'])
            store = Store(path / 'notifications.sqlite3', user['site_user'])
            store.observe([notification('old'), notification('new', preview='private-' + user['site_user'])])
            store.db.close()
            user['next_poll'] = time.time() + 100
            self.app.poll_job(user, threading.Event())
        deliveries = [(b['chat_id'], b['text']) for m, b in self.telegram.calls if m == 'sendMessage' and 'private-' in b['text']]
        self.assertEqual(len(deliveries), 2)
        self.assertIn('private-one', deliveries[0][1])
        self.assertEqual(deliveries[0][0], 111)
        self.assertIn('private-two', deliveries[1][1])
        self.assertEqual(deliveries[1][0], 222)
        rich = [b for m, b in self.telegram.calls if m == 'sendMessage' and 'private-one' in b['text']][0]
        self.assertEqual(rich['parse_mode'], 'HTML')
        self.assertIn('reply_markup', rich)

    def test_duplicate_site_account_cannot_be_bound_to_another_chat(self):
        first = self.connect()
        self.connect(222, 'one')
        self.assertIsNone(self.app.catalog.user(222)['site_user'])
        self.assertEqual(self.app.catalog.user(111)['storage'], first['storage'])
        self.assertEqual(list(self.app.pending_root.iterdir()), [])

    def test_login_failure_removes_staged_session_and_does_not_echo_password(self):
        self.app.handle(message(111, '/connect'))
        self.app.handle(message(111, 'one'))
        self.app.handle(message(111, 'wrong'))
        with self.assertRaises(ServiceError):
            self.app.jobs[111].future.result(timeout=5)
        self.app.reap()
        self.assertIsNone(self.app.catalog.user(111)['site_user'])
        self.assertEqual(list(self.app.pending_root.iterdir()), [])

    def test_group_messages_and_spoofed_sender_are_ignored(self):
        update = message(111, '/connect')
        update['message']['chat']['type'] = 'group'
        self.app.handle(update)
        update['message']['chat']['type'] = 'private'
        update['message']['from']['id'] = 222
        self.app.handle(update)
        self.assertFalse(self.app.states)
        self.assertFalse(self.telegram.calls)

    def test_expired_input_is_deleted_without_login(self):
        self.app.handle(message(111, '/connect'))
        self.app.handle(message(111, 'one'))
        self.app.states[111]['expires'] = 0
        self.app.handle(message(111, 'my-PASSWORD-secret'))
        self.assertFalse(self.app.jobs)
        self.assertEqual(self.telegram.calls[-1][1]['text'], tr('ru', 'expired_input'))

    def test_restart_does_not_restore_login_or_password_state(self):
        self.app.handle(message(111, '/connect'))
        self.app.handle(message(111, 'one'))
        another = PublicBot(self.root, self.telegram, FakeSite)
        try:
            another.handle(message(111, 'my-PASSWORD-secret'))
            self.assertFalse(another.jobs)
            self.assertFalse(another.states)
        finally:
            another.close()

    def test_language_changes_persist_and_interface_is_turkmen(self):
        self.app.handle(message(111, '/start'))
        self.app.handle(callback(111, 'lang_tk'))
        self.assertEqual(self.app.catalog.user(111)['language'], 'tk')
        self.assertEqual(self.telegram.calls[-1][1]['text'], tr('tk', 'welcome'))
        self.app.handle(message(111, '/connect'))
        self.assertEqual(self.telegram.calls[-1][1]['text'], tr('tk', 'login'))

    def test_timezone_menu_and_custom_offset_are_scoped_per_user(self):
        self.app.catalog.user(222)
        self.app.handle(callback(111, 'tz_480'))
        self.assertEqual(self.app.catalog.user(111)['timezone_minutes'], 480)
        self.assertEqual(self.app.catalog.user(222)['timezone_minutes'], 300)
        self.app.handle(message(111, '/timezone -03:30'))
        self.assertEqual(self.app.catalog.user(111)['timezone_minutes'], -210)
        for invalid in ('+14:30', '-12:01', '+05:99', 'garbage'):
            self.app.handle(message(111, '/timezone ' + invalid))
            self.assertEqual(self.app.catalog.user(111)['timezone_minutes'], -210)
        self.app.handle(message(111, '/timezone'))
        self.assertIn('UTC+8', str(self.telegram.calls[-1][1]['reply_markup']))

    def test_existing_catalog_migration_sets_owner_timezone_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'data').mkdir()
            db = sqlite3.connect(root / 'data' / 'public.sqlite3')
            db.execute('CREATE TABLE users(chat_id INTEGER PRIMARY KEY, language TEXT, site_user TEXT UNIQUE, '
                       'name TEXT, storage TEXT, enabled INTEGER, status TEXT, next_run REAL, next_poll REAL, failures INTEGER)')
            db.executemany("INSERT INTO users VALUES (?,'ru',NULL,NULL,NULL,0,'none',0,0,0)", [(111,), (222,)])
            db.commit()
            db.close()
            write_json(root / '.secrets' / 'telegram.json', {'chat_id': 111})
            app = PublicBot(root, self.telegram, FakeSite)
            try:
                self.assertEqual(app.catalog.user(111)['timezone_minutes'], 480)
                self.assertEqual(app.catalog.user(222)['timezone_minutes'], 300)
            finally:
                app.close()

    def test_pause_resume_and_unlink_remove_only_own_storage(self):
        first, second = self.connect(), self.connect(222, 'two')
        self.app.handle(message(111, '/pause'))
        self.assertFalse(self.app.catalog.user(111)['enabled'])
        self.assertTrue(self.app.catalog.user(222)['enabled'])
        self.app.handle(message(111, '/resume'))
        self.assertTrue(self.app.catalog.user(111)['enabled'])
        self.app.handle(callback(111, 'unlink_yes'))
        self.assertIsNone(self.app.catalog.user(111)['site_user'])
        self.assertFalse(self.app.user_path(111, first['storage']).exists())
        self.assertTrue(self.app.user_path(222, second['storage']).exists())

    def test_disconnect_waits_for_inflight_work_before_final_confirmation(self):
        user = self.connect()
        future = Future()
        cancelled = threading.Event()
        self.app.jobs[111] = Job(future, 'poll', cancelled, 'ru')
        self.app.unlink(111)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(self.app.user_path(111, user['storage']).exists())
        self.assertEqual(self.telegram.calls[-1][1]['text'], tr('ru', 'disconnecting'))
        future.set_result((0, 0, False))
        self.app.reap()
        self.assertIsNone(self.app.catalog.user(111)['site_user'])
        self.assertEqual(self.telegram.calls[-1][1]['text'], tr('ru', 'disconnected'))

    def test_cancelled_login_never_binds_even_if_http_succeeded(self):
        self.app.handle(message(111, '/connect'))
        self.app.handle(message(111, 'one'))
        self.app.handle(message(111, 'my-PASSWORD-secret'))
        future = self.app.jobs[111].future
        future.result(timeout=5)
        self.app.handle(message(111, '/cancel'))
        self.app.reap()
        self.assertIsNone(self.app.catalog.user(111)['site_user'])
        self.assertEqual(list(self.app.pending_root.iterdir()), [])

    def test_session_expiry_disables_only_affected_account(self):
        self.connect()
        self.connect(222, 'two')
        self.app.poll_error(111, ServiceError('Teswirler', 401))
        self.assertEqual(self.app.catalog.user(111)['status'], 'expired')
        self.assertTrue(self.app.catalog.user(222)['enabled'])
        self.app.handle(message(111, '/connect'))
        self.assertIn(111, self.app.states)

    def test_telegram_block_does_not_misclassify_site_session_as_expired(self):
        self.connect()
        self.app.poll_error(111, ServiceError('Telegram', 403))
        self.assertEqual(self.app.catalog.user(111)['status'], 'blocked')
        self.assertFalse(self.app.catalog.user(111)['enabled'])

    def test_attempt_limits_persist(self):
        for _ in range(5):
            self.assertTrue(self.app.catalog.attempt(111))
        self.assertFalse(self.app.catalog.attempt(111))
        another = Catalog(self.root / 'data' / 'public.sqlite3')
        try:
            self.assertFalse(another.attempt(111))
            self.assertTrue(another.attempt(222))
        finally:
            another.db.close()

    def test_reauthentication_preserves_pending_notifications_and_seen_state(self):
        old = self.connect()
        old_path = self.app.user_path(111, old['storage'])
        store = Store(old_path / 'notifications.sqlite3', 'one')
        store.observe([notification('old'), notification('pending')])
        store.db.close()
        self.app.catalog.update(111, status='expired', enabled=0)
        renewed = self.connect()
        self.assertNotEqual(old['storage'], renewed['storage'])
        self.assertFalse(old_path.exists())
        store = Store(self.app.user_path(111, renewed['storage']) / 'notifications.sqlite3', 'one')
        try:
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM seen').fetchone()[0], 2)
        finally:
            store.db.close()

    def test_failed_delivery_keeps_only_that_users_queue(self):
        user = self.connect()
        self.connect(222, 'two')
        store = Store(self.app.user_path(111, user['storage']) / 'notifications.sqlite3', 'one')
        store.observe([notification('old'), notification('pending')])
        store.db.close()
        user['next_poll'] = time.time() + 100
        with patch.object(self.telegram, 'call', side_effect=ServiceError('Telegram', 429, 10)):
            with self.assertRaises(ServiceError):
                self.app.poll_job(user, threading.Event())
        store = Store(self.app.user_path(111, user['storage']) / 'notifications.sqlite3', 'one')
        try:
            self.assertEqual(store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
        finally:
            store.db.close()
        self.assertTrue(self.app.catalog.user(222)['enabled'])

    def test_restart_cleans_abandoned_encrypted_login_sessions(self):
        abandoned = self.app.pending_root / 'login-abandoned'
        abandoned.mkdir()
        (abandoned / 'session.enc').write_bytes(b'ciphertext')
        another = PublicBot(self.root, self.telegram, FakeSite)
        try:
            self.assertFalse(abandoned.exists())
        finally:
            another.close()

    def test_user_paths_reject_traversal(self):
        for chat, storage in [('../111', 'a'*32), (111, '../outside'), (111, '/'*32)]:
            with self.assertRaises(ValueError):
                self.app.user_path(chat, storage)

    def test_legacy_migration_preserves_dedup_and_queue_once(self):
        write_json(self.root / '.secrets' / 'telegram.json', {'chat_id': 111})
        legacy = Teswirler(self.root / '.secrets')
        legacy.session = {'user_id': 'one', 'access_token': 'access-SECRET'}
        legacy.save()
        store = Store(self.root / 'data' / 'notifications.sqlite3', 'one')
        store.observe([notification('old')])
        store.observe([notification('old'), notification('pending')])
        store.db.close()
        self.app.import_legacy()
        user = self.app.catalog.user(111)
        self.assertTrue(user['enabled'])
        imported = Store(self.app.user_path(111, user['storage']) / 'notifications.sqlite3', 'one')
        try:
            self.assertEqual(imported.db.execute('SELECT COUNT(*) FROM seen').fetchone()[0], 2)
            self.assertEqual(imported.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0], 1)
        finally:
            imported.db.close()
        self.app.unlink(111)
        self.app.import_legacy()
        self.assertIsNone(self.app.catalog.user(111)['site_user'])


class VaultTests(unittest.TestCase):
    def test_refresh_persists_new_access_token_encrypted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault = Vault(root / 'public.key')
            site = EncryptedSite(root / 'account', vault)
            site.session = {'access_token': 'expired'}
            with patch('bot.request_json', side_effect=[ServiceError('Teswirler', 401),
                       {'data': {'token': {'accessToken': 'new-SECRET-token'}}}, {'data': {'id': 'owner'}}]):
                self.assertEqual(site.get('/auth/me'), {'id': 'owner'})
            restored = EncryptedSite(root / 'account', vault)
            self.assertEqual(restored.session['access_token'], 'new-SECRET-token')
            self.assertNotIn(b'new-SECRET-token', (root / 'account' / 'session.enc').read_bytes())

    def test_refresh_cookie_and_access_token_roundtrip_encrypted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault = Vault(root / 'public.key')
            site = EncryptedSite(root / 'account', vault)
            site.session = {'access_token': 'SECRET-token'}
            cookie = http.cookiejar.Cookie(0, 'refresh', 'SECRET-cookie', None, False,
                'api.teswirler.com', False, False, '/', True, True, None, True, None, None, {'HttpOnly': None})
            site.cookies.set_cookie(cookie)
            site.save()
            restored = EncryptedSite(root / 'account', vault)
            self.assertEqual(restored.session['access_token'], 'SECRET-token')
            self.assertEqual(list(restored.cookies)[0].value, 'SECRET-cookie')
            self.assertNotIn(b'SECRET', (root / 'account' / 'session.enc').read_bytes())

    def test_missing_key_does_not_silently_replace_existing_encryption(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session = root / 'public-users' / '111' / ('a'*32) / 'session.enc'
            session.parent.mkdir(parents=True)
            session.write_bytes(b'encrypted')
            with self.assertRaises(RuntimeError):
                Vault(root / 'public.key')


class TranslationTests(unittest.TestCase):
    def test_languages_have_matching_keys_and_placeholders(self):
        self.assertEqual(TEXT['ru'].keys(), TEXT['tk'].keys())
        for key in TEXT['ru']:
            fields = lambda language: {field for _, field, _, _ in Formatter().parse(TEXT[language][key]) if field}
            self.assertEqual(fields('ru'), fields('tk'), key)

    def test_turkmen_notification_headers(self):
        self.assertIn('Täze teswir ýa-da jogap', format_notification(notification(), 'tk'))
        self.assertIn('Täze bildiriş', format_notification(notification(type='FUTURE'), 'tk'))


if __name__ == '__main__':
    unittest.main()
