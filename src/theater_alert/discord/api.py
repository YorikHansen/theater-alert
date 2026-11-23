'''Discord HTTP API client used for commands and notifications.'''

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Protocol, Self

import aiohttp

from theater_alert.config import DISCORD_API_BASE, discord_bot_id, discord_bot_token

LOGGER = logging.getLogger(__name__)


class DiscordError(RuntimeError):
    '''A request to Discord failed.'''

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f'Discord API returned HTTP {status}: {body[:500]}')
        self.status = status
        self.body = body


class DiscordNotifier(Protocol):
    '''The notification methods used by the alert engine.'''

    async def create_dm_channel(self, user_id: str) -> str: ...

    async def send_message(
        self,
        channel_id: str,
        payload: dict[str, Any],
        image: bytes | None = None,
    ) -> dict[str, Any]: ...

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        payload: dict[str, Any],
        image: bytes | None = None,
    ) -> dict[str, Any]: ...

    async def delete_message(self, channel_id: str, message_id: str) -> None: ...

    async def report_error(self, message: str) -> None: ...


class DiscordClient:
    '''Thin async wrapper around the parts of Discord's HTTP API we need.'''

    def __init__(
        self,
        application_id: str | None = None,
        token: str | None = None,
        admin_id: str | None = None,
    ) -> None:
        self.application_id = application_id if application_id is not None else discord_bot_id()
        self.token = token if token is not None else discord_bot_token()
        self.admin_id = admin_id if admin_id is not None else os.environ.get(
            'DISCORD_BOT_ADMIN', ''
        ).strip()
        self.verify_key: str | None = None
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError('Discord client has not been started.')
        return self._session

    async def start(self) -> None:
        '''Open the HTTP session and register the application commands.'''
        self._session = aiohttp.ClientSession(
            headers={
                'Authorization': f'Bot {self.token}',
                'User-Agent': 'theater-alert/1.0',
            }
        )
        application = await self._request('GET', '/applications/@me')
        self.verify_key = application.get('verify_key') or None
        await self.register_commands()

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
        self._session = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | list[dict[str, Any]] | None = None,
        form: aiohttp.FormData | None = None,
    ) -> Any:
        url = f'{DISCORD_API_BASE}{path}'
        kwargs: dict[str, Any] = {}
        if payload is not None:
            kwargs['json'] = payload
        if form is not None:
            kwargs['data'] = form

        for attempt in range(4):
            async with self.session.request(method, url, **kwargs) as response:
                body = await response.text()
                if response.status == 429 and attempt < 3:
                    try:
                        delay = float(json.loads(body).get('retry_after', 1))
                    except (ValueError, AttributeError):
                        delay = 1.0
                    await asyncio.sleep(min(max(delay, 0.1), 10))
                    continue
                if response.status >= 400:
                    raise DiscordError(response.status, body)
                if not body:
                    return None
                try:
                    return json.loads(body)
                except ValueError:
                    return body

        raise DiscordError(429, 'Rate limited after retries.')

    async def register_commands(self) -> None:
        '''Publish the `/subscriptions` command tree.'''
        from theater_alert.discord.interactions import COMMANDS

        await self._request(
            'PUT',
            f'/applications/{self.application_id}/commands',
            payload=COMMANDS,
        )

    async def create_dm_channel(self, user_id: str) -> str:
        channel = await self._request(
            'POST',
            '/users/@me/channels',
            payload={'recipient_id': user_id},
        )
        return str(channel['id'])

    @staticmethod
    def _message_form(payload: dict[str, Any], image: bytes | None) -> aiohttp.FormData | None:
        if image is None:
            return None
        form = aiohttp.FormData()
        form.add_field(
            'payload_json',
            json.dumps(payload, ensure_ascii=False),
            content_type='application/json',
        )
        form.add_field('files[0]', image, filename='seatmap.png', content_type='image/png')
        return form

    async def send_message(
        self,
        channel_id: str,
        payload: dict[str, Any],
        image: bytes | None = None,
    ) -> dict[str, Any]:
        form = self._message_form(payload, image)
        result = await self._request(
            'POST',
            f'/channels/{channel_id}/messages',
            payload=payload if form is None else None,
            form=form,
        )
        return dict(result)

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        payload: dict[str, Any],
        image: bytes | None = None,
    ) -> dict[str, Any]:
        form = self._message_form(payload, image)
        result = await self._request(
            'PATCH',
            f'/channels/{channel_id}/messages/{message_id}',
            payload=payload if form is None else None,
            form=form,
        )
        return dict(result)

    async def delete_message(self, channel_id: str, message_id: str) -> None:
        await self._request('DELETE', f'/channels/{channel_id}/messages/{message_id}')

    async def send_followup(self, interaction_token: str, payload: dict[str, Any]) -> None:
        '''Answer a deferred interaction using its one-time webhook token.'''
        await self._request(
            'POST',
            f'/webhooks/{self.application_id}/{interaction_token}',
            payload=payload,
        )

    async def report_error(self, message: str) -> None:
        '''Send an error report to the configured administrator, if any.'''
        if not self.admin_id:
            return
        try:
            channel_id = await self.create_dm_channel(self.admin_id)
            await self.send_message(channel_id, {'content': f'Theater Alert: {message[:1500]}'})
        except Exception:
            LOGGER.exception('Could not notify the administrator about an error')


class NullDiscordClient:
    '''Discord stand-in for local commands that should not send messages.'''

    def __init__(self) -> None:
        self._counter = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def create_dm_channel(self, user_id: str) -> str:
        return f'local-dm-{user_id}'

    async def send_message(
        self,
        channel_id: str,
        payload: dict[str, Any],
        image: bytes | None = None,
    ) -> dict[str, Any]:
        self._counter += 1
        LOGGER.info('Would send message %s to %s', self._counter, channel_id)
        return {'id': f'local-message-{self._counter}'}

    async def edit_message(
        self,
        channel_id: str,
        message_id: str,
        payload: dict[str, Any],
        image: bytes | None = None,
    ) -> dict[str, Any]:
        LOGGER.info('Would update message %s in %s', message_id, channel_id)
        return {}

    async def delete_message(self, channel_id: str, message_id: str) -> None:
        LOGGER.info('Would delete message %s in %s', message_id, channel_id)

    async def send_followup(self, interaction_token: str, payload: dict[str, Any]) -> None:
        LOGGER.info('Would send followup for interaction %s', interaction_token)

    async def report_error(self, message: str) -> None:
        LOGGER.error(message)
