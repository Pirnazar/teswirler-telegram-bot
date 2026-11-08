import copy
from datetime import datetime, timezone
import html
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bot import (ServiceError, Store, Teswirler, fingerprint, format_notification,
                 notification_url, notification_message, notification_date, queued_message)


def notification(identifier='one', **extra):
    return {'id': identifier, 'type': 'ANSWER', 'createdAt': '2026-11-08T12:00:00Z',
            'read': False, 'actors': [{'id': 'actor', 'name': 'Имя', 'username': 'author'}],
            'actorCount': 1, 'preview': 'Текст ответа', 'blog': {'slug': 'article'},
            'commentId': 'comment', **extra}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / 'state.sqlite3'
        self.store = Store(self.path, 'owner')
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(lambda: self.store.db.close())

    def queued(self):
        return self.store.db.execute('SELECT COUNT(*) FROM outbox').fetchone()[0]

    def test_baseline_then_new_then_repeat(self):
        old, new = notification(), notification('two')
        self.assertEqual(self.store.observe([old]), (False, 0))
        self.assertEqual(self.store.observe([old, new]), (True, 1))
        self.assertEqual(self.store.observe([old, new]), (True, 0))
        self.assertEqual(self.queued(), 1)

    def test_empty_baseline_still_delivers_first_new_item(self):
        self.store.observe([])
        self.assertEqual(self.store.observe([notification()]), (True, 1))

    def test_read_and_avatar_changes_do_not_resend(self):
        old = notification()
        new = copy.deepcopy(old)
        new['read'] = True
        new['updatedAt'] = 'later'
        new['actors'][0]['avatar'] = 'new.png'
        self.assertEqual(fingerprint(old), fingerprint(new))
        self.store.observe([old])
        self.assertEqual(self.store.observe([new]), (True, 0))

    def test_group_activity_and_new_message_preview_are_delivered(self):
        old = notification()
        self.store.observe([old])
        updated = {**old, 'actorCount': 2, 'preview': 'Другой ответ'}
        self.assertEqual(self.store.observe([updated]), (True, 1))

    def test_outbox_survives_restart_and_failed_delivery(self):
        self.store.observe([])
        self.store.observe([notification()])
        class Offline:
            def call(self, *args):
                raise ServiceError('Telegram', 429, 5)
        with self.assertRaises(ServiceError):
            self.store.flush(Offline(), 123)
        self.store.db.close()
        self.store = Store(self.path, 'owner')
        self.assertEqual(self.queued(), 1)
        calls = []
        class Online:
            def call(self, *args):
                calls.append(args)
        with patch('bot.time.sleep'):
            self.assertEqual(self.store.flush(Online(), 123), 1)
            self.assertEqual(self.store.flush(Online(), 123), 0)
        self.assertEqual(len(calls), 1)

    def test_account_cannot_receive_another_accounts_outbox(self):
        with self.assertRaises(RuntimeError):
            Store(self.path, 'different-owner')


class ApiTests(unittest.TestCase):
    def test_paginated_object_and_array_responses(self):
        for object_format in (True, False):
            with tempfile.TemporaryDirectory() as directory:
                site = Teswirler(Path(directory))
                first = [notification(str(i)) for i in range(50)]
                last = [notification('last')]
                pages = ([{'items': first, 'hasMore': True}, {'items': last, 'hasMore': False}]
                         if object_format else [first, last])
                with patch.object(site, 'get', side_effect=pages) as get:
                    self.assertEqual(len(site.notifications()), 51)
                    self.assertEqual(get.call_args.args[0], '/notifications?skip=50&take=50')

    def test_expired_access_token_refreshes_and_retries_get_only(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Teswirler(Path(directory))
            site.session['access_token'] = 'expired'
            def refresh(*args):
                site.session['access_token'] = 'fresh'
            with patch('bot.request_json', side_effect=[ServiceError('site', 401), {'data': {'id': 'owner'}}]) as request:
                with patch.object(site, 'auth', side_effect=refresh) as auth:
                    self.assertEqual(site.get('/auth/me'), {'id': 'owner'})
                    auth.assert_called_once_with('/auth/refresh', {})
                    self.assertEqual(request.call_args.kwargs['headers']['Authorization'], 'Bearer fresh')

    def test_malformed_notifications_do_not_become_empty_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            site = Teswirler(Path(directory))
            with patch.object(site, 'get', return_value={'unexpected': []}):
                with self.assertRaises(ServiceError):
                    site.notifications()


class MessageTests(unittest.TestCase):
    def test_rich_message_has_quote_bold_heading_and_context_button(self):
        payload = notification_message(notification(type='MESSAGE', conversationId='room'), 'tk', 480)
        self.assertEqual(payload['parse_mode'], 'HTML')
        self.assertIn('<b>Şahsy hat</b>', payload['text'])
        self.assertIn('<blockquote>Текст ответа</blockquote>', payload['text'])
        self.assertNotIn('https://', payload['text'])
        self.assertNotIn('T12:00:00', payload['text'])
        button = payload['reply_markup']['inline_keyboard'][0][0]
        self.assertEqual(button['text'], 'Haty açmak ↗')
        self.assertEqual(button['url'], 'https://teswirler.com/sahsy-hat/room')

    def test_html_escapes_source_content_and_preserves_paragraphs(self):
        payload = notification_message(notification(preview='<b>hello</b> & world\nsecond line',
                    actors=[{'name': '<a href="bad">Имя</a>'}]))
        self.assertIn('&lt;b&gt;hello&lt;/b&gt; &amp; world\nsecond line', payload['text'])
        self.assertNotIn('<a href=', payload['text'])

    def test_large_special_characters_and_emoji_fit_without_broken_html(self):
        payload = notification_message(notification(preview='😀&<' * 10000, message='>&😀' * 10000,
                    actors=[{'name': '&' * 10000}], amount='<' * 10000))
        self.assertLessEqual(len(payload['text'].encode('utf-16-le')) // 2, 4000)
        self.assertIn('…</blockquote>', payload['text'])
        self.assertNotIn('\ufffd', payload['text'])
        self.assertEqual(payload['text'].count('<blockquote>'), payload['text'].count('</blockquote>'))

    def test_identical_preview_is_not_duplicated(self):
        payload = notification_message(notification(preview='Same text', message='Same text'))
        self.assertEqual(payload['text'].count('Same text'), 1)

    def test_date_rollover_and_language(self):
        now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        stamp = '2026-11-08T16:43:51.957Z'
        self.assertEqual(notification_date(stamp, 'tk', 480, now), '9 noýabr · 00:43 · UTC+8')
        self.assertEqual(notification_date(stamp, 'ru', 300, now), '8 ноября · 21:43 · UTC+5')
        self.assertIn('2025', notification_date('2025-01-01T00:00:00Z', 'ru', 0, now))
        self.assertEqual(notification_date(stamp, 'ru', -210, now), '8 ноября · 13:13 · UTC-3:30')
        self.assertEqual(notification_date('not-a-date'), '')

    def test_legacy_queue_remains_plain_and_malformed_payload_falls_back(self):
        self.assertNotIn('parse_mode', queued_message('<b>literal text</b>'))
        self.assertNotIn('parse_mode', queued_message('<b>literal text</b>', '{broken'))

    def test_outbox_migration_preserves_legacy_message_and_rich_rows(self):
        import json
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'old.sqlite3'
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE outbox(seq INTEGER PRIMARY KEY, text TEXT NOT NULL)')
            db.execute('INSERT INTO outbox(text) VALUES (?)', ('Old <b>literal</b>',))
            db.commit()
            db.close()
            store = Store(path, 'owner')
            try:
                old = store.db.execute('SELECT text,payload FROM outbox').fetchone()
                self.assertEqual(old, ('Old <b>literal</b>', None))
                store.observe([])
                store.observe([notification()], 'tk', 480)
                rich = store.db.execute('SELECT payload FROM outbox ORDER BY seq DESC LIMIT 1').fetchone()[0]
                self.assertEqual(json.loads(rich)['parse_mode'], 'HTML')
                self.assertIn('UTC+8', json.loads(rich)['text'])
            finally:
                store.db.close()

    def test_links_match_site_routes(self):
        self.assertEqual(notification_url(notification()), 'https://teswirler.com/article#comment-comment')
        self.assertEqual(notification_url(notification(type='MESSAGE', conversationId='room')),
                         'https://teswirler.com/sahsy-hat/room')
        self.assertEqual(notification_url(notification(type='WITHDRAWAL_PAID')),
                         'https://teswirler.com/tolegler')
        self.assertEqual(notification_url(notification(blog=None, teswirId='post', teswirCommentId='reply')),
                         'https://teswirler.com/pikirler/post#comment-reply')

    def test_unrecognized_type_and_untrusted_text_remain_deliverable(self):
        text = format_notification(notification(type='FUTURE_TYPE', preview='<b>' + 'я' * 10000))
        self.assertIn('Новое уведомление', text)
        self.assertIn('<b>', text)  # plain text, no Telegram parse_mode
        self.assertLessEqual(len(text), 4000)


if __name__ == '__main__':
    unittest.main()
