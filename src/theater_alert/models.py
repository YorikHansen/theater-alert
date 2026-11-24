'''Data models shared by the scraper, the database and Discord messages.'''

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from theater_alert.config import BASE_URL


@dataclass(frozen=True)

class SeatGroup:
    '''A single, selectable price group of an event's seatmap.'''

    key: str
    name: str
    color: str
    price: Decimal
    available: bool = True

    @property
    def price_label(self) -> str:
        return f'{self.price:.2f} €'.replace('.', ',')

    def to_dict(self) -> dict[str, Any]:
        return {
            'key': self.key,
            'name': self.name,
            'color': self.color,
            'price': str(self.price),
            'available': self.available,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SeatGroup:
        try:
            price = Decimal(str(data['price']))
        except (InvalidOperation, KeyError, TypeError):
            price = Decimal(0)
        return cls(
            key=str(data.get('key', '')),
            name=str(data.get('name', '')),
            color=str(data.get('color', '#000000')),
            price=price,
            available=bool(data.get('available', True)),
        )


@dataclass(frozen=True)
class Seatmap:
    '''All seat groups of an event, plus an optional screenshot of the map.'''

    groups: tuple[SeatGroup, ...] = ()
    screenshot: bytes | None = field(default=None, compare=False, repr=False)

    @property
    def available(self) -> bool:
        return bool(self.groups) and any(group.available for group in self.groups)

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            [group.to_dict() for group in self.groups],
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()

    def to_json(self) -> str:
        image = base64.b64encode(self.screenshot).decode('ascii') if self.screenshot else ''
        return json.dumps(
            {'groups': [group.to_dict() for group in self.groups], 'image': image},
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> Seatmap:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return cls()
        if isinstance(data, list):
            data = {'groups': data, 'image': ''}
        if not isinstance(data, dict):
            return cls()
        groups = data.get('groups', [])
        if not isinstance(groups, list):
            groups = []
        image = data.get('image', '')
        screenshot = None
        if isinstance(image, str) and image:
            try:
                screenshot = base64.b64decode(image, validate=True)
            except (ValueError, TypeError):
                screenshot = None
        return cls(
            groups=tuple(SeatGroup.from_dict(item) for item in groups),
            screenshot=screenshot,
        )


@dataclass(frozen=True)
class Event:
    '''A single performance at Theater Kiel.'''

    id: str
    production_id: str
    title: str
    date: date
    time: str
    location: str
    url: str = ''

    @property
    def ticket_url(self) -> str:
        return f'{BASE_URL}webticket/shop?event={self.id}'

    @property
    def date_label(self) -> str:
        return self.date.strftime('%d.%m.%Y')

    @property
    def date_time_label(self) -> str:
        return f'{self.date_label} · {self.time} Uhr'
