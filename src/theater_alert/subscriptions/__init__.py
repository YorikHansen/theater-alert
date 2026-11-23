'''Knows which events match which subscriptions.'''

from theater_alert.database.store import WILDCARD
from theater_alert.subscriptions.rules import (
    autocomplete_choices,
    format_subscriptions,
    is_subscribed,
    normalize_production,
    subscribed_users,
)

__all__ = [
    'WILDCARD',
    'autocomplete_choices',
    'format_subscriptions',
    'is_subscribed',
    'normalize_production',
    'subscribed_users',
]
