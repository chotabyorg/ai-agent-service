"""Bounded, request-local reasoning continuity for managed tool messages only."""

import json
import math
from types import SimpleNamespace

from services.managed_tools import ManagedExecutionError


def _copy_bounded_json(value):
    # Match managed JSON limits, charging before descent/copy. Provider blocks
    # are opaque: neither their keys nor their type values confer authority.
    nodes = 0
    size = 0
    active = set()

    def charge(amount):
        nonlocal size
        size += amount
        if size > 131072:
            raise ManagedExecutionError("CONTEXT_LIMIT")

    def copy(item, depth):
        nonlocal nodes
        nodes += 1
        if depth > 32 or nodes > 4096:
            raise ManagedExecutionError("CONTEXT_LIMIT")
        kind = type(item)
        if kind in (dict, list, SimpleNamespace):
            stored = vars(item) if kind is SimpleNamespace else item
            if len(stored) > 4096:
                raise ManagedExecutionError("CONTEXT_LIMIT")
            if id(item) in active:
                raise ManagedExecutionError("INVALID_RESULT")
            active.add(id(item))
            charge(2 + max(0, len(stored) - 1) * 2)
            try:
                if kind is list:
                    return [copy(child, depth + 1) for child in stored]
                result = {}
                for key, child in stored.items():
                    if type(key) is not str:
                        raise ManagedExecutionError("INVALID_RESULT")
                    charge(2)
                    result[copy(key, depth + 1)] = copy(child, depth + 1)
                return result
            finally:
                active.remove(id(item))
        if kind is str:
            if len(item) > 131072:
                raise ManagedExecutionError("CONTEXT_LIMIT")
            if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                raise ManagedExecutionError("INVALID_RESULT")
        elif kind is float:
            if not math.isfinite(item):
                raise ManagedExecutionError("INVALID_RESULT")
        elif kind is int:
            if item.bit_length() > 131072 * 4:
                raise ManagedExecutionError("CONTEXT_LIMIT")
        elif item is not None and kind is not bool:
            raise ManagedExecutionError("INVALID_RESULT")
        try:
            charge(len(json.dumps(item, ensure_ascii=False, allow_nan=False).encode("utf-8")))
        except (ValueError, OverflowError, UnicodeError):
            raise ManagedExecutionError("INVALID_RESULT") from None
        return item

    return copy(value, 0)


def managed_reasoning_fields(message):
    """Select one provider representation, validating only the selected data."""
    details = getattr(message, "reasoning_details", None)
    if details is not None:
        if type(details) is not list:
            raise ManagedExecutionError("INVALID_RESULT")
        if len(details) > 4096:
            raise ManagedExecutionError("CONTEXT_LIMIT")
        if details:
            if any(type(block) not in (dict, SimpleNamespace) for block in details):
                raise ManagedExecutionError("INVALID_RESULT")
            return {"reasoning_details": _copy_bounded_json(details)}
    for name in ("reasoning", "reasoning_content"):
        value = getattr(message, name, None)
        if value is not None:
            if type(value) is not str:
                raise ManagedExecutionError("INVALID_RESULT")
            return {name: _copy_bounded_json(value)}
    return {}
