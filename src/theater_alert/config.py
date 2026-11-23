'''Configuration and constants for Theater Alert.'''

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TIMEZONE = ZoneInfo('Europe/Berlin')

EVENT_WINDOW_DAYS = 3
CHECK_INTERVAL_HOURS = 3

BASE_URL = 'https://theater-kiel.eventim-inhouse.de/webshop/'

DATA_DIR = Path(__file__).resolve().parent.parent.parent / 'data'
DATABASE_PATH = DATA_DIR / 'theater-alert.sqlite3'

DISCORD_API_BASE = 'https://discord.com/api/v10'
DISCORD_INTERACTIONS_PATH = '/interactions'
DISCORD_INTERACTIONS_PORT = 8080
DISCORD_INTERACTIONS_HOST = '0.0.0.0'


def now() -> datetime:
    '''Return the current time in the theater's timezone.'''
    return datetime.now(TIMEZONE)


def env(name: str) -> str:
    '''Read a required environment variable.'''
    value = os.environ.get(name, '').strip()
    if not value:
        raise RuntimeError(f'Missing required environment variable {name}.')
    return value


def discord_bot_id() -> str:
    return env('DISCORD_BOT_ID')


def discord_bot_token() -> str:
    return env('DISCORD_BOT_TOKEN')


def discord_bot_admin() -> str:
    return env('DISCORD_BOT_ADMIN')
