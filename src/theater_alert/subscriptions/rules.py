'''Resolve which users should be notified about which productions.'''

from __future__ import annotations

from collections.abc import Mapping, Sequence

from theater_alert.database.store import WILDCARD, Database, Subscription

MAX_CHOICES = 25


def is_subscribed(rules: Mapping[str, bool], production_id: str) -> bool:
    '''A specific rule wins over the wildcard rule.'''
    if production_id in rules:
        return rules[production_id]
    return rules.get(WILDCARD, False)


def subscribed_users(
    rules_by_user: Mapping[str, Mapping[str, bool]],
    production_id: str,
) -> list[str]:
    '''Return all users that actively subscribed to the given production.'''
    return sorted(
        user_id
        for user_id, rules in rules_by_user.items()
        if is_subscribed(rules, production_id)
    )


def format_subscriptions(subscriptions: Sequence[Subscription]) -> str:
    '''Render a user's rules for the `/subscriptions list` response.'''
    if not subscriptions:
        return 'Du hast noch keine Abonnement-Regeln. Nutze `/subscriptions add`.'

    lines = []
    for subscription in subscriptions:
        if subscription.production_id == WILDCARD:
            name = 'alle Produktionen (*)'
        elif subscription.title:
            name = f'{subscription.title} ({subscription.production_id})'
        else:
            name = f'Produktion {subscription.production_id}'
        state = 'aktiv' if subscription.subscribed else 'inaktiv'
        lines.append(f'- {name}: {state}')
    return '\n'.join(lines)


def autocomplete_choices(database: Database, query: str) -> list[dict[str, str]]:
    '''Return production ids as values and their titles as displayed names.'''
    needle = query.strip().casefold()
    choices: list[dict[str, str]] = [
        {'name': 'Alle Produktionen (*)', 'value': WILDCARD}
    ]

    for production_id, title in database.production_names():
        name = f'{title} ({production_id})' if title else f'Produktion {production_id}'
        if needle and needle not in name.casefold() and needle not in production_id.casefold():
            continue
        choices.append({'name': name[:100], 'value': production_id})
        if len(choices) >= MAX_CHOICES:
            break

    return choices


def normalize_production(value: str) -> str:
    '''Normalize the user supplied production argument.'''
    value = value.strip()
    return WILDCARD if value in {'*', 'all', 'alle'} else value
