"""Bounded optional accounting; never inspect or retain provider content."""
from decimal import Decimal, ROUND_HALF_UP
import math
from types import SimpleNamespace

from openai.types.completion_usage import (
    CompletionTokensDetails, CompletionUsage, PromptTokensDetails,
)

_TYPES = (SimpleNamespace, CompletionUsage, CompletionTokensDetails, PromptTokensDetails)
_MAX_COUNT = 2147483647
_MAX_COST = 1_000_000_000


def usage_field(value, name):
    """Read stored data only, including extra OpenRouter SDK usage fields."""
    if type(value) is dict:
        return value.get(name)
    if type(value) not in _TYPES:
        return None
    stored = object.__getattribute__(value, '__dict__')
    if name in stored:
        return stored[name]
    if type(value) is not SimpleNamespace:
        try:
            extra = object.__getattribute__(value, '__pydantic_extra__')
        except AttributeError:
            return None
        if type(extra) is dict:
            return extra.get(name)
    return None


def bounded_cost(value):
    # Ignore absent, hostile and nonsensical costs; absence is never zero cost.
    if type(value) not in (int, float):
        return None
    if not 0 <= value <= _MAX_COST or (type(value) is float and not math.isfinite(value)):
        return None
    return value


def accounting_metrics(usage):
    out = {}
    for name in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
        value = usage_field(usage, name)
        if type(value) is int and 0 <= value <= _MAX_COUNT:
            out[name] = value
    for field, names in (
        ('completion_tokens_details', ('reasoning_tokens',)),
        ('prompt_tokens_details', ('cached_tokens', 'cache_write_tokens')),
    ):
        details = usage_field(usage, field)
        for name in names:
            value = usage_field(details, name)
            if type(value) is int and 0 <= value <= _MAX_COUNT:
                out[name] = value
    cost = bounded_cost(usage_field(usage, 'cost'))
    if cost is not None:
        out['cost_nano_credits'] = int(
            (Decimal(str(cost)) * 1_000_000_000).to_integral_value(rounding=ROUND_HALF_UP))
    return out
