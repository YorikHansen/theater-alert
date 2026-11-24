'''Application wiring: Discord interaction server and periodic checks.'''

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from theater_alert.alert import AlertEngine, CheckReport
from theater_alert.config import (
    CHECK_INTERVAL_HOURS,
    DISCORD_INTERACTIONS_HOST,
    DISCORD_INTERACTIONS_PORT,
    now,
)
from theater_alert.database.store import Database
from theater_alert.discord.api import DiscordClient, DiscordNotifier, NullDiscordClient
from theater_alert.discord.interactions import create_app
from theater_alert.theater.client import TheaterBrowser

LOGGER = logging.getLogger(__name__)
START_WATCH_INTERVAL_SECONDS = 30


async def check_forever(engine: AlertEngine, discord: DiscordNotifier) -> None:
    '''Check immediately, then every CHECK_INTERVAL_HOURS hours.'''
    while True:
        try:
            report = await engine.check()
            LOGGER.info(
                'Checked %s events: %s new, %s changed, %s messages sent, %s updated',
                report.events,
                report.new_events,
                report.changed_events,
                report.messages_sent,
                report.messages_updated,
            )
        except Exception as error:
            LOGGER.exception('Availability check failed')
            await discord.report_error(f'Availability check failed: {error}')
        await asyncio.sleep(CHECK_INTERVAL_HOURS * 60 * 60)


async def watch_event_starts(engine: AlertEngine, discord: DiscordNotifier) -> None:
    '''Redraw events briefly after their advertised start time.'''
    while True:
        await asyncio.sleep(START_WATCH_INTERVAL_SECONDS)
        try:
            redrawn = await engine.redraw_starting_events()
            if redrawn:
                LOGGER.info('Redrew %s messages for events that started', redrawn)
        except Exception as error:
            LOGGER.exception('Start-time watcher failed')
            await discord.report_error(f'Start-time watcher failed: {error}')


async def serve(headless: bool = True) -> None:
    '''Run the Discord bot, the interactions endpoint and the scheduler.'''
    database = Database()
    database.setup()

    async with DiscordClient() as discord, TheaterBrowser(headless=headless) as browser:
        engine = AlertEngine(browser, database, discord)
        runner = web.AppRunner(create_app(database, discord, engine))
        await runner.setup()
        site = web.TCPSite(runner, DISCORD_INTERACTIONS_HOST, DISCORD_INTERACTIONS_PORT)
        await site.start()
        LOGGER.info(
            'Listening for Discord interactions on port %s; checking every %s hours',
            DISCORD_INTERACTIONS_PORT,
            CHECK_INTERVAL_HOURS,
        )

        scheduler = asyncio.create_task(check_forever(engine, discord))
        watcher = asyncio.create_task(watch_event_starts(engine, discord))
        try:
            await asyncio.gather(scheduler, watcher)
        finally:
            for task in (scheduler, watcher):
                task.cancel()
            await asyncio.gather(scheduler, watcher, return_exceptions=True)
            await runner.cleanup()


async def check_once(
    discord: DiscordNotifier | None = None,
    headless: bool = True,
    force: bool = False,
) -> CheckReport:
    '''Run a single availability check (used by the CLI).'''
    database = Database()
    database.setup()
    client = discord or NullDiscordClient()
    async with TheaterBrowser(headless=headless) as browser:
        engine = AlertEngine(browser, database, client)
        return await engine.check(force=force)
