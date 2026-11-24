'''Build Discord message payloads for event notifications.'''

from __future__ import annotations

from datetime import datetime
from typing import Any

from theater_alert.models import Event, Seatmap

UNSUBSCRIBE_PREFIX = 'unsubscribe:'
SUBSCRIBE_PREFIX = 'subscribe:'
STATUS_COLORS = {
    'available': 0x2ECC71,
    'sold_out': 0xE74C3C,
    'past': 0x808080,
}


def seatmap_summary(seatmap: Seatmap) -> str:
    '''Short textual overview of all price groups.'''
    if not seatmap.groups:
        return 'Keine Sitzplatzgruppen verfügbar.'
    lines = []
    for group in seatmap.groups:
        if group.available:
            lines.append(f'{group.name}: {group.price_label}')
        else:
            lines.append(f'{group.name}: {group.price_label} (ausverkauft)')
    if not seatmap.available:
        lines.append('**Keine Plätze verfügbar.**')
    return '\n'.join(lines)


def event_components(
    event: Event,
    status: str = 'available',
    subscribed: bool = True,
) -> list[dict[str, Any]]:
    '''Buttons attached to every event message.'''
    buttons: list[dict[str, Any]] = []
    if subscribed:
        buttons.append(
            {
                'type': 2,
                'style': 4, # Danger
                'label': 'Unsubscribe',
                'custom_id': f'{UNSUBSCRIBE_PREFIX}{event.production_id}',
            }
        )
    else:
        buttons.append(
            {
                'type': 2,
                'style': 1, # Primary
                'label': 'Subscribe',
                'custom_id': f'{SUBSCRIBE_PREFIX}{event.production_id}',
            }
        )
    if status != 'past':
        buttons.append(
            {
                'type': 2,
                'style': 5, # Link
                'label': 'Tickets',
                'url': event.ticket_url,
            }
        )
    return [{'type': 1, 'components': buttons}]


def event_embed(
    event: Event,
    seatmap: Seatmap,
    checked_at: datetime,
    status_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        'title': event.title,
        'description': seatmap_summary(seatmap)[:4000],
        'color': STATUS_COLORS[event.notification_status(seatmap, status_at or checked_at)],
        'fields': [
            {'name': 'Datum', 'value': event.date_label, 'inline': True},
            {'name': 'Zeit', 'value': f'{event.time} Uhr', 'inline': True},
            {'name': 'Ort', 'value': event.location, 'inline': True},
        ],
        'image': {'url': 'attachment://seatmap.png'},
        'footer': {
            'text': f'Stand: {checked_at.strftime("%d.%m.%Y %H:%M")} Uhr · Theater Kiel'
        },
    }


def build_message(
    event: Event,
    seatmap: Seatmap,
    checked_at: datetime,
    subscribed: bool = True,
    status_at: datetime | None = None,
) -> dict[str, Any]:
    '''Return the JSON payload for a new or updated event message.'''
    status_at = status_at or checked_at
    status = event.notification_status(seatmap, status_at)
    return {
        'embeds': [event_embed(event, seatmap, checked_at, status_at=status_at)],
        'components': event_components(event, status=status, subscribed=subscribed),
        'attachments': [],
        'allowed_mentions': {'parse': []},
    }
