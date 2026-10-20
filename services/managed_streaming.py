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
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any

from openai.types.chat.chat_completion_chunk import ChatCompletionChunk
from openai.types.completion_usage import CompletionTokensDetails, CompletionUsage

from services.completion_timing import CompletionTiming
from services.completion_accounting import accounting_metrics, bounded_cost, usage_field
from services.router_metadata_observation import RouterObservation, has_router_metadata, router_observation

# Bounded assembly; mirrors the managed result budget without importing it.
MAX_STREAM_CHARS = 8000
MAX_STREAM_CHUNKS = 4096

# The first spoken chunk must not wait for a full sentence boundary: a long
# opening sentence would otherwise stay silent until the model stops. Cut the
# first chunk at a clause or word boundary inside this window instead.
FIRST_CHUNK_MIN_CHARS = 24
FIRST_CHUNK_MAX_CHARS = 72
_CLAUSE_MARKS = (",", ";", ":", "—", "–", "\u2014")

# Keep explicit spoken values together. A provider-side normalizer receives
# each authorized fragment separately, so splitting a clock at its colon or
# an amount before its currency loses the value's pronunciation context.
_SPOKEN_NUMBER = re.compile(
    r"(?<!\w)(?:\bв\s+|\b(?:BYN|USD|EUR|RUB)\s+)?\+?\d[\d.,:/+\-]*"
    r"(?:[ \t\u00a0\u202f]+(?:\d[\d.,:/+\-]*|в(?:\s+\d[\d.,:/+\-]*)?"
    r"|BYN\b|USD\b|EUR\b|RUB\b))*",
    re.IGNORECASE,
)
_SPOKEN_PRICE_CONTEXT = re.compile(
    r"\b(?:стоимость|цена|сумма|итого|оплата|стоит|ценой)\b"
    r"[^0-9,.;:!?]{0,40}[:=—–-]?\s*(?:\d[\d.,]*|$)",
    re.IGNORECASE,
)
_SPOKEN_VALUE_PREFIX = re.compile(r"\b(?:в|BYN|USD|EUR|RUB)\s*$", re.IGNORECASE)


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


def _metadata_field(value: Any, name: str) -> Any:
    # Optional accounting metadata cannot execute arbitrary accessors or affect
    # result validation. Copy only fields stored by the supported client shapes.
    if type(value) is dict:
        return value.get(name)
    if type(value) in (SimpleNamespace, ChatCompletionChunk, CompletionUsage, CompletionTokensDetails):
        return object.__getattribute__(value, '__dict__').get(name)
    return None


def _usage(item: Any) -> SimpleNamespace | None:
    usage = _metadata_field(item, 'usage')
    if type(usage) not in (dict, SimpleNamespace, CompletionUsage):
        return None
    metrics = accounting_metrics(usage)
    counts = {name: metrics[name] for name in ('prompt_tokens', 'completion_tokens', 'total_tokens')
              if name in metrics}
    if 'reasoning_tokens' in metrics:
        counts['completion_tokens_details'] = SimpleNamespace(reasoning_tokens=metrics['reasoning_tokens'])
    prompt = {name: metrics[name] for name in ('cached_tokens', 'cache_write_tokens') if name in metrics}
    if prompt:
        counts['prompt_tokens_details'] = SimpleNamespace(**prompt)
    cost = bounded_cost(usage_field(usage, 'cost'))
    if cost is not None:
        counts['cost'] = cost
    return SimpleNamespace(**counts)


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


class SentenceAccumulator:
    """Collects newly spokable chunks from cumulative streamed text.

    From the second chunk onward a sentence completes at a terminal mark
    (. ! ? …) followed by whitespace and an uppercase letter. This is a
    heuristic, not a linguistic parser: decimals never match, while
    abbreviations before a capital (such as "ул. Ленина") may split early; the
    voice layer applies the same class of heuristic when fragmenting speech.

    The first chunk is latency-critical, so it does not wait for a full
    sentence: once the un-emitted text reaches ``first_max_chars`` the earliest
    clause boundary (else the last word boundary) inside that window is spoken,
    so a long opening sentence starts sounding while the model keeps decoding.
    If streamed text ever revises already emitted content instead of extending
    it, emission freezes and the final result stays authoritative.
    """

    def __init__(
        self,
        *,
        max_chars: int = MAX_STREAM_CHARS,
        first_min_chars: int = FIRST_CHUNK_MIN_CHARS,
        first_max_chars: int = FIRST_CHUNK_MAX_CHARS,
    ) -> None:
        if (
            not 1 <= max_chars <= 65536
            or not 1 <= first_min_chars <= first_max_chars <= 65536
        ):
            raise ValueError("Invalid streaming bounds")
        self._max_chars = max_chars
        self._first_min_chars = first_min_chars
        self._first_max_chars = first_max_chars
        self._emitted = ""
        self._frozen = False
        self._first_done = False

    @property
    def emitted(self) -> str:
        return self._emitted

    def feed(self, cumulative: str) -> list[str]:
        """Return newly emitted chunks; empty list while nothing new is ready."""
        if not isinstance(cumulative, str):
            raise ValueError("Cumulative partial text is not a string")
        if len(cumulative) > self._max_chars:
            raise StreamLimitExceeded("Streaming completion exceeded text bound")
        if self._frozen or not cumulative.startswith(self._emitted):
            self._frozen = True
            return []
        fresh = cumulative[len(self._emitted) :]
        chunks = self._completed_sentences(fresh)
        if not chunks and not self._first_done:
            chunks = self._first_chunk(fresh)
        if chunks:
            self._emitted += "".join(chunks)
            self._first_done = True
        return chunks

    @staticmethod
    def _completed_sentences(fresh: str) -> list[str]:
        sentences: list[str] = []
        start = 0
        index = 0
        while index < len(fresh):
            if fresh[index] in ".!?…":
                end = index + 1
                while end < len(fresh) and fresh[end] in ".!?…":
                    end += 1
                if end < len(fresh) and fresh[end] in (" ", "\t", "\n", "\r"):
                    look = end + 1
                    while look < len(fresh) and fresh[look] in (" ", "\t", "\n", "\r"):
                        look += 1
                    if look < len(fresh) and fresh[look].isupper():
                        sentences.append(fresh[start:look])
                        start = look
                        index = look
                        continue
            index += 1
        return sentences

    def _first_chunk(self, fresh: str) -> list[str]:
        """Cut the first chunk at a clause/word boundary once it is long enough."""
        if len(fresh) < self._first_max_chars:
            return []
        window = fresh[: self._first_max_chars]
        protected = [
            match.span()
            for pattern in (_SPOKEN_NUMBER, _SPOKEN_PRICE_CONTEXT, _SPOKEN_VALUE_PREFIX)
            for match in pattern.finditer(fresh)
        ]
        clauses = [index for index, char in enumerate(window) if char in _CLAUSE_MARKS]
        words = [index for index, char in enumerate(window) if char == " "]
        for candidates in (clauses, words):
            for cut in reversed(candidates):
                if cut < self._first_min_chars - 1:
                    break
                end = cut + 1
                while end < len(fresh) and fresh[end] in (" ", "\t", "\n", "\r"):
                    end += 1
                if any(start < end <= stop for start, stop in protected):
                    continue
                # The next delta may still add a currency or a date/time suffix.
                if fresh[:end].rstrip()[-1:].isdigit():
                    continue
                return [fresh[:end]]
        return []


async def complete_with_partials(
    create: Callable[..., Awaitable[Any]],
    request_kwargs: dict[str, Any],
    *,
    on_text: Callable[[str], Awaitable[None]] | None = None,
    timing: CompletionTiming | None = None,
) -> Any:
    """Buffered completion by default; streams only when on_text is set.

    Without a callback this awaits a single non-streaming completion exactly
    like the historical call path. With a callback it assembles through
    stream_completion(), forwarding cumulative text partials.
    """
    if on_text is None:
        response = await create(**request_kwargs)
        _observe_router(timing, response)
        return response
    return await stream_completion(create, request_kwargs, on_text=on_text, timing=timing)


def _observe_router(timing, response):
    if type(timing) is not CompletionTiming or timing.router_observation is None:
        return False
    if not has_router_metadata(response):
        return False
    try:
        observed = router_observation(response)
    except Exception:
        # Optional observations cannot turn a valid completion into a failure.
        observed = RouterObservation('INVALID', 'INVALID', 'INVALID')
    timing.router_observation = observed
    return True


async def stream_completion(
    create: Callable[..., Awaitable[Any]],
    request_kwargs: dict[str, Any],
    *,
    on_text: Callable[[str], Awaitable[None]],
    max_chars: int = MAX_STREAM_CHARS,
    max_chunks: int = MAX_STREAM_CHUNKS,
    timing: CompletionTiming | None = None,
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
        _observe_router(timing, result)
        return result
    text_parts: list[str] = []
    text_len = 0
    tooling = False
    tools: dict[int, dict[str, Any]] = {}
    finish_reason: Any = None
    seen = 0
    usage = None
    async for item in _chunks(result):
        seen += 1
        if seen > max_chunks:
            raise StreamLimitExceeded("Streaming completion exceeded chunk bound")
        route_present = _observe_router(timing, item)
        if (route_present and type(_get(item, 'choices')) is list
                and not _get(item, 'choices') and _get(item, 'error') is None):
            # Opted-in routing frames may be separate from terminal usage.
            # They supply no text, tools, finish reason or execution authority.
            accounting = _usage(item)
            if accounting is not None and vars(accounting):
                usage = accounting
            continue
        accounting = _usage(item)
        if accounting is not None:
            if vars(accounting):
                usage = accounting
            # OpenAI-compatible accounting frames may have no choice. OpenRouter
            # repeats the terminal choice instead; both remain one completion.
            choices = _get(item, 'choices')
            if type(choices) is list and not choices and _get(item, 'error') is None:
                continue
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
            function = _get(part, 'function')
            if timing is not None and any(isinstance(value, str) and value for value in (
                _get(part, 'id'), _get(function, 'name'), _get(function, 'arguments')
            )):
                timing.first_delta()
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
        if timing is not None:
            timing.first_delta()
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
        ],
        usage=usage,
    )
