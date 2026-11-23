'''Extract events and seat groups from the webshop's HTML.'''

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html import unescape
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from theater_alert.config import BASE_URL, EVENT_WINDOW_DAYS
from theater_alert.models import Event, SeatGroup, Seatmap

DATE_PATTERN = re.compile(r'(\d{2}\.\d{2}\.\d{4})')
TIME_PATTERN = re.compile(r'(\d{1,2}:\d{2})')
PRICE_PATTERN = re.compile(r'(\d+(?:[.,]\d{1,2})?)')
COLOR_PATTERN = re.compile(r'background-color\s*:\s*([^;]+)', re.IGNORECASE)


def _text(node: Any) -> str:
    if node is None:
        return ''
    return node.get_text(' ', strip=True).strip()


def _classes(node: Any) -> set[str]:
    value = node.get('class')
    if value is None:
        return set()
    if isinstance(value, str):
        return set(value.split())
    return {str(item) for item in value}


def events_in_window(
    events: list[Event],
    today: date | None = None,
    window_days: int | None = EVENT_WINDOW_DAYS,
) -> list[Event]:
    '''Filter parsed events by the notification window; None keeps everything.'''
    if window_days is None:
        return list(events)
    today = today or date.today()
    last_day = today + timedelta(days=window_days)
    return [event for event in events if today <= event.date <= last_day]


def parse_events(
    html: str,
    today: date | None = None,
    window_days: int | None = EVENT_WINDOW_DAYS,
) -> list[Event]:
    '''Return all bookable events, optionally limited to the notification window.'''
    soup = BeautifulSoup(html, 'html.parser')
    today = today or date.today()
    last_day = today + timedelta(days=window_days) if window_days is not None else None
    events: list[Event] = []

    for container in soup.select('[id^="eventKey:"]'):
        event_id = str(container.get('id') or '').partition(':')[2].strip()
        if not event_id:
            continue

        title_node = container.find(id=f'event-title-{event_id}')
        date_node = container.find(id=f'event-date-{event_id}')
        time_node = container.find(id=f'event-time-{event_id}')
        location_node = container.find(id=f'event-location-{event_id}')
        data_node = container.find(id=f'eventData:{event_id}')
        if data_node is None:
            data_node = container.select_one('[data-event-production]')

        link_node = container.find('a', id=f'buttontext:{event_id}') or container.find(
            'a', href=re.compile(r'eventId=')
        )
        href = str(link_node.get('href') or '').strip() if link_node else ''

        date_match = DATE_PATTERN.search(_text(date_node))
        time_match = TIME_PATTERN.search(_text(time_node))
        production_id = (
            str(data_node.get('data-event-production') or '').strip() if data_node else ''
        )

        if not (event_id and title_node and date_match and time_match and production_id and href):
            continue

        try:
            event_date = datetime.strptime(date_match.group(1), '%d.%m.%Y').date()
        except ValueError:
            continue

        if last_day is not None and not today <= event_date <= last_day:
            continue

        events.append(
            Event(
                id=event_id,
                production_id=production_id,
                title=_text(title_node),
                date=event_date,
                time=time_match.group(1),
                location=_text(location_node),
                url=urljoin(BASE_URL, unescape(href)),
            )
        )

    return sorted(events, key=lambda event: (event.date, event.time, event.id))


def _parse_price(raw: str) -> Decimal:
    match = PRICE_PATTERN.search(raw)
    if not match:
        return Decimal(0)
    try:
        return Decimal(match.group(1).replace(',', '.'))
    except InvalidOperation:
        return Decimal(0)


def parse_seatmap(html: str) -> Seatmap:
    '''Return the seat groups shown in the webshop's seatgroup dropdown.'''
    soup = BeautifulSoup(html, 'html.parser')
    groups: dict[str, SeatGroup] = {}

    for index, option in enumerate(soup.select('li[data-original-value]')):
        key = str(option.get('data-original-value') or '').strip() or str(index)
        name_node = option.select_one('.inh-SeatGroupDowndown-NameInOptionLine')
        color_node = option.select_one('.circle')
        price_node = option.select_one('.inh-SeatGroupDowndown-MoneyInOptionLine')
        if name_node is None:
            continue

        name = _text(name_node)
        if not name:
            continue

        color_match = COLOR_PATTERN.search(str(color_node.get('style') or '')) if color_node else None
        color = color_match.group(1).strip() if color_match else '#000000'
        if color.lower() in {'none', 'transparent', ''}:
            color = '#000000'

        disabled = (
            'disabled' in _classes(option)
            or name_node.find('s') is not None
            or any(
                'disabled' in _classes(parent)
                for parent in option.parents
                if parent.name in {'span', 'a'}
            )
        )

        groups[key] = SeatGroup(
            key=key,
            name=name,
            color=color,
            price=_parse_price(_text(price_node)),
            available=not disabled,
        )

    return Seatmap(groups=tuple(groups.values()))
