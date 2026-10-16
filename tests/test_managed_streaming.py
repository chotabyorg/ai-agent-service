"""Streaming completion assembly: partials, fallback, bounds, no persistence."""

import pytest
from types import SimpleNamespace

from services.managed_completion import select_managed_completion
from services.managed_streaming import (
    SentenceAccumulator,
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


def test_accumulator_emits_only_completed_sentences():
    acc = SentenceAccumulator()
    assert acc.feed("Hel") == []
    assert acc.feed("Hello. W") == ["Hello. "]
    assert acc.emitted == "Hello. "
    assert acc.feed("Hello. World") == []
    assert acc.feed("Hello. World. Done") == ["World. "]


def test_accumulator_ignores_decimals_but_documents_abbreviation_risk():
    acc = SentenceAccumulator()
    assert acc.feed("Price 10.30 rub") == []
    assert acc.feed("Price 10.30 rubles. Thanks") == ["Price 10.30 rubles. "]


def test_accumulator_freezes_on_revision_and_bounds_input():
    acc = SentenceAccumulator()
    assert acc.feed("Hello. World") == ["Hello. "]
    assert acc.feed("Hi. World") == []
    assert acc.feed("Hi. World. Again") == []
    with pytest.raises(StreamLimitExceeded):
        SentenceAccumulator(max_chars=4).feed("Hello. World")
    with pytest.raises(ValueError, match="not a string"):
        acc.feed(None)


def test_streaming_request_contract_defaults_and_version_gate():
    from schemas.managed_turn import ManagedTurnRequest

    base = {
        "conversation_id": "00000000-0000-4000-8000-000000000001",
        "turn_id": "00000000-0000-4000-8000-000000000002",
        "owner_id": "owner",
        "integration_id": "00000000-0000-4000-8000-000000000003",
        "agent_id": "00000000-0000-4000-8000-000000000004",
        "channel": "VOICE",
        "input_revision": 1,
        "authority_revision": 0,
        "input_first_sequence": 1,
        "input_last_sequence": 1,
        "context_last_sequence": 1,
        "messages": [
            {
                "id": "00000000-0000-4000-8000-000000000005",
                "sequence": 1,
                "actor": "CUSTOMER",
                "direction": "INBOUND",
                "content_type": "TEXT",
                "text": "hi",
            }
        ],
        "selected_input_message_ids": ["00000000-0000-4000-8000-000000000005"],
    }
    plain = ManagedTurnRequest(**base)
    assert plain.stream_partials is False and plain.max_partial_chars == 2000
    assert plain.contract_version == 1
    with pytest.raises(ValueError, match="contract version 2"):
        ManagedTurnRequest(**{**base, "stream_partials": True})
    streamed = ManagedTurnRequest(
        **{**base, "contract_version": 2, "stream_partials": True, "max_partial_chars": 50}
    )
    assert streamed.stream_partials is True and streamed.max_partial_chars == 50


def test_partial_result_fields_validate_text_and_shape():
    from schemas.managed_turn import ManagedTurnResult

    base = {
        "conversation_id": "00000000-0000-4000-8000-000000000001",
        "turn_id": "00000000-0000-4000-8000-000000000002",
        "agent_id": "00000000-0000-4000-8000-000000000004",
        "input_revision": 1,
        "authority_revision": 0,
        "input_first_sequence": 1,
        "input_last_sequence": 1,
        "context_last_sequence": 1,
        "status": "DRAFT_READY",
        "draft_text": "Hello there.",
        "error_code": None,
    }
    result = ManagedTurnResult(**base)
    assert result.partial_text is None and result.partial_final is False
    assert ManagedTurnResult(**{**base, "partial_text": "Hello."}).partial_text == "Hello."
