"""Closed command-field validation shared by domain handlers."""

from .errors import RuleViolation


def fields(order: dict, required: dict, optional: dict | None = None) -> None:
    optional = optional or {}
    mandatory = {'id', 'kind', 'op'} | required.keys()
    if not mandatory <= order.keys() or order.keys() - mandatory - optional.keys():
        raise RuleViolation('ERR_SCHEMA', 'missing or unknown field')
    for key, expected in (required | optional).items():
        types = expected if isinstance(expected, tuple) else (expected,)
        if key in order and type(order[key]) not in types:
            raise RuleViolation('ERR_SCHEMA', f'invalid {key}')


def version(order: dict, asset) -> None:
    if 'expected_version' in order:
        value = order['expected_version']
        if type(value) is not int or value < 0:
            raise RuleViolation('ERR_SCHEMA')
        if value != asset.version:
            raise RuleViolation('ERR_STALE_VERSION')
