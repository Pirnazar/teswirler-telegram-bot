from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from bot import ServiceError, Teswirler
from features import FeatureStore, quiet_until, digest_due, today_range
from features_ui import FeatureError
from public_bot import PublicBot
from test_bot import notification
from test_public_bot import FakeSite, FakeTelegram, message, callback


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def preferences(**values):
    return {'language': 'ru', 'timezone_minutes': 0, 'enabled': 1, 'quiet_start': -1, 'quiet_end': -1,
            'muted': '[]', 'grouping': 1, 'digest_enabled': 1, 'digest_minute': 1260, 'digest_since': 1, **values}


class FeatureStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'account.sqlite3'
        self.store = FeatureStore(self.path, 'one')
        self.now = timestamp('2026-11-09T12:00:00Z')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: self.store.db.close())

    def rows(self):
        return self.store.db.execute('SELECT seq,text,payload,kind,category,due FROM outbox ORDER BY seq').fetchall()

    def event(self, identifier='new', **values):
        return notification(identifier, createdAt='2026-11-09T11:30:00Z', **values)

    def test_filters_record_seen_and_activity_without_enqueuing(self):
        self.store.observe([], now=self.now)
        user = preferences(muted='["likes"]')
        self.store.observe([self.event(type='LIKE')], user=user, now=self.now)
        self.assertFalse(self.rows())
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM seen').fetchone()[0], 1)
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM activity').fetchone()[0], 1)
        self.store.observe([self.event(type='LIKE')], user=preferences(), now=self.now+30)
        self.assertFalse(self.rows())  # reenabling a type does not resend old events

    def test_filter_change_suppresses_already_queued_automatic_type(self):
        self.store.observe([], now=self.now)
        self.store.observe([self.event()], now=self.now)
        self.assertIsNone(self.store.delivery(preferences(muted='["answers"]'), self.now))
        self.assertFalse(self.rows())

    def test_manual_recent_ignores_filters_pause_and_quiet_hours(self):
        user = preferences(enabled=0, muted='["messages"]', quiet_start=0, quiet_end=23*60)
        self.store.queue_recent([self.event(type='MESSAGE', conversationId='room')], user, self.now)
        row = self.store.delivery(user, self.now)
        self.assertIsNotNone(row)
        payload = json.loads(row[2])
        self.assertIn('reply:', str(payload['reply_markup']))
        self.assertEqual(self.store.db.execute('SELECT COUNT(*) FROM seen').fetchone()[0], 0)

    def test_quiet_hours_retain_automatic_queue_then_release(self):
        self.store.observe([], now=self.now)
        self.store.observe([self.event()], now=self.now)
        user = preferences(quiet_start=11*60, quiet_end=13*60)
        self.assertIsNone(self.store.delivery(user, self.now))
        self.assertEqual(len(self.rows()), 1)
        self.assertIsNotNone(self.store.delivery(user, self.now+3600))

    def test_eight_likes_one_target_form_one_card_with_eight_actors(self):
        self.store.observe([], now=self.now)
        likes = [self.event(str(i), type='LIKE', blogId='post', actors=[{'id': str(i), 'name': f'Person {i}'}]) for i in range(8)]
        self.store.observe(likes, now=self.now)
        self.assertEqual(len(self.rows()), 1)
        self.assertIsNone(self.store.delivery(preferences(), self.now+19))
        rich = json.loads(self.store.delivery(preferences(), self.now+20)[2])
        self.assertIn('и ещё 7', rich['text'])
        self.store.observe(likes, now=self.now+30)
        self.assertEqual(len(self.rows()), 1)

    def test_likes_on_different_comments_are_not_combined(self):
        self.store.observe([], now=self.now)
        self.store.observe([self.event('a', type='LIKE', commentId='a'),
                            self.event('b', type='LIKE', commentId='b')], now=self.now)
        self.assertEqual(len(self.rows()), 2)

    def test_grouping_can_be_disabled(self):
        self.store.observe([], now=self.now)
        self.store.observe([self.event('a', type='LIKE'), self.event('b', type='LIKE')],
                           user=preferences(grouping=0), now=self.now)
        self.assertEqual(len(self.rows()), 2)
        self.assertIsNotNone(self.store.delivery(preferences(), self.now))

    def test_like_group_update_replaces_same_source_instead_of_double_counting(self):
        self.store.observe([], now=self.now)
        first=self.event(type='LIKE', actorCount=3)
        self.store.observe([first], now=self.now)
        self.store.observe([{**first, 'actorCount': 4}], now=self.now+10)
        rich=json.loads(self.rows()[0][2])
        self.assertEqual(len(self.rows()), 1)
        self.assertIn('и ещё 3', rich['text'])

    def test_summary_unique_activity_and_filters(self):
        rows=[self.event('one', type='MESSAGE'), self.event('two', type='LIKE'), self.event('three', type='ANSWER')]
        self.store.observe(rows, now=self.now)
        self.store.observe(rows, now=self.now+30)
        all_summary=self.store.summary(preferences(), self.now-3600, self.now+60)
        self.assertIn('Всего событий: 3', all_summary['text'])
        filtered=self.store.summary(preferences(muted='["messages","likes"]'), self.now-3600,self.now+60,respect_filters=True)
        self.assertIn('Всего событий: 1', filtered['text'])
        self.assertNotIn('Личные сообщения:', filtered['text'])

    def test_daily_digest_is_durable_and_not_repeated_after_restart(self):
        self.store.observe([self.event()], now=self.now)
        user=preferences(digest_minute=12*60, digest_since=self.now-86400)
        self.assertTrue(self.store.schedule_digest(user,self.now+60))
        self.assertFalse(self.store.schedule_digest(user,self.now+120))
        self.store.db.close()
        self.store=FeatureStore(self.path,'one')
        self.assertFalse(self.store.schedule_digest(user,self.now+120))
        self.assertEqual(len(self.rows()),1)

    def test_digest_not_sent_retroactively_on_feature_activation(self):
        self.assertFalse(self.store.schedule_digest(preferences(digest_minute=11*60,digest_since=self.now),self.now))
        self.assertTrue(self.store.schedule_digest(preferences(digest_minute=11*60,digest_since=self.now),self.now+86400))

    def test_digest_waits_for_quiet_end_and_can_be_disabled_while_queued(self):
        user=preferences(digest_minute=12*60,quiet_start=11*60,quiet_end=13*60)
        self.store.schedule_digest(user,self.now+60)
        self.assertIsNone(self.store.delivery(user,self.now+60))
        self.assertIsNotNone(self.store.delivery(user,self.now+3600))
        self.assertIsNone(self.store.delivery({**user,'digest_enabled':0},self.now+3600))
        self.assertFalse(self.rows())

    def test_reply_token_is_owned_scoped_and_expires(self):
        self.store.queue_recent([self.event(type='MESSAGE',conversationId='room')],preferences(),self.now)
        payload=json.loads(self.rows()[0][2])
        token=payload['reply_markup']['inline_keyboard'][1][0]['callback_data'][6:]
        self.assertEqual(self.store.reply_target(token,self.now)['conversation_id'],'room')
        self.assertIsNone(self.store.reply_target(token,self.now+8*86400))
        other=FeatureStore(Path(self.temp.name)/'other.sqlite3','two')
        try:
            self.assertIsNone(other.reply_target(token,self.now))
        finally:
            other.db.close()


class TimePolicyTests(unittest.TestCase):
    def test_cross_midnight_quiet_hours_in_user_timezone(self):
        now=timestamp('2026-11-08T20:00:00Z')  # 04:00 next day at UTC+8
        user=preferences(timezone_minutes=480,quiet_start=23*60,quiet_end=8*60)
        self.assertEqual(quiet_until(user,now),timestamp('2026-11-09T00:00:00Z'))
        self.assertEqual(quiet_until(user,now+4*3600),now+4*3600)

    def test_digest_schedule_uses_local_day_and_catches_latest_due_only(self):
        now=timestamp('2026-11-09T14:00:00Z') # 22:00 UTC+8
        user=preferences(timezone_minutes=480)
        self.assertEqual(digest_due(user,now),timestamp('2026-11-09T13:00:00Z'))
        self.assertEqual(today_range(user,now)[0],timestamp('2026-11-08T16:00:00Z'))


class ActionSite(FakeSite):
    rows={}
    conversations={}
    posts=[]
    failure=None

    def notifications(self):
        return self.rows.get(self.session.get('user_id'),[notification('old')])

    def conversation(self, identifier):
        return self.conversations.get(self.session.get('user_id'),{}).get(identifier)

    def post(self,path,body):
        self.posts.append((self.session['user_id'],path,body))
        if self.failure:
            raise self.failure
        return {'id':'sent-message'}


class FeatureBotTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.telegram=FakeTelegram()
        ActionSite.rows={}
        ActionSite.conversations={'one':{'room':{'id':'room','participants':[{'id':'peer','name':'Verified person'}]}},'two':{}}
        ActionSite.posts=[]
        ActionSite.failure=None
        self.app=PublicBot(Path(self.temp.name),self.telegram,ActionSite)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.app.close)
        self.connect(111,'one')

    def finish(self,chat=111,raises=False):
        future=self.app.jobs[chat].future
        if raises:
            with self.assertRaises(Exception):
                future.result(timeout=5)
        else:
            future.result(timeout=5)
        self.app.reap()

    def connect(self,chat,account):
        for text in ('/connect',account,'password-not-stored'):
            self.app.handle(message(chat,text))
        self.finish(chat)

    def action(self,action,values=None,chat=111,raises=False):
        self.app.submit_feature(chat,action,values or {})
        self.app.dispatch_features()
        self.finish(chat,raises)

    def target_token(self):
        user=self.app.catalog.user(111)
        store=self.app.feature_store(user)
        try:
            store.queue_recent([notification('incoming',type='MESSAGE',conversationId='room')],user,time.time())
            return store.db.execute('SELECT token FROM reply_targets').fetchone()[0]
        finally:
            store.db.close()

    def prepare_draft(self):
        self.action('prepare_reply',{'token':self.target_token()})
        self.assertEqual(self.app.states[111]['name'],'Verified person')
        self.app.handle(message(111,'Actual answer <b>literal</b> & text'))
        self.assertFalse(ActionSite.posts)
        return self.app.states[111]['nonce']

    def test_recent_is_accessible_while_paused_and_manual_delivery_drains(self):
        self.app.handle(message(111,'/pause'))
        ActionSite.rows['one']=[notification(str(i)) for i in range(12)]
        self.action('recent',{'count':5})
        user=self.app.catalog.user(111)
        self.assertTrue(user['pending_manual'])
        self.assertFalse(user['enabled'])
        self.assertEqual(len(self.app.catalog.due(time.time())),1)
        store=self.app.feature_store(user)
        try:
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM outbox WHERE kind='manual'").fetchone()[0],5)
        finally:
            store.db.close()
        self.app.poll_job(user,threading.Event())
        deliveries=[b for m,b in self.telegram.calls if m=='sendMessage' and b.get('parse_mode')=='HTML']
        self.assertTrue(deliveries)

    def test_settings_filters_and_quiet_are_scoped_and_persistent(self):
        self.connect(222,'two')
        self.app.handle(callback(111,'filter:likes'))
        self.app.handle(message(111,'/quiet 23:00-08:00'))
        self.app.handle(message(111,'/digest 20:30'))
        self.assertEqual(self.app.catalog.user(111)['muted'],'["likes"]')
        self.assertEqual(self.app.catalog.user(111)['quiet_start'],1380)
        self.assertEqual(self.app.catalog.user(111)['digest_minute'],1230)
        self.assertEqual(self.app.catalog.user(222)['muted'],'[]')
        for invalid in ('24:00-08:00','23:00-23:00','oops'):
            self.app.handle(message(111,'/quiet '+invalid))
            self.assertEqual(self.app.catalog.user(111)['quiet_start'],1380)

    def test_reply_requires_verification_confirmation_and_is_sent_once(self):
        nonce=self.prepare_draft()
        self.app.handle(callback(111,'send:'+nonce))
        self.app.dispatch_features()
        self.finish()
        self.app.handle(callback(111,'send:'+nonce))
        self.assertEqual(len(ActionSite.posts),1)
        self.assertEqual(ActionSite.posts[0],('one','/conversations/room/messages',{'content':'Actual answer <b>literal</b> & text'}))
        self.assertFalse(self.app.pending_actions)

    def test_forwarded_reply_token_cannot_access_another_account(self):
        token=self.target_token()
        self.connect(222,'two')
        self.action('prepare_reply',{'token':token},222,raises=True)
        self.assertNotIn(222,self.app.states)
        self.assertFalse(ActionSite.posts)

    def test_changed_recipient_after_preview_is_rejected(self):
        nonce=self.prepare_draft()
        ActionSite.conversations['one']['room']['participants']=[{'id':'different','name':'Different recipient'}]
        self.app.handle(callback(111,'send:'+nonce))
        self.app.dispatch_features()
        self.finish(raises=True)
        self.assertFalse(ActionSite.posts)

    def test_unknown_network_outcome_is_not_retried(self):
        nonce=self.prepare_draft()
        ActionSite.failure=ServiceError('Teswirler',0)
        self.app.handle(callback(111,'send:'+nonce))
        self.app.dispatch_features()
        self.finish(raises=True)
        self.app.handle(callback(111,'send:'+nonce))
        self.assertEqual(len(ActionSite.posts),1)
        self.assertTrue(any('могло уже дойти' in b.get('text','') for m,b in self.telegram.calls if m=='sendMessage'))

    def test_cancelled_draft_never_posts(self):
        nonce=self.prepare_draft()
        self.app.handle(message(111,'/cancel'))
        self.app.handle(callback(111,'send:'+nonce))
        self.assertFalse(self.app.pending_actions)
        self.assertFalse(ActionSite.posts)

    def test_cancel_removes_a_confirmed_reply_still_waiting_for_worker(self):
        nonce=self.prepare_draft()
        self.app.handle(callback(111,'send:'+nonce))
        self.assertIn(111,self.app.pending_actions)
        self.app.handle(message(111,'/cancel'))
        self.app.dispatch_features()
        self.assertFalse(self.app.pending_actions)
        self.assertFalse(ActionSite.posts)

    def test_new_draft_supersedes_previous_confirmation(self):
        old_nonce=self.prepare_draft()
        self.app.handle(message(111,'Changed answer'))
        new_nonce=self.app.states[111]['nonce']
        self.assertNotEqual(old_nonce,new_nonce)
        self.app.handle(callback(111,'send:'+old_nonce))
        self.assertFalse(self.app.pending_actions)
        self.app.handle(callback(111,'send:'+new_nonce))
        self.app.dispatch_features()
        self.finish()
        self.assertEqual(ActionSite.posts[0][2]['content'],'Changed answer')

    def test_reply_token_is_not_trusted_as_a_conversation_id(self):
        self.app.handle(callback(111,'reply:room'))
        self.assertFalse(self.app.pending_actions)


class WriteTransportTests(unittest.TestCase):
    def test_post_never_retries_network_or_server_error(self):
        for status in (0,500):
            with tempfile.TemporaryDirectory() as directory:
                site=Teswirler(Path(directory))
                site.session={'access_token':'token'}
                with patch('bot.request_json',side_effect=ServiceError('Teswirler',status)) as request:
                    with self.assertRaises(ServiceError):
                        site.post('/conversations/room/messages',{'content':'test'})
                    self.assertEqual(request.call_count,1)

    def test_post_refreshes_once_after_explicit_401(self):
        with tempfile.TemporaryDirectory() as directory:
            site=Teswirler(Path(directory))
            site.session={'access_token':'expired'}
            with patch('bot.request_json',side_effect=[ServiceError('Teswirler',401),{'data':{'id':'message'}}]) as request:
                with patch.object(site,'auth') as auth:
                    self.assertEqual(site.post('/messages',{'content':'test'}),{'id':'message'})
                    self.assertEqual(request.call_count,2)
                    auth.assert_called_once()


if __name__=='__main__':
    unittest.main()
