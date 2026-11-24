'''Periodic checks that turn scraped changes into Discord notifications.'''

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime

from theater_alert.config import EVENT_WINDOW_DAYS, now
from theater_alert.database.store import WILDCARD, Database, MessageRef
from theater_alert.discord.api import DiscordError, DiscordNotifier
from theater_alert.discord.messages import build_message
from theater_alert.models import Event, Seatmap
from theater_alert.seatmap.render import render_seatmap_image
from theater_alert.subscriptions.rules import is_subscribed, subscribed_users
from theater_alert.theater.client import TheaterBrowser
from theater_alert.theater.parser import events_in_window

LOGGER = logging.getLogger(__name__)


@dataclass
class CheckReport:
    '''Summary of one complete check run.'''

    events: int = 0
    new_events: int = 0
    changed_events: int = 0
    messages_sent: int = 0
    messages_updated: int = 0


class AlertEngine:
    '''Compare the current webshop state with the stored state and notify.'''

    def __init__(
        self,
        browser: TheaterBrowser,
        database: Database,
        discord: DiscordNotifier,
        fetch_delay: float = 0.5,
    ) -> None:
        self.browser = browser
        self.database = database
        self.discord = discord
        self.fetch_delay = fetch_delay
        self._lock = asyncio.Lock()

    async def check(self, force: bool = False) -> CheckReport:
        '''Run one check at a time, even if the scheduler and a command overlap.'''
        async with self._lock:
            return await self._check_impl(force=force)

    async def _check_impl(self, force: bool = False) -> CheckReport:
        '''Run one full check of all events inside the notification window.'''
        report = CheckReport()
        checked_at = now()
        all_events = await self.browser.list_all_events(today=checked_at.date())
        self.database.save_production_names(all_events)
        events = events_in_window(
            all_events,
            today=checked_at.date(),
            window_days=EVENT_WINDOW_DAYS,
        )
        active_ids: set[str] = set()

        for event in events:
            active_ids.add(event.id)
            try:
                seatmap = await self.browser.fetch_seatmap(event)
                state = self.database.save_event(event, seatmap, checked_at)
                report.events += 1

                existing = self.database.get_messages(event.id)
                if state.is_new:
                    report.new_events += 1
                    if existing:
                        await self._discard_messages(existing)
                        existing = []
                elif state.changed or force:
                    report.changed_events += 1

                if existing:
                    report.messages_updated += await self._update_messages(
                        event, seatmap, existing, checked_at
                    )
                if seatmap.available:
                    recipients = subscribed_users(self.database.all_rules(), event.production_id)
                    notified = {message.user_id for message in existing}
                    missing = [user_id for user_id in recipients if user_id not in notified]
                    if missing:
                        report.messages_sent += await self._notify_subscribers(
                            event, seatmap, missing, checked_at
                        )

                if self.fetch_delay:
                    await asyncio.sleep(self.fetch_delay)
            except Exception:
                LOGGER.exception('Could not check event %s', event.id)

        self.database.mark_inactive_except(active_ids)
        return report

    async def _discard_messages(self, messages: list[MessageRef]) -> None:
        '''Remove messages of an event that re-entered the notification window.'''
        for message in messages:
            try:
                await self.discord.delete_message(message.channel_id, message.message_id)
            except DiscordError:
                pass
            except Exception:
                LOGGER.exception('Could not delete message %s', message.message_id)
            self.database.delete_message(message.event_id, message.user_id)

    async def _notify_subscribers(
        self,
        event: Event,
        seatmap: Seatmap,
        user_ids: list[str],
        checked_at: datetime,
    ) -> int:
        if not user_ids:
            return 0

        image = render_seatmap_image(event, seatmap, checked_at)
        payload = build_message(event, seatmap, checked_at)
        sent = 0
        for user_id in user_ids:
            try:
                channel_id = self.database.get_dm_channel(user_id)
                if channel_id is None:
                    channel_id = await self.discord.create_dm_channel(user_id)
                    self.database.save_dm_channel(user_id, channel_id)
                message = await self.discord.send_message(channel_id, payload, image=image)
                self.database.save_message(event.id, user_id, channel_id, str(message['id']))
                sent += 1
            except Exception:
                LOGGER.exception('Could not notify user %s about event %s', user_id, event.id)
        return sent

    async def notify_new_subscriber(
        self,
        user_id: str,
        production_id: str,
        checked_at: datetime | None = None,
    ) -> int:
        '''Send current available event notifications for a newly subscribed production.'''
        checked_at = checked_at or now()
        rules = self.database.all_rules().get(user_id, {})
        if production_id == WILDCARD:
            events = self.database.active_available_events()
        else:
            events = self.database.active_available_events(production_id)
        sent = 0
        for event in events:
            if not is_subscribed(rules, event.production_id):
                continue
            if self.database.has_message(event.id, user_id):
                continue
            seatmap = self.database.event_seatmap(event.id)
            if not seatmap.available:
                continue
            sent += await self._notify_subscribers(event, seatmap, [user_id], checked_at)
        return sent

    async def _update_messages(
        self,
        event: Event,
        seatmap: Seatmap,
        messages: list[MessageRef],
        checked_at: datetime,
    ) -> int:
        image = render_seatmap_image(event, seatmap, checked_at)
        payload = build_message(event, seatmap, checked_at)
        updated = 0
        for message in messages:
            try:
                try:
                    await self.discord.edit_message(
                        message.channel_id, message.message_id, payload, image=image
                    )
                except DiscordError as error:
                    if error.status not in {400, 404}:
                        raise
                    try:
                        await self.discord.delete_message(
                            message.channel_id, message.message_id
                        )
                    except DiscordError:
                        pass
                    new_message = await self.discord.send_message(
                        message.channel_id, payload, image=image
                    )
                    self.database.save_message(
                        event.id, message.user_id, message.channel_id, str(new_message['id'])
                    )
                updated += 1
            except Exception:
                LOGGER.exception('Could not update message for event %s', event.id)
        return updated
