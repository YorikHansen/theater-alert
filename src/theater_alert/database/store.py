'''Persistent state: events, sent messages, DM channels and subscriptions.'''

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from theater_alert.config import DATABASE_PATH
from theater_alert.models import Event, Seatmap

WILDCARD = '*'


@dataclass(frozen=True)
class EventState:
    '''What changed during the last check of an event.'''

    is_new: bool
    changed: bool


@dataclass(frozen=True)
class MessageRef:
    '''A Discord message already sent for an event.'''

    event_id: str
    user_id: str
    channel_id: str
    message_id: str


@dataclass(frozen=True)
class Subscription:
    '''A user's explicit rule for one production (or the wildcard).'''

    production_id: str
    subscribed: bool
    title: str = ''


class Database:
    '''Small SQLite backed store, shared by the bot and the scheduler.'''

    def __init__(self, path: Path = DATABASE_PATH) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None

    @contextmanager
    def _cursor(self) -> Generator[sqlite3.Cursor]:
        with self._lock:
            if self._connection is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._connection = sqlite3.connect(self.path, check_same_thread=False)
                self._connection.row_factory = sqlite3.Row
            cursor = self._connection.cursor()
            try:
                yield cursor
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise
            finally:
                cursor.close()

    def setup(self) -> None:
        '''Create all tables if they do not exist yet.'''
        with self._cursor() as cursor:
            cursor.executescript(
                '''
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    production_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    event_date TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    location TEXT NOT NULL,
                    url TEXT NOT NULL DEFAULT '',
                    seatmap TEXT NOT NULL,
                    seatmap_hash TEXT NOT NULL,
                    available INTEGER NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_checked TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS messages (
                    event_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    PRIMARY KEY (event_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS dm_channels (
                    user_id TEXT PRIMARY KEY,
                    channel_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    user_id TEXT NOT NULL,
                    production_id TEXT NOT NULL,
                    subscribed INTEGER NOT NULL,
                    PRIMARY KEY (user_id, production_id)
                );
                CREATE TABLE IF NOT EXISTS production_names (
                    production_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL
                );
                '''
            )
            columns = {row['name'] for row in cursor.execute('PRAGMA table_info(events)')}
            if 'url' not in columns:
                cursor.execute("ALTER TABLE events ADD COLUMN url TEXT NOT NULL DEFAULT ''")

    def save_event(self, event: Event, seatmap: Seatmap, checked_at: datetime) -> EventState:
        '''Store the current state of an event and report new/changed data.'''
        fingerprint = seatmap.fingerprint
        timestamp = checked_at.isoformat()
        with self._cursor() as cursor:
            row = cursor.execute(
                '''
                SELECT active, seatmap_hash, production_id, title, event_date, event_time, location, url
                FROM events WHERE event_id = ?
                ''',
                (event.id,),
            ).fetchone()
            is_new = row is None or not bool(row['active'])
            info_changed = row is not None and (
                row['production_id'] != event.production_id
                or row['title'] != event.title
                or row['event_date'] != event.date.isoformat()
                or row['event_time'] != event.time
                or row['location'] != event.location
                or row['url'] != event.url
            )
            changed = bool(
                row is not None
                and not is_new
                and (row['seatmap_hash'] != fingerprint or info_changed)
            )

            cursor.execute(
                '''
                INSERT INTO events (
                    event_id, production_id, title, event_date, event_time, location, url,
                    seatmap, seatmap_hash, available, first_seen, last_checked, active
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(event_id) DO UPDATE SET
                    production_id = excluded.production_id,
                    title = excluded.title,
                    event_date = excluded.event_date,
                    event_time = excluded.event_time,
                    location = excluded.location,
                    url = excluded.url,
                    seatmap = excluded.seatmap,
                    seatmap_hash = excluded.seatmap_hash,
                    available = excluded.available,
                    last_checked = excluded.last_checked,
                    active = 1
                ''',
                (
                    event.id,
                    event.production_id,
                    event.title,
                    event.date.isoformat(),
                    event.time,
                    event.location,
                    event.url,
                    seatmap.to_json(),
                    fingerprint,
                    int(seatmap.available),
                    timestamp,
                    timestamp,
                ),
            )
            cursor.execute(
                '''
                INSERT INTO production_names (production_id, title) VALUES (?, ?)
                ON CONFLICT(production_id) DO UPDATE SET title = excluded.title
                ''',
                (event.production_id, event.title),
            )

        return EventState(is_new=is_new, changed=changed)

    def save_production_names(self, events: Sequence[Event]) -> None:
        '''Keep names from the full event list available for autocomplete.'''
        if not events:
            return
        with self._cursor() as cursor:
            cursor.executemany(
                '''
                INSERT INTO production_names (production_id, title) VALUES (?, ?)
                ON CONFLICT(production_id) DO UPDATE SET title = excluded.title
                ''',
                [(event.production_id, event.title) for event in events],
            )

    def mark_inactive_except(self, event_ids: set[str]) -> None:
        '''Events outside the window are no longer considered active.'''
        with self._cursor() as cursor:
            if event_ids:
                placeholders = ', '.join('?' for _ in event_ids)
                cursor.execute(
                    f'UPDATE events SET active = 0 WHERE active = 1 AND event_id NOT IN ({placeholders})',
                    tuple(event_ids),
                )
            else:
                cursor.execute('UPDATE events SET active = 0 WHERE active = 1')

    def get_event(self, event_id: str) -> Event | None:
        with self._cursor() as cursor:
            row = cursor.execute('SELECT * FROM events WHERE event_id = ?', (event_id,)).fetchone()
        return self._event_from_row(row) if row else None

    def event_seatmap(self, event_id: str) -> Seatmap:
        with self._cursor() as cursor:
            row = cursor.execute('SELECT seatmap FROM events WHERE event_id = ?', (event_id,)).fetchone()
        return Seatmap.from_json(row['seatmap']) if row else Seatmap()

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> Event:
        return Event(
            id=row['event_id'],
            production_id=row['production_id'],
            title=row['title'],
            date=datetime.strptime(row['event_date'], '%Y-%m-%d').date(),
            time=row['event_time'],
            location=row['location'],
            url=row['url'],
        )

    def get_messages(self, event_id: str) -> list[MessageRef]:
        with self._cursor() as cursor:
            rows = cursor.execute(
                'SELECT * FROM messages WHERE event_id = ?', (event_id,)
            ).fetchall()
        return [
            MessageRef(
                event_id=row['event_id'],
                user_id=row['user_id'],
                channel_id=row['channel_id'],
                message_id=row['message_id'],
            )
            for row in rows
        ]

    def save_message(self, event_id: str, user_id: str, channel_id: str, message_id: str) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                '''
                INSERT INTO messages (event_id, user_id, channel_id, message_id)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(event_id, user_id) DO UPDATE SET
                    channel_id = excluded.channel_id,
                    message_id = excluded.message_id
                ''',
                (event_id, user_id, channel_id, message_id),
            )

    def delete_message(self, event_id: str, user_id: str) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                'DELETE FROM messages WHERE event_id = ? AND user_id = ?',
                (event_id, user_id),
            )

    def get_dm_channel(self, user_id: str) -> str | None:
        with self._cursor() as cursor:
            row = cursor.execute(
                'SELECT channel_id FROM dm_channels WHERE user_id = ?', (user_id,)
            ).fetchone()
        return str(row['channel_id']) if row else None

    def save_dm_channel(self, user_id: str, channel_id: str) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                '''
                INSERT INTO dm_channels (user_id, channel_id) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET channel_id = excluded.channel_id
                ''',
                (user_id, channel_id),
            )

    def set_subscription(self, user_id: str, production_id: str, subscribed: bool) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                '''
                INSERT INTO subscriptions (user_id, production_id, subscribed) VALUES (?, ?, ?)
                ON CONFLICT(user_id, production_id) DO UPDATE SET subscribed = excluded.subscribed
                ''',
                (user_id, production_id, int(subscribed)),
            )

    def clear_subscription(self, user_id: str, production_id: str) -> None:
        with self._cursor() as cursor:
            cursor.execute(
                'DELETE FROM subscriptions WHERE user_id = ? AND production_id = ?',
                (user_id, production_id),
            )

    def list_subscriptions(self, user_id: str) -> list[Subscription]:
        with self._cursor() as cursor:
            rows = cursor.execute(
                '''
                SELECT s.production_id, s.subscribed, COALESCE(p.title, '') AS title
                FROM subscriptions s
                LEFT JOIN production_names p ON p.production_id = s.production_id
                WHERE s.user_id = ?
                ORDER BY s.production_id
                ''',
                (user_id,),
            ).fetchall()
        return [
            Subscription(
                production_id=row['production_id'],
                subscribed=bool(row['subscribed']),
                title=row['title'],
            )
            for row in rows
        ]

    def all_rules(self) -> dict[str, dict[str, bool]]:
        with self._cursor() as cursor:
            rows = cursor.execute(
                'SELECT user_id, production_id, subscribed FROM subscriptions'
            ).fetchall()
        rules: dict[str, dict[str, bool]] = {}
        for row in rows:
            rules.setdefault(row['user_id'], {})[row['production_id']] = bool(row['subscribed'])
        return rules

    def production_names(self) -> list[tuple[str, str]]:
        with self._cursor() as cursor:
            rows = cursor.execute(
                'SELECT production_id, title FROM production_names ORDER BY title'
            ).fetchall()
        return [(row['production_id'], row['title']) for row in rows]

    def production_title(self, production_id: str) -> str:
        with self._cursor() as cursor:
            row = cursor.execute(
                'SELECT title FROM production_names WHERE production_id = ?',
                (production_id,),
            ).fetchone()
        return str(row['title']) if row else ''

    def active_available_events(self, production_id: str | None = None) -> list[Event]:
        '''Return active events with available seats, optionally for one production.'''
        query = 'SELECT * FROM events WHERE active = 1 AND available = 1'
        params: tuple[str, ...] = ()
        if production_id is not None:
            query += ' AND production_id = ?'
            params = (production_id,)
        query += ' ORDER BY event_date, event_time, event_id'
        with self._cursor() as cursor:
            rows = cursor.execute(query, params).fetchall()
        return [self._event_from_row(row) for row in rows]


__all__ = ['WILDCARD', 'Database', 'EventState', 'MessageRef', 'Subscription']
