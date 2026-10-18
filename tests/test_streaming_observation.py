"""Accounting and local latency observations must preserve the chosen result."""

from types import SimpleNamespace

import pytest
from openai.types.chat.chat_completion_chunk import ChatCompletionChunk

from services.completion_timing import CompletionTiming
from services.managed_completion import select_managed_completion
from services.managed_streaming import StreamLimitExceeded, complete_with_partials, stream_completion


def frame(text=None, **kwargs):
    return {'choices': [{'index': 0, 'delta': {'content': text}, 'finish_reason': kwargs.pop('finish', None)}], **kwargs}


async def assemble(items, **kwargs):
    partials = []

    async def create(**request):
        assert request == {'stream': True}

        async def source():
            for item in items:
                yield item

        return source()

    async def partial(text):
        partials.append(text)

    return await stream_completion(create, {}, on_text=partial, **kwargs), partials


@pytest.mark.parametrize('shape', ['openrouter', 'openai', 'sdk', 'namespace', 'sse'])
async def test_terminal_usage_preserves_the_same_answer_and_partials(shape):
    usage = {'prompt_tokens': 20, 'completion_tokens': 5, 'total_tokens': 25,
             'completion_tokens_details': {'reasoning_tokens': 2}, 'private': 'do-not-retain'}
    terminal = frame('', finish='stop', usage=usage)
    if shape == 'openai':
        terminal['choices'] = []
    elif shape == 'sdk':
        terminal = ChatCompletionChunk.model_validate({
            'id': 'test', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'test', **terminal})
    elif shape == 'namespace':
        terminal['usage'] = SimpleNamespace(**usage, extra='do-not-retain')
        terminal = SimpleNamespace(**terminal)
    elif shape == 'sse':
        import json
        terminal = 'data: ' + json.dumps(terminal)
    result, partials = await assemble([frame('Ответ'), frame(' готов'), frame(finish='stop'), terminal])
    assert partials == ['Ответ', 'Ответ готов']
    assert select_managed_completion(result, str.strip).answer == 'Ответ готов'
    assert result.choices[0].finish_reason == 'stop'
    assert vars(result.usage) == {
        'prompt_tokens': 20, 'completion_tokens': 5, 'total_tokens': 25,
        'completion_tokens_details': SimpleNamespace(reasoning_tokens=2)}
    assert 'do-not-retain' not in repr(result)


@pytest.mark.parametrize('bad', [True, -1, 2147483648, 1.5, float('nan'), 'secret', None])
async def test_invalid_usage_is_ignored_without_affecting_text(bad):
    result, partials = await assemble([
        frame('Ответ'), frame(finish='stop', usage={
            'prompt_tokens': bad, 'completion_tokens': 5,
            'completion_tokens_details': {'reasoning_tokens': bad}})])
    assert partials == ['Ответ']
    assert vars(result.usage) == {'completion_tokens': 5}


async def test_usage_only_frames_still_consume_the_chunk_budget():
    with pytest.raises(StreamLimitExceeded, match='chunk bound'):
        await assemble([frame('Ответ'), {'choices': [], 'usage': {}}, {'choices': [], 'usage': {}}], max_chunks=2)


@pytest.mark.parametrize('accounting', [{}, {'usage': {}}, {'usage': {}, 'error': {'message': 'private'}}])
async def test_unaccounted_or_error_frames_are_not_silently_skipped(accounting):
    # A metadata object alone does not make an error response successful.
    item = {'choices': [], **accounting}
    if accounting == {'usage': {}}:
        result, _ = await assemble([frame('Ответ'), item])
        assert result.usage is None
    else:
        with pytest.raises(ValueError, match='no choices'):
            await assemble([frame('Ответ'), item])


async def test_hostile_usage_is_not_inspected():
    class Hostile:
        def __getattribute__(self, name):
            raise AssertionError('metadata accessor was executed')

    result, _ = await assemble([frame('Ответ', finish='stop', usage=Hostile())])
    assert result.usage is None
    assert select_managed_completion(result, str.strip).answer == 'Ответ'


async def test_first_meaningful_text_delta_is_timed_once_before_callback(monkeypatch):
    clock = iter([10.25, 11.0])
    monkeypatch.setattr('services.completion_timing.time', SimpleNamespace(monotonic=lambda: next(clock)))
    timing = CompletionTiming(started_at=10.0)
    # Empty role/terminal/accounting chunks are not generated text tokens.
    result, _ = await assemble([frame(''), frame('Ответ'), frame(' готов'), frame(finish='stop')], timing=timing)
    assert timing.first_delta_ms == 250
    timing.complete()
    assert timing.model_ms == 1000
    assert select_managed_completion(result, str.strip).answer == 'Ответ готов'


async def test_first_tool_fragment_is_timed_and_usage_does_not_change_dispatch_shape(monkeypatch):
    monkeypatch.setattr('services.completion_timing.time', SimpleNamespace(monotonic=lambda: 20.5))
    timing = CompletionTiming(started_at=20.0)
    tool = {'index': 0, 'id': 'call1', 'type': 'function',
            'function': {'name': 'lookup', 'arguments': '{}'}}
    result, partials = await assemble([
        {'choices': [{'delta': {'tool_calls': [tool]}, 'finish_reason': 'tool_calls'}]},
        {'choices': [], 'usage': {'total_tokens': 25}},
    ], timing=timing)
    assert partials == [] and timing.first_delta_ms == 500
    selected = select_managed_completion(result, str.strip)
    assert selected.branch == 'TOOL_CALLS'
    assert result.choices[0].message.tool_calls[0].function.arguments == '{}'
    assert result.usage.total_tokens == 25


async def test_buffered_path_does_not_claim_first_delta_timing():
    sentinel = SimpleNamespace(choices=[])
    timing = CompletionTiming()

    async def create(**kwargs):
        assert kwargs == {'model': 'm'}
        return sentinel

    assert await complete_with_partials(create, {'model': 'm'}, timing=timing) is sentinel
    timing.complete()
    assert timing.first_delta_ms is None and timing.model_ms >= 0
