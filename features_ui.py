"""Bilingual feature menus and explicit, recipient-verified reply confirmation."""
from __future__ import annotations

import html
import json
import re
import secrets
import threading
import time
import urllib.parse

from bot import ServiceError, SITE, escaped_excerpt, queued_message, timezone_label
from features import FeatureStore, CATEGORIES, muted_categories, today_range
from i18n import tr


class FeatureError(Exception):
    pass


def clock(value):
    match = re.fullmatch(r'([01]\d|2[0-3]):([0-5]\d)', value)
    return int(match[1]) * 60 + int(match[2]) if match else None


def clock_text(value):
    return f'{value // 60:02d}:{value % 60:02d}'


class FeatureUI:
    def feature_store(self, user):
        return FeatureStore(self.user_path(user['chat_id'], user['storage']) / 'notifications.sqlite3', user['site_user'])

    async def feature_panel(self, chat_id, key, keyboard, message_id=None, **values):
        user = self.catalog.user(chat_id)
        if message_id:
            try:
                return await self.telegram.call('editMessageText', {
                    'chat_id': chat_id, 'message_id': message_id,
                    'text': tr(user['language'], key, **values), 'reply_markup': keyboard,
                })
            except ServiceError:
                pass
        return await self.send(chat_id, key, keyboard=keyboard, **values)

    def pref_update(self, chat_id, **values):
        self.catalog.update(chat_id, **values, next_run=0)
        job = self.jobs.get(chat_id)
        if job and job.kind == 'poll':
            job.cancel.set()

    async def feature_command(self, chat_id, command, argument='', message_id=None):
        user = self.catalog.user(chat_id)
        lang = user['language']
        def button(key, data=None):
            return {'text': tr(lang, key), 'callback_data': data or key}
        back = [button('back_settings', 'settings')]
        quiet = (f'{clock_text(user["quiet_start"])}–{clock_text(user["quiet_end"])}'
                 if user['quiet_start'] >= 0 else tr(lang, 'off'))
        digest = clock_text(user['digest_minute']) if user['digest_enabled'] else tr(lang, 'off')
        if command == 'settings':
            await self.feature_panel(chat_id, 'settings_text', {'inline_keyboard': [
                [button('filters'), button('quiet')], [button('grouping'), button('digest')],
                [button('timezone'), button('language')],
                [button('disconnect')],
            ]}, message_id, count=8-len(muted_categories(user)), quiet=quiet,
                grouping=tr(lang, 'on' if user['grouping'] else 'off'), digest=digest,
                zone=timezone_label(user['timezone_minutes']))
        elif command == 'filters':
            muted = muted_categories(user)
            rows = [[{'text': ('☑ ' if key not in muted else '☐ ') + tr(lang, 'cat_' + key),
                      'callback_data': 'filter:' + key}] for key in CATEGORIES]
            rows += [[button('filters_all')], back]
            await self.feature_panel(chat_id, 'filters_text', {'inline_keyboard': rows}, message_id)
        elif command == 'quiet':
            if argument:
                if argument.lower() == 'off':
                    self.pref_update(chat_id, quiet_start=-1, quiet_end=-1)
                else:
                    parts = argument.split('-')
                    values = [clock(part) for part in parts]
                    if len(values) != 2 or None in values or values[0] == values[1]:
                        await self.send(chat_id, 'quiet_invalid')
                        return True
                    self.pref_update(chat_id, quiet_start=values[0], quiet_end=values[1])
                return await self.feature_command(chat_id, 'quiet', message_id=message_id)
            await self.feature_panel(chat_id, 'quiet_text', {'inline_keyboard': [
                [button('quiet_preset', 'quiet_on')], [button('quiet_off')], back,
            ]}, message_id, quiet=quiet, zone=timezone_label(user['timezone_minutes']))
        elif command == 'grouping':
            await self.feature_panel(chat_id, 'grouping_text', {'inline_keyboard': [
                [button('toggle_grouping', 'grouping_toggle')], back,
            ]}, message_id, state=tr(lang, 'on' if user['grouping'] else 'off'))
        elif command == 'digest':
            if argument:
                if argument.lower() == 'off':
                    self.pref_update(chat_id, digest_enabled=0)
                else:
                    minute = clock(argument)
                    if minute is None:
                        await self.send(chat_id, 'digest_invalid')
                        return True
                    self.pref_update(chat_id, digest_enabled=1, digest_minute=minute, digest_since=time.time())
                return await self.feature_command(chat_id, 'digest', message_id=message_id)
            await self.feature_panel(chat_id, 'digest_text', {'inline_keyboard': [
                [button('digest_toggle')], [button('digest_21')], back,
            ]}, message_id, state=digest, zone=timezone_label(user['timezone_minutes']))
        elif command == 'recent':
            if argument in ('5', '10'):
                await self.submit_feature(chat_id, 'recent', {'count': int(argument)})
            else:
                await self.feature_panel(chat_id, 'recent_choose', {'inline_keyboard': [
                    [button('recent_5'), button('recent_10')],
                ]}, message_id)
        elif command == 'summary':
            await self.submit_feature(chat_id, 'summary', {})
        else:
            return False
        return True

    async def feature_callback(self, chat_id, action, message_id=None):
        user = self.catalog.user(chat_id)
        if not action.startswith(('reply:', 'send:')):
            await self.cancel_input(chat_id)
        if action.startswith('filter:') and action[7:] in CATEGORIES:
            muted = muted_categories(user)
            key = action[7:]
            muted.symmetric_difference_update({key})
            self.pref_update(chat_id, muted=json.dumps(sorted(muted)))
            return await self.feature_command(chat_id, 'filters', message_id=message_id)
        if action == 'filters_all':
            self.pref_update(chat_id, muted='[]')
            return await self.feature_command(chat_id, 'filters', message_id=message_id)
        if action in ('quiet_on', 'quiet_off'):
            return await self.feature_command(chat_id, 'quiet', '23:00-08:00' if action == 'quiet_on' else 'off', message_id)
        if action == 'grouping_toggle':
            self.pref_update(chat_id, grouping=1-int(bool(user['grouping'])))
            return await self.feature_command(chat_id, 'grouping', message_id=message_id)
        if action in ('digest_toggle', 'digest_21'):
            if action == 'digest_21':
                return await self.feature_command(chat_id, 'digest', '21:00', message_id)
            self.pref_update(chat_id, digest_enabled=1-int(bool(user['digest_enabled'])), digest_since=time.time())
            return await self.feature_command(chat_id, 'digest', message_id=message_id)
        if action in ('recent_5', 'recent_10'):
            return await self.feature_command(chat_id, 'recent', action.split('_')[1], message_id)
        if action.startswith('reply:'):
            token = action[6:]
            if re.fullmatch(r'[0-9a-f]{24}', token):
                await self.cancel_input(chat_id)
                await self.submit_feature(chat_id, 'prepare_reply', {'token': token})
            else:
                await self.send(chat_id, 'reply_expired')
            return True
        if action.startswith('send:'):
            nonce = action[5:]
            state = await self.get_conversation(chat_id)
            if (not state or state['step'] != 'reply_confirm' or state['expires'] < time.time()
                    or not secrets.compare_digest(str(state['nonce']), nonce)
                    or state['storage'] != user['storage']):
                await self.send(chat_id, 'reply_stale')
                return True
            if chat_id in self.pending_actions or chat_id in self.jobs and self.jobs[chat_id].kind != 'poll':
                await self.send(chat_id, 'request_busy')
                return True
            await self.clear_conversation(chat_id)
            await self.submit_feature(chat_id, 'send_reply', state)
            return True
        return await self.feature_command(chat_id, action, message_id=message_id)

    async def feature_input(self, chat_id, message):
        state = await self.get_conversation(chat_id)
        if not state or state['step'] not in ('reply_text', 'reply_confirm'):
            return False
        if state['expires'] < time.time() or self.catalog.user(chat_id)['storage'] != state['storage']:
            await self.clear_conversation(chat_id)
            await self.send(chat_id, 'reply_stale')
            return True
        text = message.text or ''
        return await self.confirm_reply(chat_id, state, text)

    async def confirm_reply(self, chat_id, state, text):
        if not text.strip() or len(text.encode('utf-16-le')) // 2 > 4000:
            await self.send(chat_id, 'reply_invalid')
            return True
        lang = self.catalog.user(chat_id)['language']
        nonce = secrets.token_hex(12)
        confirmed = {**state, 'step': 'reply_confirm', 'text': text, 'nonce': nonce, 'expires': time.time() + 600}
        await self.set_conversation(chat_id, confirmed)
        preview = (f'<b>{html.escape(tr(lang,"reply_confirm_title"))}</b>\n'
                   + escaped_excerpt(tr(lang, 'reply_recipient', name=state['name']), 400)
                   + '\n\n<blockquote>' + escaped_excerpt(text, 2600) + '</blockquote>\n\n'
                   + html.escape(tr(lang, 'reply_full_length', count=len(text))))
        sent = await self.telegram.call('sendMessage', {'chat_id': chat_id, 'text': preview, 'parse_mode': 'HTML',
            'reply_markup': {'inline_keyboard': [[{'text': tr(lang, 'reply_send'), 'callback_data': 'send:' + nonce},
                                                {'text': tr(lang, 'cancel'), 'callback_data': 'cancel'}]]}})
        await self.remember_reply_prompt(chat_id, sent, confirmed)
        return True

    async def remember_reply_prompt(self, chat_id, sent, expected):
        current = await self.get_conversation(chat_id)
        if sent and sent.get('message_id') and current == expected:
            # Do not restore a draft cancelled while Telegram was delivering its preview.
            await self.set_conversation(chat_id, {**current, 'prompt_message_ids': [
                *current.get('prompt_message_ids', [])[-3:], sent['message_id']]})

    def run_feature(self, user, action, values, cancel):
        directory = self.user_path(user['chat_id'], user['storage'])
        site = self.site_factory(directory, self.vault)
        store = self.feature_store(user)
        now = time.time()
        try:
            if cancel.is_set():
                raise InterruptedError()
            if action in ('recent', 'summary'):
                rows = site.notifications()
                if cancel.is_set():
                    raise InterruptedError()
                if action == 'recent':
                    selected = rows[:values['count']]
                    store.queue_recent(selected, user, now)
                    return {'action': action, 'count': len(selected)}
                with store.db:
                    store.archive(rows, now)
                    start, end = today_range(user, now)
                    store.queue_summary(user, start, end)
                return {'action': action}
            if action == 'prepare_reply':
                token = values.get('token')
                if 'telegram_message_id' in values:
                    token = store.reply_message_token(values['telegram_message_id'], values.get('fallback_token'), now)
                target = store.reply_target(token, now)
                if not target:
                    raise FeatureError('reply_unavailable' if 'telegram_message_id' in values else 'reply_expired')
                conversation = site.conversation(target['conversation_id'])
                if not conversation:
                    raise FeatureError('reply_expired')
                recipient = self.verified_recipient(conversation, user)
                return {'action': action, 'conversation_id': str(conversation['id']), **recipient,
                        'storage': user['storage'], **({'draft_text': values['draft_text']} if 'draft_text' in values else {})}
            if action == 'send_reply':
                conversation = site.conversation(values['conversation_id'])
                if not conversation:
                    raise FeatureError('reply_failed')
                recipient = self.verified_recipient(conversation, user)
                if recipient['participants'] != values['participants']:
                    raise FeatureError('reply_stale')
                if cancel.is_set():
                    raise InterruptedError()
                # Persist the consumed confirmation before POST. No network retries after uncertainty.
                with store.db:
                    existing = store.db.execute('SELECT 1 FROM reply_sends WHERE nonce=?', (values['nonce'],)).fetchone()
                    if existing:
                        raise FeatureError('reply_stale')
                    store.db.execute('INSERT INTO reply_sends VALUES (?,?,?)', (values['nonce'], 'sending', now))
                try:
                    result = site.post('/conversations/' + urllib.parse.quote(values['conversation_id'], safe='') + '/messages',
                                       {'content': values['text']})
                    if not isinstance(result, dict) or not result.get('id'):
                        raise FeatureError('reply_uncertain')
                except BaseException:
                    with store.db:
                        store.db.execute("UPDATE reply_sends SET status='unknown' WHERE nonce=?", (values['nonce'],))
                    raise
                with store.db:
                    store.db.execute("UPDATE reply_sends SET status='sent' WHERE nonce=?", (values['nonce'],))
                return {'action': action, 'name': values['name']}
            raise ValueError('Unknown feature action')
        finally:
            store.db.close()

    @staticmethod
    def verified_recipient(conversation, user):
        people = [person for person in conversation.get('participants', []) if str(person.get('id')) != str(user['site_user'])]
        if not people or any(not person.get('id') for person in people):
            raise FeatureError('reply_failed')
        if conversation.get('isGroup'):
            name = tr(user['language'], 'reply_group_name', name=str(conversation.get('name') or '')[:100], count=len(people)+1)
        elif len(people) == 1:
            name = str(people[0].get('name') or people[0].get('username') or people[0]['id'])[:150]
        else:
            raise FeatureError('reply_failed')
        return {'name': name, 'participants': sorted(str(person['id']) for person in people)}

    async def finish_feature(self, chat_id, result, job):
        if job.cancel.is_set() or chat_id in self.removing:
            return
        action = result['action']
        if action in ('recent', 'summary'):
            self.catalog.update(chat_id, pending_manual=1, next_run=0)
            if action == 'recent':
                if result['count']:
                    await self.safe_send(chat_id, 'recent_done', count=result['count'])
                else:
                    await self.safe_send(chat_id, 'recent_empty')
        elif action == 'prepare_reply':
            draft = result.pop('draft_text', None)
            state = {**result, 'step': 'reply_text', 'expires': time.time() + 600}
            await self.set_conversation(chat_id, state)
            if draft is not None:
                await self.confirm_reply(chat_id, state, draft)
            else:
                sent = await self.safe_send(chat_id, 'reply_prompt', name=result['name'])
                await self.remember_reply_prompt(chat_id, sent, state)
        elif action == 'send_reply':
            await self.safe_send(chat_id, 'reply_sent', name=result['name'])

    async def feature_error(self, chat_id, action, error):
        if isinstance(error, FeatureError):
            await self.safe_send(chat_id, str(error))
        elif action == 'send_reply':
            uncertain = not isinstance(error, ServiceError) or error.status == 0 or error.status >= 500
            await self.safe_send(chat_id, 'reply_uncertain' if uncertain else 'reply_failed')
        else:
            await self.safe_send(chat_id, 'unavailable')
            if isinstance(error, ServiceError) and error.status in (401, 403) and str(error).startswith('Teswirler'):
                await self.poll_error(chat_id, error)
