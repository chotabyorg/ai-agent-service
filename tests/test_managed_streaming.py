"""Streaming completion assembly: partials, fallback, bounds, no persistence."""

import pytest
from types import SimpleNamespace

from services.managed_completion import select_managed_completion
from services.managed_streaming import (
    StreamLimitExceeded,
    complete_with_partials,
    stream_completion,
)
from tests.fakes import build_llm_response


def chunk(*, text=None, tool_calls=None, finish=None):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=text, tool_calls=tool_calls),
                finish_reason=finish,
            )
        ]
    )


async def collect(create, **kwargs):
    partials = []

    async def on_text(cumulative):
        partials.append(cumulative)

    result = await stream_completion(create, {}, on_text=on_text, **kwargs)
    return result, partials


def sse_stream(lines):
    async def generate():
        for line in lines:
            yield line

    return generate()


async def test_text_partials_forward_cumulative_and_assemble_final():
    async def create(**kwargs):
        assert kwargs.get("stream") is True

        async def generate():
            yield chunk(text="Hello")
            yield chunk(text=" there")
            yield chunk(finish="stop")

        return generate()

    result, partials = await collect(create)
    assert partials == ["Hello", "Hello there"]
    selected = select_managed_completion(result, lambda text: text.strip())
    assert (selected.branch, selected.answer) == ("FINAL", "Hello there")


async def test_raw_sse_text_is_parsed():
    async def create(**kwargs):
        return sse_stream(
            [
                'data: {"choices": [{"delta": {"content": "Hi"}}]}',
                "",
                'data: {"choices": [{"delta": {"content": "!"}}]}',
                "data: [DONE]",
            ]
        )

    result, partials = await collect(create)
    assert partials == ["Hi", "Hi!"]
    selected = select_managed_completion(result, lambda text: text.strip())
    assert selected.answer == "Hi!"


async def test_non_awaitable_stream_is_consumed_directly():
    def create(**kwargs):
        assert kwargs.get("stream") is True

        async def generate():
            yield chunk(text="direct")
            yield chunk(finish="stop")

        return generate()

    result, partials = await collect(create)
    assert partials == ["direct"]
    selected = select_managed_completion(result, lambda text: text.strip())
    assert selected.answer == "direct"


async def test_buffered_answer_returns_unchanged_without_partials():
    sentinel = build_llm_response(content="full answer")

    async def create(**kwargs):
        return sentinel

    result, partials = await collect(create)
    assert result is sentinel
    assert partials == []


async def test_tool_delta_stops_partials_but_keeps_full_result():
    tool = SimpleNamespace(
        index=0,
        id="call_1",
        type="function",
        function=SimpleNamespace(name="lookup", arguments='{"q":'),
    )
    tool_more = SimpleNamespace(
        index=0,
        id=None,
        type=None,
        function=SimpleNamespace(name=None, arguments='"x"}'),
    )

    async def create(**kwargs):
        async def generate():
            yield chunk(text="Checking")
            yield chunk(tool_calls=[tool])
            yield chunk(text=" ignored", tool_calls=[tool_more])
            yield chunk(finish="tool_calls")

        return generate()

    result, partials = await collect(create)
    assert partials == ["Checking"]
    selected = select_managed_completion(result, lambda text: text.strip())
    assert selected.branch == "TOOL_CALLS"
    calls = result.choices[0].message.tool_calls
    assert len(calls) == 1 and calls[0].id == "call_1"
    assert calls[0].function.arguments == '{"q":"x"}'


async def test_text_bound_is_explicit():
    async def create(**kwargs):
        async def generate():
            yield chunk(text="ab")
            yield chunk(text="cd")

        return generate()

    with pytest.raises(StreamLimitExceeded):
        await collect(create, max_chars=3)


async def test_chunk_bound_is_explicit():
    async def create(**kwargs):
        async def generate():
            yield chunk(text="a")
            yield chunk(text="b")
            yield chunk(text="c")

        return generate()

    with pytest.raises(StreamLimitExceeded):
        await collect(create, max_chunks=2)


async def test_malformed_chunk_fails_explicitly():
    async def create(**kwargs):
        async def generate():
            yield SimpleNamespace()

        return generate()

    with pytest.raises(ValueError, match="no choices"):
        await collect(create)


async def test_provider_error_propagates_without_partials():
    async def create(**kwargs):
        raise RuntimeError("provider unavailable")

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await collect(create)


async def test_helper_without_hook_passes_request_through_unchanged():
    async def create(**kwargs):
        return build_llm_response(content="same model path")

    result = await complete_with_partials(create, {"model": "m", "stream": False})
    assert result.choices[0].message.content == "same model path"


async def test_helper_with_hook_streams_partials_and_final():
    async def create(**kwargs):
        assert kwargs.get("stream") is True

        async def generate():
            yield chunk(text="nine")
            yield chunk(text=" plus ten")
            yield chunk(finish="stop")

        return generate()

    partials = []

    async def on_text(cumulative):
        partials.append(cumulative)

    result = await complete_with_partials(create, {"model": "m"}, on_text=on_text)
    assert partials == ["nine", "nine plus ten"]
    selected = select_managed_completion(result, lambda text: text.strip())
    assert selected.answer == "nine plus ten"
