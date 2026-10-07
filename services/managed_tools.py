"""Bounded managed-only inspection of current MCP adapter error envelopes."""

import json
import math


class ManagedExecutionError(Exception):
    def __init__(self, code: str, *, processing_status=None):
        self.code = code
        # This is processing evidence, never a BusinessOperation transition.
        # A scope/predecessor block is UNKNOWN even without a provider attempt.
        self.processing_status = processing_status
        super().__init__(code)


def parse_managed_tool_arguments(text: str) -> dict:
    """Closed managed input; legacy keeps its existing permissive parser."""
    try:
        if not isinstance(text, str) or len(text.encode("utf-8")) > 131072:
            raise ValueError
        value = json.loads(text, object_pairs_hook=_object_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        if not isinstance(value, dict):
            raise ValueError
        _bounded_json(value)
        return value
    except ManagedExecutionError:
        raise
    except Exception:
        raise ManagedExecutionError("TOOL_SETUP_FAILED") from None


def _bounded_json(value):
    pending = [(value, 0)]
    nodes = 0
    seen = set()
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 32 or nodes > 4096:
            raise ManagedExecutionError("CONTEXT_LIMIT")
        if isinstance(item, (dict, list)):
            if len(item) > 4096:
                raise ManagedExecutionError("CONTEXT_LIMIT")
            if id(item) in seen:
                raise ManagedExecutionError("INVALID_RESULT")
            seen.add(id(item))
            if isinstance(item, dict):
                if not all(isinstance(key, str) for key in item):
                    raise ManagedExecutionError("INVALID_RESULT")
                pending.extend((key, depth + 1) for key in item)
                pending.extend((child, depth + 1) for child in item.values())
            else:
                pending.extend((child, depth + 1) for child in item)
        elif isinstance(item, str):
            if any(0xD800 <= ord(c) <= 0xDFFF for c in item):
                raise ManagedExecutionError("INVALID_RESULT")
            if len(item.encode("utf-8")) > 131072:
                raise ManagedExecutionError("CONTEXT_LIMIT")
        elif item is None or isinstance(item, (bool, int)):
            pass
        elif isinstance(item, float) and math.isfinite(item):
            pass
        else:
            raise ManagedExecutionError("INVALID_RESULT")
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, OverflowError, RecursionError, UnicodeError):
        raise ManagedExecutionError("INVALID_RESULT") from None
    if len(encoded) > 131072:
        raise ManagedExecutionError("CONTEXT_LIMIT")
    return len(encoded)


def _object_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError
        value[key] = item
    return value


def _parse_text(text):
    try:
        value = json.loads(text, object_pairs_hook=_object_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError, OverflowError):
        # Plain text is a documented opaque-success limitation. A JSON envelope
        # with broken syntax/duplicate keys/nonfinite data is never forwarded.
        if text.lstrip().startswith(("{", "[")) or text.strip() in {"NaN", "Infinity", "-Infinity"}:
            raise ManagedExecutionError("INVALID_RESULT") from None
        return None
    _bounded_json(value)
    return value


def _application_error(service, value):
    if isinstance(value, dict) and set(value) == {"result"}:
        value = value["result"]
    if service == "bitrix":
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        return isinstance(value, dict) and "error" in value
    if service == "google_workspace" and isinstance(value, dict) and value.get("ok") is False:
        if "error" not in value:
            raise ManagedExecutionError("INVALID_RESULT")
        return True
    return False


def inspect_managed_tool_result(service: str, result: object) -> None:
    """Run before any extraction, append, subsequent tool or model invocation."""
    if getattr(result, "isError", False):
        raise ManagedExecutionError("TOOL_EXECUTION_UNKNOWN")
    content = getattr(result, "content", None) or []
    if not isinstance(content, (list, tuple)):
        raise ManagedExecutionError("INVALID_RESULT")
    if len(content) > 32:
        raise ManagedExecutionError("CONTEXT_LIMIT")
    structured = getattr(result, "structuredContent", None)
    total = _bounded_json(structured) if structured is not None else 0
    values = [structured] if structured is not None else []
    for block in content:
        text = getattr(block, "text", None)
        if text is None:
            continue
        if not isinstance(text, str):
            raise ManagedExecutionError("INVALID_RESULT")
        try:
            total += len(text.encode("utf-8"))
        except UnicodeError:
            raise ManagedExecutionError("INVALID_RESULT") from None
        if total > 131072:
            raise ManagedExecutionError("CONTEXT_LIMIT")
        values.append(_parse_text(text))
    if any(_application_error(service, value) for value in values):
        raise ManagedExecutionError("TOOL_EXECUTION_UNKNOWN")
