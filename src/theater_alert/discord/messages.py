'''Build Discord message payloads for event notifications.'''

from __future__ import annotations

from datetime import datetime
from typing import Any

from theater_alert.models import Event, Seatmap

UNSUBSCRIBE_PREFIX = 'unsubscribe:'


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


def event_components(event: Event, include_unsubscribe: bool = True) -> list[dict[str, Any]]:
    '''Buttons attached to every event message.'''
    buttons: list[dict[str, Any]] = []
    if include_unsubscribe:
        buttons.append(
            {
                'type': 2,
                'style': 4, # Danger
                'label': 'Unsubscribe',
                'custom_id': f'{UNSUBSCRIBE_PREFIX}{event.id}',
            }
        )
    buttons.append(
        {
            'type': 2,
            'style': 5, # Link
            'label': 'Tickets',
            'url': event.ticket_url,
        }
    )
    return [{'type': 1, 'components': buttons}]


def event_embed(event: Event, seatmap: Seatmap, checked_at: datetime) -> dict[str, Any]:
    return {
        'title': event.title,
        'description': seatmap_summary(seatmap)[:4000],
        'color': 0x2ECC71 if seatmap.available else 0xE74C3C, # TODO: change color to grey if in past
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


def build_message( # TODO: instead of embed, use block component, where buttons may be directly included
    event: Event,
    seatmap: Seatmap,
    checked_at: datetime,
    include_unsubscribe: bool = True,
) -> dict[str, Any]:
    '''Return the JSON payload for a new or updated event message.'''
    return {
        'embeds': [event_embed(event, seatmap, checked_at)],
        'components': event_components(event, include_unsubscribe),
        'attachments': [],
        'allowed_mentions': {'parse': []},
    }
