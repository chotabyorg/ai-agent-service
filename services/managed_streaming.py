"""Opt-in streaming text assembly for managed completions.

The default managed path stays fully buffered. stream_completion() asks the
completions endpoint for server-sent events and forwards incremental text to
an async callback while still assembling the complete result for the existing
validation. A tool-call delta stops partial emission; the buffered remainder
preserves the full-result contract. A non-streaming answer degrades to the
buffered path without partials. Nothing here grants authority, persists,
retries, falls back to another model, or logs content.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any

# Bounded assembly; mirrors the managed result budget without importing it.
MAX_STREAM_CHARS = 8000
MAX_STREAM_CHUNKS = 4096


class StreamLimitExceeded(ValueError):
    """A streamed completion exceeded its assembly bounds."""


def _get(value: Any, name: str) -> Any:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _choice(item: Any) -> Any:
    choices = _get(item, "choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("Streaming completion chunk has no choices")
    return choices[0]


def _parse_sse_line(line: str) -> Any | None:
    import json as _json

    if not line.startswith("data:"):
        return None
    payload = line[len("data:") :].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        parsed = _json.loads(payload)
    except ValueError as exc:
        raise ValueError("Streaming completion chunk is not JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("Streaming completion chunk is not an object")
    return parsed


def _delta_text(delta: Any) -> str | None:
    if delta is None:
        return None
    content = _get(delta, "content")
    if content is None:
        return None
    if not isinstance(content, str):
        raise ValueError("Streaming text delta is not a string")
    return content


def _delta_tool_calls(delta: Any) -> list[Any]:
    if delta is None:
        return []
    calls = _get(delta, "tool_calls")
    if calls is None:
        return []
    if not isinstance(calls, list):
        raise ValueError("Streaming tool deltas are not a list")
    return calls


def _merge_tool_call(target: dict[str, Any], part: Any) -> None:
    index = _get(part, "index")
    if not isinstance(index, int) or index < 0:
        index = len(target)
    slot = target.setdefault(
        index, {"id": None, "type": None, "name": None, "arguments": ""}
    )
    call_id, call_type = _get(part, "id"), _get(part, "type")
    if isinstance(call_id, str) and call_id:
        slot["id"] = call_id
    if isinstance(call_type, str) and call_type:
        slot["type"] = call_type
    function = _get(part, "function")
    name, arguments = _get(function, "name"), _get(function, "arguments")
    if isinstance(name, str) and name:
        slot["name"] = name
    if isinstance(arguments, str):
        slot["arguments"] += arguments


def _tool_namespace(slot: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        id=slot["id"],
        type=slot["type"],
        function=SimpleNamespace(name=slot["name"], arguments=slot["arguments"]),
    )


async def _chunks(source: Any) -> AsyncIterator[Any]:
    if isinstance(source, str):
        for raw in source.splitlines():
            parsed = _parse_sse_line(raw.strip())
            if parsed is not None:
                yield parsed
        return
    if hasattr(source, "__aiter__"):
        async for item in source:
            if isinstance(item, str):
                parsed = _parse_sse_line(item.strip())
                if parsed is not None:
                    yield parsed
            else:
                yield item
        return
    raise ValueError("Streaming completion source is not iterable")


async def complete_with_partials(
    create: Callable[..., Awaitable[Any]],
    request_kwargs: dict[str, Any],
    *,
    on_text: Callable[[str], Awaitable[None]] | None = None,
) -> Any:
    """Buffered completion by default; streams only when on_text is set.

    Without a callback this awaits a single non-streaming completion exactly
    like the historical call path. With a callback it assembles through
    stream_completion(), forwarding cumulative text partials.
    """
    if on_text is None:
        return await create(**request_kwargs)
    return await stream_completion(create, request_kwargs, on_text=on_text)


async def stream_completion(
    create: Callable[..., Awaitable[Any]],
    request_kwargs: dict[str, Any],
    *,
    on_text: Callable[[str], Awaitable[None]],
    max_chars: int = MAX_STREAM_CHARS,
    max_chunks: int = MAX_STREAM_CHUNKS,
) -> Any:
    """Assemble one completion while forwarding cumulative text partials.

    Calls create() with stream enabled. Awaitable results are awaited; async
    iterables are consumed directly, so both buffered clients and OpenAI-style
    streaming clients work. Text deltas invoke on_text() with the cumulative
    text so far; the first tool-call delta stops partial emission and the
    remainder is buffered for the assembled full result. Returns a namespace
    shaped like a buffered completion for the existing validation. Raises
    ValueError on malformed chunks or exceeded bounds; provider errors from
    create() propagate unchanged.
    """
    if not 1 <= max_chars <= 65536 or not 1 <= max_chunks <= 65536:
        raise ValueError("Invalid streaming bounds")
    result = create(**request_kwargs, stream=True)
    if inspect.isawaitable(result):
        result = await result
    if _get(result, "choices") is not None:
        return result
    text_parts: list[str] = []
    text_len = 0
    tooling = False
    tools: dict[int, dict[str, Any]] = {}
    finish_reason: Any = None
    seen = 0
    async for item in _chunks(result):
        seen += 1
        if seen > max_chunks:
            raise StreamLimitExceeded("Streaming completion exceeded chunk bound")
        choice = _choice(item)
        reason = _get(choice, "finish_reason")
        if reason is not None:
            if not isinstance(reason, str):
                raise ValueError("Streaming finish reason is not a string")
            finish_reason = reason
        delta = _get(choice, "delta")
        if delta is None:
            continue
        for part in _delta_tool_calls(delta):
            tooling = True
            _merge_tool_call(tools, part)
        if tooling:
            continue
        piece = _delta_text(delta)
        if not piece:
            continue
        text_len += len(piece)
        if text_len > max_chars:
            raise StreamLimitExceeded("Streaming completion exceeded text bound")
        text_parts.append(piece)
        await on_text("".join(text_parts))
    full_text = "".join(text_parts)
    assembled_calls = [_tool_namespace(tools[index]) for index in sorted(tools)] or None
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason=finish_reason,
                message=SimpleNamespace(
                    content=full_text or None,
                    tool_calls=assembled_calls,
                ),
            )
        ]
    )
