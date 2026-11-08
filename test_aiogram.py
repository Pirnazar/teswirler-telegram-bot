import asyncio
import contextlib
from datetime import datetime, timezone
import io
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import GetMe, GetUpdates, GetWebhookInfo, SendMessage
from aiogram.types import Chat, Message, Update, User, WebhookInfo

from bot import ServiceError, read_json
from public_bot import PublicBot
from telegram_runtime import AiogramGateway, Checkpoint, Conversation, InitialOffset, pair_bot
from test_public_bot import FakeSite, FakeTelegram, message


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.methods=[]
        self.error=None
        self.closed=False
        self.updates=[]

    async def close(self):
        self.closed=True

    async def make_request(self, bot, method, timeout=None):
        self.methods.append(method)
        if self.error:
            raise self.error
        if isinstance(method,GetMe):
            return User(id=bot.id,is_bot=True,first_name='Test bot',username='test_bot')
        if isinstance(method,SendMessage):
            return Message(message_id=len(self.methods),date=datetime.now(timezone.utc),
                           chat=Chat(id=int(method.chat_id),type='private'),text=method.text,
                           reply_markup=method.reply_markup)
        if isinstance(method,GetUpdates):
            await asyncio.sleep(.01)
            return [update for update in self.updates if update.update_id >= (method.offset or 0)]
        if isinstance(method,GetWebhookInfo):
            return WebhookInfo(url='',has_custom_certificate=False,pending_update_count=0)
        return True

    async def stream_content(self,*args,**kwargs):
        if False:
            yield b''


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session=RecordingSession()
        self.bot=Bot('123456:TEST_TOKEN',session=self.session)
        self.gateway=AiogramGateway(bot=self.bot)
        self.addAsyncCleanup(self.gateway.close)

    async def test_typed_html_message_and_inline_keyboard(self):
        result=await self.gateway.call('sendMessage',{'chat_id':111,'text':'<b>Title</b>',
            'parse_mode':'HTML','reply_markup':{'inline_keyboard':[[{'text':'Open','url':'https://teswirler.com'}]]}})
        self.assertIsInstance(self.session.methods[-1],SendMessage)
        self.assertEqual(self.session.methods[-1].parse_mode,'HTML')
        self.assertEqual(result['reply_markup']['inline_keyboard'][0][0]['text'],'Open')
        self.assertIsNone(self.bot.default.parse_mode)

    async def test_network_exception_does_not_expose_credentials(self):
        self.session.error=TelegramNetworkError(method=GetMe(),message='SECRET request URL and password')
        with self.assertRaises(ServiceError) as caught:
            await self.gateway.call('getMe')
        self.assertNotIn('SECRET',str(caught.exception))
        self.assertEqual(caught.exception.status,0)

    async def test_retry_after_is_preserved_without_automatic_send_retry(self):
        self.session.error=TelegramRetryAfter(method=SendMessage(chat_id=111,text='test'),message='slow down',retry_after=8)
        with self.assertRaises(ServiceError) as caught:
            await self.gateway.call('sendMessage',{'chat_id':111,'text':'test'})
        self.assertEqual(caught.exception.retry_after,8)
        self.assertEqual(len(self.session.methods),1)

    async def test_chat_pacing_does_not_block_other_chats(self):
        await self.gateway.call('sendMessage',{'chat_id':111,'text':'first'})
        delayed=asyncio.create_task(self.gateway.call('sendMessage',{'chat_id':111,'text':'second'}))
        try:
            other=await asyncio.wait_for(self.gateway.call('sendMessage',{'chat_id':222,'text':'independent'}),timeout=.5)
            self.assertEqual(other['chat']['id'],222)
            self.assertFalse(delayed.done())
        finally:
            await delayed

    async def test_initial_offset_seeds_native_aiogram_polling_once(self):
        self.bot.session.middleware(InitialOffset(321))
        first=GetUpdates(timeout=0)
        await self.bot(first)
        self.assertEqual(first.offset,321)
        second=GetUpdates(timeout=0,offset=400)
        await self.bot(second)
        self.assertEqual(second.offset,400)

    async def test_administrator_setup_uses_native_dispatcher_and_checkpoints_pairing(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'telegram-token.txt').write_text('123456:TEST_TOKEN',encoding='utf-8')
            self.session.updates=[Update.model_validate(message(111,'/start a1b2c3d4'))]
            with patch('telegram_runtime.AiogramGateway',return_value=self.gateway), patch('secrets.token_hex',return_value='a1b2c3d4'):
                with contextlib.redirect_stdout(io.StringIO()):
                    await asyncio.wait_for(pair_bot(root),timeout=3)
            config=read_json(root/'.secrets/telegram.json')
            self.assertEqual(config['chat_id'],111)
            self.assertEqual(config['bot_id'],self.bot.id)
            self.assertEqual(config['update_offset'],2)
            self.assertFalse((root/'telegram-token.txt').exists())
            self.assertEqual(self.session.methods[-1].offset,2)


class RouterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.gateway=FakeTelegram()
        self.app=PublicBot(Path(self.temp.name),self.gateway,FakeSite)
        self.addCleanup(self.temp.cleanup)
        self.addAsyncCleanup(self.app.close)

    async def test_conversation_uses_native_fsm_and_never_keeps_password(self):
        await self.app.handle(message(111,'/connect'))
        self.assertEqual(await self.app.state(111).get_state(),Conversation.login.state)
        await self.app.handle(message(111,'one'))
        self.assertEqual(await self.app.state(111).get_state(),Conversation.password.state)
        await self.app.handle(message(111,'private-password'))
        self.assertIsNone(await self.app.state(111).get_state())
        self.assertEqual(await self.app.state(111).get_data(),{})
        await self.app.jobs[111].future
        await self.app.reap()
        self.assertEqual(self.app.catalog.user(111)['site_user'],'one')

    async def test_checkpoint_moves_only_after_handler_finishes(self):
        entered=asyncio.Event()
        release=asyncio.Event()
        original=self.gateway.call
        async def delayed(method,body=None):
            entered.set()
            await release.wait()
            return await original(method,body)
        with patch.object(self.gateway,'call',side_effect=delayed):
            request=asyncio.create_task(self.app.handle(message(111,'/help')))
            await entered.wait()
            self.assertEqual(self.app.offset,0)
            release.set()
            await request
        self.assertEqual(self.app.offset,2)
        self.assertEqual(self.app.catalog.meta('update_offset'),'2')

    async def test_cancelled_update_is_not_checkpointed(self):
        async def cancelled(event,data):
            raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await Checkpoint(self.app)(cancelled,Update.model_validate(message(111,'/help')), {})
        self.assertEqual(self.app.offset,0)

    async def test_update_errors_log_only_type_not_message_body(self):
        with patch.object(self.gateway,'call',side_effect=RuntimeError('SECRET body password')):
            with self.assertLogs('teswirler',level='WARNING') as captured:
                await self.app.handle(message(111,'/help'))
        self.assertNotIn('SECRET',' '.join(captured.output))
        self.assertNotIn('password',' '.join(captured.output))

    async def test_per_user_fsm_isolation_preserves_concurrent_input_order(self):
        original=self.gateway.call
        async def slow(method,body=None):
            await asyncio.sleep(.02)
            return await original(method,body)
        with patch.object(self.gateway,'call',side_effect=slow):
            tasks=[]
            for text in ('/connect','one','private-password'):
                tasks.append(asyncio.create_task(self.app.handle(message(111,text))))
                await asyncio.sleep(.005)
            await asyncio.gather(*tasks)
        self.assertIn(111,self.app.jobs)
        await self.app.jobs[111].future
        await self.app.reap()
        self.assertEqual(self.app.catalog.user(111)['site_user'],'one')

    async def test_slow_site_request_does_not_block_router(self):
        for text in ('/connect','one','private-password'):
            await self.app.handle(message(111,text))
        await self.app.jobs[111].future
        await self.app.reap()
        user=self.app.catalog.user(111)
        user['next_poll']=0
        started=threading.Event()
        def slow_site(instance):
            started.set()
            time.sleep(.3)
            return []
        with patch.object(FakeSite,'notifications',slow_site):
            poll=asyncio.create_task(self.app.poll_job(user,threading.Event()))
            while not started.is_set():
                await asyncio.sleep(.001)
            await asyncio.wait_for(self.app.handle(message(222,'/help')),timeout=.2)
            self.assertFalse(poll.done())
            await poll


if __name__=='__main__':
    unittest.main()
