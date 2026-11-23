'''Command-line interface for Theater Alert.'''

from __future__ import annotations

import asyncio
import logging
import os

import typer

from theater_alert import __version__
from theater_alert.app import check_once, serve
from theater_alert.config import BASE_URL, DATA_DIR, now
from theater_alert.database.store import Database
from theater_alert.discord.api import DiscordClient
from theater_alert.models import Event
from theater_alert.seatmap.render import render_seatmap_image
from theater_alert.theater.client import TheaterBrowser

app = typer.Typer(
    name='theater-alert',
    help='Monitor seat availability at Theater Kiel.',
    no_args_is_help=True,
)


@app.callback()
def common(
    ctx: typer.Context,
    headed: bool = typer.Option(False, '--headed', is_flag=True, help='Show the Chromium window.'),
) -> None:
    ctx.obj = headed


def _headless(ctx: typer.Context | None) -> bool:
    return not bool(ctx and ctx.obj)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    )


async def _find_event(browser: TheaterBrowser, event_id: str) -> Event | None:
    return next((event for event in await browser.list_events() if event.id == event_id), None)


async def _scrape(headless: bool = True) -> None:
    async with TheaterBrowser(headless=headless) as browser:
        events = await browser.list_events()
    typer.echo(f'{len(events)} events in the notification window:')
    for event in events:
        typer.echo(
            f'{event.id} | {event.date_label} {event.time} | {event.location} | '
            f'{event.title} | production {event.production_id}'
        )


async def _event_details(event_id: str, headless: bool = True) -> None:
    async with TheaterBrowser(headless=headless) as browser:
        event = await _find_event(browser, event_id)
        if event is None:
            typer.echo(f'Unknown event {event_id}.', err=True)
            raise typer.Exit(code=1)
        seatmap = await browser.fetch_seatmap(event)
    typer.echo(f'Seatmap for event {event_id}:')
    for group in seatmap.groups:
        state = 'verfügbar' if group.available else 'ausverkauft'
        typer.echo(f'{group.name}: {group.price_label} [{group.color}] ({state})')
    if not seatmap.available:
        typer.echo('Keine Plätze verfügbar.')


async def _render(event_id: str, headless: bool = True) -> None:
    database = Database()
    database.setup()
    event = database.get_event(event_id)
    async with TheaterBrowser(headless=headless) as browser:
        if event is None:
            event = await _find_event(browser, event_id)
        if event is None:
            typer.echo(f'Unknown event {event_id}.', err=True)
            raise typer.Exit(code=1)
        seatmap = await browser.fetch_seatmap(event)

    image = render_seatmap_image(event, seatmap, now())
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = DATA_DIR / f'seatmap-{event_id}.png'
    target.write_bytes(image)
    typer.echo(str(target))
    async with TheaterBrowser(headless=headless) as browser:
        event = await _find_event(browser, event_id)
        if event is None:
            typer.echo(f'Unknown event {event_id}.', err=True)
            raise typer.Exit(code=1)
        page = await browser.new_page()
        try:
            await browser._open_base(page)
            await page.goto(event.url or BASE_URL)
            await page.wait_for_selector('img.leaflet-tile-loaded', timeout=15_000)
            layout = await TheaterBrowser._read_map_tiles(page)
        finally:
            await page.close()

    if not layout:
        typer.echo('No leaflet tiles found.', err=True)
        raise typer.Exit(code=1)
    typer.echo(f'{len(layout["tiles"])} leaflet tiles loaded:')
    for tile in layout['tiles'][:20]:
        typer.echo(
            f'{tile["src"]} -> x={tile["x"]:.0f} y={tile["y"]:.0f} '
            f'w={tile["width"]:.0f} h={tile["height"]:.0f}'
        )


async def _check(headless: bool = True) -> None:
    if all(os.environ.get(name) for name in ('DISCORD_BOT_ID', 'DISCORD_BOT_TOKEN')):
        async with DiscordClient() as discord:
            report = await check_once(discord, headless=headless)
    else:
        report = await check_once(headless=headless)
    typer.echo(
        f'Checked {report.events} events: {report.new_events} new, '
        f'{report.changed_events} changed, {report.messages_sent} messages sent, '
        f'{report.messages_updated} messages updated.'
    )


@app.command()
def run(ctx: typer.Context) -> None:
    '''Start the scheduler and Discord bot.'''
    _configure_logging()
    typer.echo('Starting Theater Alert...')
    asyncio.run(serve(headless=_headless(ctx)))


@app.command()
def scrape(ctx: typer.Context) -> None:
    '''Fetch and display events from Theater Kiel.'''
    asyncio.run(_scrape(headless=_headless(ctx)))


@app.command()
def event(
    ctx: typer.Context,
    event_id: str = typer.Argument(help='The Theater Kiel event ID.'),
) -> None:
    '''Display details about a specific event.'''
    asyncio.run(_event_details(event_id, headless=_headless(ctx)))


@app.command()
def render(
    ctx: typer.Context,
    event_id: str = typer.Argument(help='The Theater Kiel event ID.'),
) -> None:
    '''Generate a seatmap image for an event.'''
    asyncio.run(_render(event_id, headless=_headless(ctx)))


@app.command()
def check(ctx: typer.Context) -> None:
    '''Trigger an immediate availability check.'''
    _configure_logging()
    asyncio.run(_check(headless=_headless(ctx)))


@app.command()
def version() -> None:
    '''Show the installed version.'''
    typer.echo(__version__)


def main() -> None:
    '''CLI entry point.'''
    app()


if __name__ == '__main__':
    main()
