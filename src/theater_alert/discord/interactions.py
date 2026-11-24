'''HTTP endpoint for Discord interactions (commands, autocomplete, buttons).'''

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

from theater_alert.config import now
from theater_alert.database.store import WILDCARD, Database
from theater_alert.discord.api import DiscordClient
from theater_alert.discord.messages import (
    SUBSCRIBE_PREFIX,
    UNSUBSCRIBE_PREFIX,
    build_message,
)
from theater_alert.seatmap.render import render_seatmap_image
from theater_alert.subscriptions.rules import (
    autocomplete_choices,
    format_subscriptions,
    normalize_production,
)

if TYPE_CHECKING:
    from theater_alert.alert import AlertEngine

LOGGER = logging.getLogger(__name__)
_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

SUBSCRIPTION_OPTION = {
    'name': 'production',
    'description': 'Produktions-ID oder * für alle Produktionen',
    'type': 3,
    'required': True,
    'autocomplete': True,
}

SUBSCRIBE_ALL_CUSTOM_ID = 'subscribe:all'
SUBSCRIBE_ALL_LABEL = '/subscriptions add *'
WELCOME_MESSAGE = (
    'Willkommen bei Theater Alert! Klicke auf den Button, um Benachrichtigungen '
    'für alle Produktionen zu aktivieren. Du kannst Abos später mit `/subscriptions` anpassen.'
)

COMMANDS: list[dict[str, Any]] = [
    {
        'name': 'subscriptions',
        'description': 'Verwalte deine Theater-Kiel-Benachrichtigungen',
        'options': [
            {
                'name': 'add',
                'description': 'Benachrichtigungen für eine Produktion aktivieren',
                'type': 1,
                'options': [SUBSCRIPTION_OPTION],
            },
            {
                'name': 'remove',
                'description': 'Benachrichtigungen für eine Produktion deaktivieren',
                'type': 1,
                'options': [SUBSCRIPTION_OPTION],
            },
            {
                'name': 'list',
                'description': 'Alle eigenen Abonnement-Regeln anzeigen',
                'type': 1,
            },
        ],
    },
    {
        'name': 'sudo',
        'description': 'Admin-Befehle',
        'options': [
            {
                'name': 'check',
                'description': 'Veranstaltungsliste abrufen und Prüfung starten',
                'type': 1,
            },
            {
                'name': 'redraw',
                'description': 'Alle vorhandenen Nachrichten aus dem Cache neu zeichnen',
                'type': 1,
            },
            {
                'name': 'events',
                'description': 'Zwischengespeicherte Veranstaltungen als Liste anzeigen',
                'type': 1,
            },
        ],
    },
]


def _ephemeral(content: str) -> web.Response:
    return web.json_response(
        {'type': 4, 'data': {'content': content[:4000], 'flags': 64}}
    )


def _format_cached_events(database: Database) -> str:
    events = database.cached_events()
    if not events:
        return 'Keine Veranstaltungen zwischengespeichert.'
    lines = [f'{len(events)} zwischengespeicherte Veranstaltungen:']
    for event, active, available, checked_at in events:
        state = 'aktiv' if active else 'inaktiv'
        seats = 'verfügbar' if available else 'ausverkauft'
        lines.append(
            f'- {event.id} | {event.date_label} {event.time} | {event.title} | '
            f'{event.production_id} | {state}, {seats} | '
            f'Stand {checked_at.strftime("%d.%m.%Y %H:%M")}'
        )
    return '\n'.join(lines)


async def _send_admin_followup(
    discord: DiscordClient,
    interaction_token: str,
    content: str,
) -> None:
    try:
        await discord.send_followup(
            interaction_token, {'content': content[:4000], 'flags': 64}
        )
    except Exception:
        LOGGER.exception('Could not answer the admin interaction')


def _user_id(interaction: dict[str, Any]) -> str:
    user = interaction.get('user') or interaction.get('member', {}).get('user', {})
    return str(user.get('id', ''))


def _focused_option(options: list[dict[str, Any]]) -> dict[str, Any] | None:
    for option in options:
        if option.get('focused'):
            return option
        nested = _focused_option(option.get('options', []))
        if nested is not None:
            return nested
    return None


def verify_signature(
    signature: str,
    timestamp: str,
    body: bytes,
    verify_key: str | None,
) -> bool:
    '''Verify that an interaction was really sent by Discord.'''
    if not signature or not timestamp or not verify_key:
        return False
    try:
        key = VerifyKey(bytes.fromhex(verify_key))
        key.verify(f'{timestamp}'.encode() + body, bytes.fromhex(signature))
    except (ValueError, BadSignatureError):
        return False
    return True


async def handle_interaction(request: web.Request) -> web.Response:
    database: Database = request.app['database']
    discord: DiscordClient = request.app['discord']

    body = await request.read()
    key = discord.verify_key
    if not key and len(discord.application_id) == 64:
        key = discord.application_id
    if not verify_signature(
        request.headers.get('X-Signature-Ed25519', ''),
        request.headers.get('X-Signature-Timestamp', ''),
        body,
        key,
    ):
        return web.Response(status=401, text='Invalid request signature')

    try:
        interaction = json.loads(body)
    except ValueError:
        return web.Response(status=400, text='Invalid JSON')

    interaction_type = interaction.get('type')
    if interaction_type == 0:
        return web.Response(status=204)
    if interaction_type == 1 and 'event' in interaction:
        engine = request.app.get('engine')
        return await _handle_webhook_event(interaction, database, discord, engine)
    if interaction_type == 1:
        return web.json_response({'type': 1})
    if interaction_type == 2:
        engine: AlertEngine | None = request.app.get('engine')
        return await _handle_command(interaction, database, discord, engine)
    if interaction_type == 3:
        return await _handle_component(interaction, database, discord, request.app.get('engine'))
    if interaction_type == 4:
        return _handle_autocomplete(interaction, database)
    return web.Response(status=400, text='Unknown interaction type')


async def _handle_command(
    interaction: dict[str, Any],
    database: Database,
    discord: DiscordClient,
    engine: AlertEngine | None,
) -> web.Response:
    command = interaction.get('data', {}).get('name', '')
    options = interaction.get('data', {}).get('options', [])
    subcommand = options[0].get('name') if options else ''
    user_id = _user_id(interaction)
    if not user_id:
        return _ephemeral('Dein Benutzer konnte nicht bestimmt werden.')

    if command == 'sudo':
        if getattr(discord, 'admin_id', '') != user_id:
            return _ephemeral('Dieser Befehl ist nur für Administratoren verfügbar.')
        if subcommand == 'events':
            try:
                return _ephemeral(_format_cached_events(database))
            except Exception:
                LOGGER.exception('Could not list cached events')
                return _ephemeral('Die Veranstaltungsliste konnte nicht gelesen werden.')
        if subcommand not in {'check', 'redraw'}:
            return _ephemeral('Unbekannter Befehl.')
        if engine is None:
            return _ephemeral('Administrative Befehle sind in dieser Instanz nicht verfügbar.')
        if subcommand == 'check':
            coroutine = _run_admin_check(discord, interaction['token'], engine)
        else:
            coroutine = _run_admin_redraw(discord, interaction['token'], engine)
        task = asyncio.create_task(coroutine)
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
        return web.json_response({'type': 5, 'data': {'flags': 64}})

    if command != 'subscriptions':
        return _ephemeral('Unbekannter Befehl.')

    try:
        if subcommand in {'add', 'remove'}:
            values = options[0].get('options', [])
            if not values:
                return _ephemeral('Bitte gib eine Produktions-ID an.')
            production_id = normalize_production(str(values[0].get('value', '')))
            if not production_id:
                return _ephemeral('Bitte gib eine gültige Produktions-ID an.')
            subscribed = subcommand == 'add'
            return _ephemeral(
                _set_subscription(database, discord, engine, user_id, production_id, subscribed)
            )

        if subcommand == 'list':
            return _ephemeral(format_subscriptions(database.list_subscriptions(user_id)))

        return _ephemeral('Unbekannter Befehl.')
    except Exception:
        LOGGER.exception('Could not handle /subscriptions command')
        return _ephemeral('Der Befehl konnte nicht verarbeitet werden.')


def _set_subscription(
    database: Database,
    discord: DiscordClient,
    engine: AlertEngine | None,
    user_id: str,
    production_id: str,
    subscribed: bool,
) -> str:
    database.set_subscription(user_id, production_id, subscribed)
    title = database.production_title(production_id)
    if production_id == WILDCARD:
        label = 'alle Produktionen (*)'
    elif title:
        label = f'{title} ({production_id})'
    else:
        label = f'Produktion {production_id}'
    verb = 'aktiviert' if subscribed else 'deaktiviert'
    if subscribed and engine is not None:
        task = asyncio.create_task(
            _notify_new_subscriber(discord, engine, user_id, production_id)
        )
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
    return f'Benachrichtigungen für {label}: {verb}.'


async def _run_admin_check(
    discord: DiscordClient,
    interaction_token: str,
    engine: AlertEngine,
) -> None:
    try:
        report = await engine.check()
        content = (
            'Prüfung abgeschlossen: '
            f'{report.events} Veranstaltungen, {report.new_events} neu, '
            f'{report.changed_events} geändert, {report.messages_sent} Nachrichten gesendet, '
            f'{report.messages_updated} aktualisiert.'
        )
    except Exception as error:
        LOGGER.exception('Admin triggered check failed')
        content = f'Prüfung fehlgeschlagen: {error}'
    await _send_admin_followup(discord, interaction_token, content)


async def _run_admin_redraw(
    discord: DiscordClient,
    interaction_token: str,
    engine: AlertEngine,
) -> None:
    try:
        redrawn = await engine.redraw_messages()
        content = f'Nachrichten aus dem Cache neu gezeichnet: {redrawn}.'
    except Exception as error:
        LOGGER.exception('Admin triggered redraw failed')
        content = f'Neuzeichnen fehlgeschlagen: {error}'
    await _send_admin_followup(discord, interaction_token, content)


async def _notify_new_subscriber(
    discord: DiscordClient,
    engine: AlertEngine,
    user_id: str,
    production_id: str,
) -> None:
    try:
        await engine.notify_new_subscriber(user_id, production_id)
    except Exception:
        LOGGER.exception('Could not notify new subscriber %s for %s', user_id, production_id)
        await discord.report_error(
            f'Could not send existing notifications to {user_id} for {production_id}.'
        )


async def _handle_webhook_event(
    interaction: dict[str, Any],
    database: Database,
    discord: DiscordClient,
    engine: AlertEngine | None,
) -> web.Response:
    event = interaction.get('event', {})
    event_type = event.get('type')
    if event_type not in {'APPLICATION_AUTHORIZED', 'APPLICATION_DEAUTHORIZED'}:
        return web.Response(status=204)

    user = (event.get('data', {}).get('user') or {})
    user_id = str(user.get('id', ''))
    if not user_id:
        LOGGER.warning('%s event without user id: %s', event_type, interaction)
        return web.Response(status=204)

    if event_type == 'APPLICATION_AUTHORIZED':
        task = asyncio.create_task(_send_welcome_message(database, discord, user_id))
    else:
        task = asyncio.create_task(_delete_user_data(database, user_id))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return web.Response(status=204)


async def _delete_user_data(database: Database, user_id: str) -> None:
    try:
        database.delete_user_data(user_id)
    except Exception:
        LOGGER.exception('Could not delete data of deauthorized user %s', user_id)


async def _send_welcome_message(database: Database, discord: DiscordClient, user_id: str) -> None:
    try:
        channel_id = database.get_dm_channel(user_id)
        if channel_id is None:
            channel_id = await discord.create_dm_channel(user_id)
            database.save_dm_channel(user_id, channel_id)
        await discord.send_message(
            channel_id,
            {
                'content': WELCOME_MESSAGE,
                'components': [
                    {
                        'type': 1,
                        'components': [
                            {
                                'type': 2,
                                'style': 1,
                                'label': SUBSCRIBE_ALL_LABEL,
                                'custom_id': SUBSCRIBE_ALL_CUSTOM_ID,
                            }
                        ],
                    }
                ],
                'allowed_mentions': {'parse': []},
            },
        )
    except Exception:
        LOGGER.exception('Could not send welcome message to %s', user_id)


def _handle_autocomplete(interaction: dict[str, Any], database: Database) -> web.Response:
    options = interaction.get('data', {}).get('options', [])
    focused = _focused_option(options)
    query = str(focused.get('value', '')) if focused else ''
    try:
        choices = autocomplete_choices(database, query)
    except Exception:
        LOGGER.exception('Could not build autocomplete choices')
        choices = []
    return web.json_response({'type': 8, 'data': {'choices': choices}})


async def _edit_production_messages(
    database: Database,
    discord: DiscordClient,
    production_id: str,
    user_id: str,
    subscribed: bool,
) -> None:
    for message in database.all_messages():
        if message.user_id != user_id:
            continue
        event = database.get_event(message.event_id)
        if event is None or event.production_id != production_id:
            continue
        checked_at = database.event_last_checked(event.id) or now()
        status_at = now()
        seatmap = database.event_seatmap(event.id)
        image = render_seatmap_image(event, seatmap, checked_at, status_at=status_at)
        payload = build_message(
            event, seatmap, checked_at, subscribed=subscribed, status_at=status_at
        )
        await discord.edit_message(
            message.channel_id, message.message_id, payload, image=image
        )


async def _handle_component(
    interaction: dict[str, Any],
    database: Database,
    discord: DiscordClient,
    engine: AlertEngine | None,
) -> web.Response:
    custom_id = str(interaction.get('data', {}).get('custom_id', ''))
    user_id = _user_id(interaction)
    if not user_id:
        return _ephemeral('Dein Benutzer konnte nicht bestimmt werden.')

    if custom_id == SUBSCRIBE_ALL_CUSTOM_ID:
        try:
            content = _set_subscription(database, discord, engine, user_id, WILDCARD, True)
        except Exception:
            LOGGER.exception('Could not handle the subscribe-all button')
            return _ephemeral('Das Abonnement konnte nicht gespeichert werden.')
        return web.json_response(
            {'type': 7, 'data': {'content': content, 'components': []}}
        )

    if custom_id.startswith(SUBSCRIBE_PREFIX):
        subscribed = True
    elif custom_id.startswith(UNSUBSCRIBE_PREFIX):
        subscribed = False
    else:
        return _ephemeral('Unbekannte Schaltfläche.')
    production_id = custom_id.partition(':')[2]
    if not production_id:
        return _ephemeral('Unbekannte Schaltfläche.')

    try:
        content = _set_subscription(
            database, discord, engine, user_id, production_id, subscribed
        )
        await _edit_production_messages(
            database, discord, production_id, user_id, subscribed
        )
    except Exception:
        LOGGER.exception('Could not handle an event message button')
        return _ephemeral('Die Abonnement-Änderung konnte nicht gespeichert werden.')

    return _ephemeral(content)


async def health(request: web.Request) -> web.Response:
    return web.json_response({'status': 'ok'})


def create_app(
    database: Database,
    discord: DiscordClient,
    engine: AlertEngine | None = None,
) -> web.Application:
    '''Create the aiohttp application serving Discord interactions.'''
    app = web.Application()
    app['database'] = database
    app['discord'] = discord
    app['engine'] = engine
    app.router.add_post('/interactions', handle_interaction)
    app.router.add_get('/health', health)
    return app
