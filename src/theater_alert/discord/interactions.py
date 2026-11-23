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
from theater_alert.database.store import Database
from theater_alert.discord.api import DiscordClient
from theater_alert.discord.messages import UNSUBSCRIBE_PREFIX, build_message
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
        'name': 'check',
        'description': 'Admin: Veranstaltungsliste abrufen und Prüfung starten',
    },
]


def _ephemeral(content: str) -> web.Response:
    return web.json_response(
        {'type': 4, 'data': {'content': content[:4000], 'flags': 64}}
    )


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
    if interaction_type == 1:
        return web.json_response({'type': 1})
    if interaction_type == 2:
        engine: AlertEngine | None = request.app.get('engine')
        return await _handle_command(interaction, database, discord, engine)
    if interaction_type == 3:
        return await _handle_component(interaction, database, discord)
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

    if command == 'check':
        if getattr(discord, 'admin_id', '') != user_id:
            return _ephemeral('Dieser Befehl ist nur für Administratoren verfügbar.')
        if engine is None:
            return _ephemeral('Die Prüfung ist in dieser Instanz nicht verfügbar.')
        task = asyncio.create_task(_run_admin_check(discord, interaction['token'], engine))
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
            database.set_subscription(user_id, production_id, subscribed)
            title = database.production_title(production_id)
            if production_id == '*':
                label = 'alle Produktionen (*)'
            elif title:
                label = f'{title} ({production_id})'
            else:
                label = f'Produktion {production_id}'
            verb = 'aktiviert' if subscribed else 'deaktiviert'
            return _ephemeral(f'Benachrichtigungen für {label}: {verb}.')

        if subcommand == 'list':
            return _ephemeral(format_subscriptions(database.list_subscriptions(user_id)))

        return _ephemeral('Unbekannter Befehl.')
    except Exception:
        LOGGER.exception('Could not handle /subscriptions command')
        return _ephemeral('Der Befehl konnte nicht verarbeitet werden.')


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
    try:
        await discord.send_followup(interaction_token, {'content': content[:4000], 'flags': 64})
    except Exception:
        LOGGER.exception('Could not answer the admin check interaction')


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


async def _handle_component(
    interaction: dict[str, Any],
    database: Database,
    discord: DiscordClient,
) -> web.Response:
    custom_id = str(interaction.get('data', {}).get('custom_id', ''))
    user_id = _user_id(interaction)

    if not custom_id.startswith(UNSUBSCRIBE_PREFIX):
        return _ephemeral('Unbekannte Schaltfläche.')
    event_id = custom_id.partition(':')[2]
    event = database.get_event(event_id)
    if event is None:
        return _ephemeral('Diese Veranstaltung ist nicht mehr bekannt.')

    try:
        database.set_subscription(user_id, event.production_id, False)
        for message in database.get_messages(event_id):
            if message.user_id == user_id:
                checked_at = now()
                seatmap = database.event_seatmap(event_id)
                image = render_seatmap_image(event, seatmap, checked_at)
                payload = build_message(
                    event, seatmap, checked_at, include_unsubscribe=False
                )
                await discord.edit_message(
                    message.channel_id, message.message_id, payload, image=image
                )
                break
    except Exception:
        LOGGER.exception('Could not unsubscribe from a message button')
        return _ephemeral('Die Abmeldung konnte nicht gespeichert werden.')

    return _ephemeral(
        f'Du wirst für "{event.title}" (Produktion {event.production_id}) '
        'nicht mehr benachrichtigt.'
    )


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
