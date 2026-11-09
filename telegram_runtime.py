"""Native aiogram 3 transport, routing, transient FSM and update checkpointing."""
from __future__ import annotations

import asyncio
import logging
import time

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramConflictError,
    TelegramForbiddenError, TelegramNetworkError, TelegramNotFound, TelegramRetryAfter,
    TelegramServerError, TelegramUnauthorizedError)
from aiogram.filters import Command, Filter, StateFilter
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.methods import (AnswerCallbackQuery, DeleteMessage, EditMessageText, GetMe,
    GetMyCommands, GetUpdates, GetWebhookInfo, SendMessage, SetMyCommands,
    SetMyDescription, SetMyShortDescription)
from aiogram.types import CallbackQuery, Message, Update
from aiogram import BaseMiddleware

from bot import ServiceError

LOG = logging.getLogger('teswirler')
COMMANDS = ('start', 'menu', 'connect', 'recent', 'summary', 'settings', 'filters', 'quiet', 'grouping',
            'digest', 'status', 'pause', 'resume', 'language', 'timezone', 'disconnect', 'cancel', 'help')


class Conversation(StatesGroup):
    login = State()
    password = State()
    reply_text = State()
    reply_confirm = State()


class InitialOffset(BaseRequestMiddleware):
    """Resume aiogram's native poller from the existing durable checkpoint."""
    def __init__(self, offset):
        self.offset = offset
        self.initialized = False

    async def __call__(self, make_request, bot, method):
        if isinstance(method, GetUpdates) and not self.initialized:
            method.offset = max(method.offset or 0, self.offset)
            self.initialized = True
        return await make_request(bot, method)


class PrivateOwner(Filter):
    async def __call__(self, event: Message | CallbackQuery):
        message = event.message if isinstance(event, CallbackQuery) else event
        sender = event.from_user
        return (isinstance(message, Message) and message.chat.type == 'private' and sender is not None
                and not sender.is_bot and message.chat.id == sender.id)


class Checkpoint(BaseMiddleware):
    def __init__(self, app):
        self.app = app

    async def __call__(self, handler, event: Update, data):
        try:
            result = await handler(event, data)
        except asyncio.CancelledError:
            # Never acknowledge a password/update interrupted before processing finished.
            raise
        except Exception as error:
            LOG.warning('Telegram update handler failed: %s', type(error).__name__)
            result = None
        self.app.offset = max(self.app.offset, event.update_id + 1)
        self.app.catalog.meta('update_offset', self.app.offset)
        return result


class NavigationButton(Filter):
    def __init__(self, app):
        self.app = app

    async def __call__(self, message: Message):
        # A quoted reply containing a button label is still the user's reply text.
        command = self.app.navigation_command(message.text)
        return {'navigation': command} if command and not message.reply_to_message else False


class AiogramGateway:
    """Typed aiogram Bot API requests with asynchronous message pacing."""
    METHODS = {method.__api_method__: method for method in (
        SendMessage, DeleteMessage, EditMessageText, AnswerCallbackQuery, GetMe, GetWebhookInfo,
        SetMyCommands, SetMyDescription, SetMyShortDescription, GetMyCommands, GetUpdates,
    )}

    def __init__(self, token=None, *, bot=None):
        self.bot = bot or Bot(token, default=DefaultBotProperties(parse_mode=None))
        self.global_lock = asyncio.Lock()
        self.chat_locks = {}
        self.last_chat = {}
        self.last_global = 0
        self.blocked_until = 0

    async def _request(self, method, body):
        try:
            result = await self.bot(self.METHODS[method].model_validate(body or {}))
            # Serialize only fields received from Telegram. Nested models can
            # contain aiogram Default sentinels in fields absent from the response.
            options = {'mode': 'json', 'by_alias': True, 'exclude_none': True, 'exclude_unset': True}
            if isinstance(result, list):
                return [item.model_dump(**options)
                        if hasattr(item, 'model_dump') else item for item in result]
            return result.model_dump(**options) if hasattr(result, 'model_dump') else result
        except TelegramRetryAfter as error:
            self.blocked_until = time.monotonic() + error.retry_after
            raise ServiceError('Telegram', 429, error.retry_after) from None
        except (TelegramUnauthorizedError, TelegramForbiddenError, TelegramNotFound,
                TelegramConflictError, TelegramServerError, TelegramBadRequest) as error:
            codes = {TelegramUnauthorizedError: 401, TelegramForbiddenError: 403, TelegramNotFound: 404,
                     TelegramConflictError: 409, TelegramServerError: 500, TelegramBadRequest: 400}
            status = next(code for cls, code in codes.items() if isinstance(error, cls))
            raise ServiceError('Telegram', status) from None
        except (TelegramNetworkError, TelegramAPIError, OSError):
            # aiogram/aiohttp exception strings may include request URLs or input data.
            raise ServiceError('Telegram') from None

    async def call(self, method, body=None):
        if method != 'sendMessage':
            return await self._request(method, body)
        chat = body['chat_id']
        lock = self.chat_locks.setdefault(chat, asyncio.Lock())
        async with lock:
            delay = max(self.last_chat.get(chat, 0) + 1.1, self.blocked_until) - time.monotonic()
            if delay > 2:
                raise ServiceError('Telegram', 429, int(delay) + 1)
            if delay > 0:
                await asyncio.sleep(delay)
            async with self.global_lock:
                delay = max(self.last_global + .05, self.blocked_until) - time.monotonic()
                if delay > 2:
                    raise ServiceError('Telegram', 429, int(delay) + 1)
                if delay > 0:
                    await asyncio.sleep(delay)
                self.last_global = self.last_chat[chat] = time.monotonic()
            return await self._request(method, body)

    async def close(self):
        await self.bot.session.close()


def dispatcher_for(app):
    dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dispatcher.update.outer_middleware(Checkpoint(app))
    router = Router(name='teswirler')
    router.message.filter(PrivateOwner())
    router.callback_query.filter(PrivateOwner())
    router.message.register(app.on_command, Command(commands=list(COMMANDS)))
    router.message.register(app.on_navigation, NavigationButton(app))
    # A password sent as a reply to the login prompt must remain a credential.
    router.message.register(app.on_text, StateFilter(Conversation.login, Conversation.password))
    router.message.register(app.on_native_reply, F.reply_to_message)
    router.message.register(app.on_text, StateFilter(Conversation.reply_text, Conversation.reply_confirm))
    router.message.register(app.on_text)
    router.callback_query.register(app.on_callback)
    dispatcher.include_router(router)
    return dispatcher


async def pair_bot(root):
    """One-time administrator setup, using a separate native aiogram dispatcher."""
    import getpass
    import secrets
    from aiogram.filters import CommandStart
    from bot import read_json, write_json

    token_file = root / 'telegram-token.txt'
    config_file = root / '.secrets' / 'telegram.json'
    config = read_json(config_file)
    token = token_file.read_text(encoding='utf-8-sig').strip() if token_file.exists() else config.get('token')
    if not token:
        token = getpass.getpass('Telegram bot token: ').strip()
    gateway = AiogramGateway(token)
    dispatcher = Dispatcher()
    complete = asyncio.Event()
    saved = None
    polling = None
    try:
        identity = await gateway.call('getMe')
        if (await gateway.call('getWebhookInfo')).get('url'):
            raise RuntimeError('This bot has an active webhook; it was preserved.')
        code = secrets.token_hex(4)
        print(f"Open https://t.me/{identity['username']}?start={code}", flush=True)
        print(f'Or send this exact command privately to the bot: /start {code}', flush=True)
        print('Waiting up to 10 minutes for your pairing code...', flush=True)

        async def accept(message: Message, event_update: Update):
            nonlocal saved
            if message.text != f'/start {code}':
                return
            saved = {'token': token, 'chat_id': message.chat.id, 'bot_id': identity['id'],
                     'username': identity['username'], 'update_offset': event_update.update_id + 1}
            write_json(config_file, saved)
            await gateway.call('sendMessage', {'chat_id': message.chat.id,
                'text': 'Бот настроен. Для подключения аккаунта сайта отправьте /start после запуска службы.'})
            complete.set()

        router = Router(name='administrator-pairing')
        router.message.register(accept, PrivateOwner(), CommandStart())
        dispatcher.include_router(router)
        polling = asyncio.create_task(dispatcher.start_polling(gateway.bot, handle_signals=False,
            handle_as_tasks=False, allowed_updates=['message'], close_bot_session=False))
        await asyncio.wait_for(complete.wait(), 600)
        polling.cancel()
        await asyncio.gather(polling, return_exceptions=True)
        await gateway.call('getUpdates', {'offset': saved['update_offset'], 'timeout': 0, 'limit': 1})
        token_file.unlink(missing_ok=True)
        print('Telegram administrator paired. Token saved in .secrets/telegram.json.', flush=True)
    except ServiceError as error:
        raise RuntimeError(f'Telegram setup failed: HTTP {error.status}') from None
    except asyncio.TimeoutError:
        raise RuntimeError('Pairing timed out. Run python bot.py pair again.') from None
    finally:
        if polling and not polling.done():
            polling.cancel()
            await asyncio.gather(polling, return_exceptions=True)
        await gateway.close()
