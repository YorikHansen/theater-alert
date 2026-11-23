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
)
from theater_alert.database.store import Database
from theater_alert.discord.api import DiscordClient, DiscordNotifier, NullDiscordClient
from theater_alert.discord.interactions import create_app
from theater_alert.theater.client import TheaterBrowser

LOGGER = logging.getLogger(__name__)


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
        try:
            await scheduler
        finally:
            scheduler.cancel()
            await asyncio.gather(scheduler, return_exceptions=True)
            await runner.cleanup()


async def check_once(
    discord: DiscordNotifier | None = None,
    headless: bool = True,
) -> CheckReport:
    '''Run a single availability check (used by the CLI).'''
    database = Database()
    database.setup()
    client = discord or NullDiscordClient()
    async with TheaterBrowser(headless=headless) as browser:
        engine = AlertEngine(browser, database, client)
        return await engine.check()
