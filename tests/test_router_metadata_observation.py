"""Routing observations use synthetic SDK transports, never paid completions."""
from dataclasses import replace
import json
import logging
from types import SimpleNamespace
from uuid import uuid4

import httpx
from openai import APIError, AsyncOpenAI, RateLimitError
from openai.types.chat import ChatCompletion, ChatCompletionChunk
import pytest

import core.managed_completion_diagnostics as diagnostics
from core.managed_security import managed_logging
from services.completion_timing import CompletionTiming
from services.managed_streaming import complete_with_partials
from services.model_retry import classify_model_failure, model_retry_delay
from services.router_metadata_observation import (
    HEADER, RouterObservation, error_router_observation, router_metadata_headers, router_observation,
)
from tests.test_managed_completion import capture_production_logger  # noqa: F401
from tests.test_managed_turns import setup, execute


SECRET = 'router-private-sentinel'


def metadata(provider='NextBit', attempt=2):
    return {'requested': SECRET, 'summary': SECRET, 'region': SECRET, 'attempt': attempt,
            'endpoints': {'available': [{'provider': provider, 'selected': True, 'model': SECRET}]},
            'attempts': [{'provider': SECRET}], 'pipeline': [{'data': {'private': SECRET}}]}


def completion(**extra):
    return dict(id='synthetic', created=1, model='synthetic', object='chat.completion',
        choices=[{'index': 0, 'finish_reason': 'stop',
                  'message': {'role': 'assistant', 'content': 'Какое время вам удобно?'}}], **extra)


@pytest.mark.parametrize('provider,expected', [('NextBit', 'NEXTBIT'), ('Makora', 'MAKORA'),
    ('Cloudflare', 'CLOUDFLARE'), ('OpenAI', 'OPENAI'), (SECRET, 'OTHER'), (True, 'INVALID')])
@pytest.mark.parametrize('shape', ['dict', 'namespace', 'sdk', 'chunk'])
def test_sdk_extra_storage_and_supported_shapes_discard_raw_metadata(provider, expected, shape):
    payload = completion(openrouter_metadata=metadata(provider))
    if shape == 'namespace':
        payload = SimpleNamespace(**payload)
    elif shape == 'sdk':
        payload = ChatCompletion.model_validate(payload)
        assert 'openrouter_metadata' not in vars(payload)
    elif shape == 'chunk':
        payload = ChatCompletionChunk.model_validate(dict(id='s', created=1, model='s',
            object='chat.completion.chunk', choices=[], openrouter_metadata=metadata(provider)))
    observed = router_observation(payload)
    assert observed == RouterObservation('VALID', expected, 'MULTIPLE')
    assert SECRET not in repr(observed)


@pytest.mark.parametrize('attempt,expected', [(0, 'ZERO'), (1, 'ONE'), (2, 'MULTIPLE'),
    (10**50, 'MULTIPLE'), (-1, 'INVALID'), (True, 'INVALID'), ('1', 'INVALID'), (None, 'INVALID')])
def test_attempt_is_only_a_bucket(attempt, expected):
    assert router_observation({'openrouter_metadata': metadata(attempt=attempt)}).router_attempt_bucket == expected


@pytest.mark.parametrize('available,expected', [([], 'UNSELECTED'),
    ([{'provider': SECRET, 'selected': False}], 'UNSELECTED'),
    ([{'provider': 'NextBit', 'selected': 1}], 'INVALID'),
    ([{'provider': 'NextBit'}], 'INVALID'),
    ([{'provider': 'NextBit', 'selected': True}] * 2, 'INVALID'),
    ([{'provider': 'NextBit', 'selected': False}] * 65, 'INVALID'),
    (SECRET, 'INVALID')])
def test_missing_or_ambiguous_selection_does_not_invent_a_provider(available, expected):
    raw = metadata()
    raw['endpoints']['available'] = available
    assert router_observation({'openrouter_metadata': raw}).router_selected_hint == expected


@pytest.mark.parametrize('value', [None, SECRET, [], True])
def test_malformed_metadata_is_closed(value):
    assert router_observation({'openrouter_metadata': value}) == RouterObservation('INVALID', 'INVALID', 'INVALID')


def test_cache_miss_and_arbitrary_accessors_are_not_inferred():
    class Hostile:
        @property
        def openrouter_metadata(self):
            pytest.fail('Optional metadata executed an accessor')
    assert router_observation(Hostile()) == router_observation(completion()) == RouterObservation()
    assert router_observation({'openrouter_metadata': {}}) == RouterObservation('VALID', 'MISSING', 'MISSING')


@pytest.mark.parametrize('url,managed,enabled', [
    ('https://openrouter.ai/api/v1', True, True),
    ('https://openrouter.ai:443/api/v1/', True, True),
    ('https://openrouter.ai/api/v1', False, False),
    ('http://openrouter.ai/api/v1', True, False),
    ('https://openrouter.ai:444/api/v1', True, False),
    ('https://openrouter.ai.example/api/v1', True, False),
    ('https://api.openai.com/v1', True, False),
    ('https://openrouter.ai/proxy/api/v1', True, False),
    ('https://openrouter.ai/api/v1?private=synthetic', True, False)])
async def test_header_scope_does_not_change_other_clients(url, managed, enabled):
    original = {'X-Title': 'synthetic'}
    async with AsyncOpenAI(api_key='synthetic', base_url=url, max_retries=0) as sdk:
        result = router_metadata_headers(sdk, original, managed=managed)
    assert (HEADER in result) is enabled
    assert result['X-Title'] == 'synthetic' and original == {'X-Title': 'synthetic'}
    assert router_metadata_headers(SimpleNamespace(base_url=url), None, managed=True) is None


@pytest.mark.parametrize('stream', [False, True])
@pytest.mark.parametrize('cached', [False, True])
async def test_real_sdk_success_keeps_terminal_metadata_and_unchanged_text(stream, cached):
    requests, partials = [], []
    route = {} if cached else {'openrouter_metadata': metadata()}
    def respond(request):
        requests.append(request)
        if not stream:
            return httpx.Response(200, json=completion(**route))
        chunks = [dict(id='s', created=1, model='s', object='chat.completion.chunk',
            choices=[{'index': 0, 'delta': {'content': 'Какое время вам удобно?'}, 'finish_reason': None}]),
            dict(id='s', created=1, model='s', object='chat.completion.chunk',
                 choices=[{'index': 0, 'delta': {}, 'finish_reason': 'stop'}])]
        if not cached:
            chunks.append(dict(id='s', created=1, model='s', object='chat.completion.chunk',
                choices=[], usage={'prompt_tokens': 5, 'completion_tokens': 7, 'total_tokens': 12}, **route))
        return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
            content=(''.join('data: ' + json.dumps(x) + '\n\n' for x in chunks) + 'data: [DONE]\n\n').encode())
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        async with AsyncOpenAI(api_key='synthetic', base_url='https://openrouter.ai/api/v1',
                http_client=http_client, max_retries=0) as sdk:
            async def partial(text):
                partials.append(text)
            timing = CompletionTiming(router_observation=RouterObservation())
            result = await complete_with_partials(sdk.chat.completions.create,
                dict(model='synthetic', messages=[{'role': 'user', 'content': 'synthetic'}],
                     extra_headers=router_metadata_headers(sdk, None, managed=True)),
                on_text=partial if stream else None, timing=timing)
    assert len(requests) == 1 and requests[0].headers[HEADER] == 'enabled'
    assert result.choices[0].message.content == 'Какое время вам удобно?'
    assert partials == (['Какое время вам удобно?'] if stream else [])
    assert timing.router_observation == (RouterObservation() if cached else RouterObservation('VALID', 'NEXTBIT', 'MULTIPLE'))
    assert SECRET not in repr(timing)
    assert timing.first_delta_ms is None if not stream else type(timing.first_delta_ms) is int


@pytest.mark.parametrize('stream', [False, True])
async def test_real_sdk_error_never_guesses_lost_sse_context(stream):
    requests = []
    payload = {'error': {'code': 429, 'message': SECRET}, 'openrouter_metadata': metadata()}
    def respond(request):
        requests.append(request)
        if stream:
            return httpx.Response(200, headers={'Content-Type': 'text/event-stream'},
                content=('data: ' + json.dumps(payload) + '\n\ndata: [DONE]\n\n').encode())
        return httpx.Response(429, json=payload, headers={'Retry-After': '60'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:
        async with AsyncOpenAI(api_key='synthetic', base_url='https://openrouter.ai/api/v1',
                http_client=http_client, max_retries=0) as sdk:
            with pytest.raises(APIError) as caught:
                result = await sdk.chat.completions.create(model='synthetic',
                    messages=[{'role': 'user', 'content': 'synthetic'}], stream=stream)
                if stream:
                    async for _ in result:
                        pytest.fail('Error cannot yield content')
    before = classify_model_failure(caught.value)
    observed = error_router_observation(caught.value)
    # The SDK drops the top-level sibling on SSE errors. Do not infer it from
    # prior chunks or error.metadata.provider_name; HTTP retains the response.
    assert observed == (RouterObservation() if stream else RouterObservation('VALID', 'NEXTBIT', 'MULTIPLE'))
    assert len(requests) == 1 and classify_model_failure(caught.value) == before
    assert model_retry_delay(caught.value) is None if not stream else before.category == 'RATE_LIMITED'
    assert SECRET not in repr(observed)


@pytest.mark.parametrize('body', [b'x' * 65537, b'not-json', b'\xff'],
                         ids=['oversize', 'not-json', 'bad-utf8'])
def test_error_parsing_is_bounded_and_does_not_read_a_stream(body):
    request = httpx.Request('POST', 'https://synthetic.invalid')
    error = RateLimitError(SECRET, body={}, response=httpx.Response(429, request=request, content=body))
    assert error_router_observation(error) == RouterObservation('INVALID', 'INVALID', 'INVALID')
    response = httpx.Response(429, request=request, stream=httpx.ByteStream(b'synthetic'))
    assert error_router_observation(RateLimitError(SECRET, body={}, response=response)) == RouterObservation()


@pytest.mark.parametrize('metadata_only', [False, True])
@pytest.mark.parametrize('parser_broken', [False, True])
async def test_terminal_frame_without_usage_does_not_change_tool_or_empty_result(
        monkeypatch, metadata_only, parser_broken):
    import services.managed_streaming as streaming
    from services.managed_completion import select_managed_completion
    if parser_broken:
        def broken(value):
            raise RuntimeError(SECRET)
        monkeypatch.setattr(streaming, 'router_observation', broken)
    async def chunks():
        if not metadata_only:
            yield {'choices': [{'finish_reason': 'tool_calls', 'delta': {'tool_calls': [
                {'index': 0, 'id': 's', 'type': 'function', 'function': {
                    'name': 'synthetic_tool', 'arguments': '{"ok":true}'}}]}}]}
        yield {'choices': [], 'openrouter_metadata': metadata()}
    async def create(**kwargs):
        return chunks()
    async def partial(text):
        pytest.fail('Routing frames and tool calls must never publish speech')
    timing = CompletionTiming(router_observation=RouterObservation())
    result = await complete_with_partials(create, {}, on_text=partial, timing=timing)
    selected = select_managed_completion(result, lambda text: text)
    if metadata_only:
        assert selected.branch == 'FINAL' and selected.answer is None and selected.visible_content == 'MISSING'
    else:
        assert selected.branch == 'TOOL_CALLS'
        assert result.choices[0].message.tool_calls[0].function.arguments == '{"ok":true}'
    assert timing.router_observation.router_metadata_state == ('INVALID' if parser_broken else 'VALID')
    assert timing.first_delta_ms is None if metadata_only else type(timing.first_delta_ms) is int


@pytest.mark.parametrize('field', list(diagnostics._ROUTER_FIELDS))
@pytest.mark.parametrize('value', [SECRET, True, [], 1, None])
def test_private_marker_cannot_bypass_route_field_validation(field, value, caplog):
    base = diagnostics.CompletionDiagnostic(uuid4(), 1, 'FINAL', 'STOP', 'TEXT',
        **RouterObservation('VALID', 'NEXTBIT', 'ONE').fields())
    mark = managed_logging.set(True)
    try:
        with caplog.at_level(logging.INFO):
            diagnostics.emit_completion_diagnostic(replace(base, **{field: value}))
    finally:
        managed_logging.reset(mark)
    assert not any(getattr(r, 'event', None) == 'managed_completion_observed' for r in caplog.records)
    assert SECRET not in caplog.text


@pytest.mark.integration
@pytest.mark.parametrize('outcome', ['success', 'terminal', 'recovered'])
async def test_actual_managed_sdk_path_observes_finite_fields_without_effects(
        api_client, db_session, monkeypatch, caplog, outcome):
    import services.llm_service as llm_module
    requests = []
    def respond(request):
        if request.method != 'POST':
            return httpx.Response(200, json={'data': {}})
        requests.append(request)
        if outcome == 'terminal' or outcome == 'recovered' and len(requests) == 1:
            return httpx.Response(429, headers={'Retry-After': '60' if outcome == 'terminal' else '0'}, json={
                'error': {'code': 429, 'message': SECRET}, 'openrouter_metadata': metadata()})
        # A cached recovery supplies no routing metadata. The failed attempt's
        # provider must never be copied to the successful attempt.
        return httpx.Response(200, json=completion(**({} if outcome == 'recovered'
                                                     else {'openrouter_metadata': metadata()})))
    _, value = await setup(api_client, db_session, monkeypatch, channel='VOICE')
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    sdk = AsyncOpenAI(api_key='synthetic', base_url='https://openrouter.ai/api/v1',
        http_client=http_client, max_retries=0)
    monkeypatch.setattr(llm_module.LLMService, '_create_llm_client', lambda *args, **kwargs: sdk)
    with caplog.at_level(logging.INFO):
        result = (await execute(api_client, value)).json()
    assert result['status'] == ('FAILED' if outcome == 'terminal' else 'DRAFT_READY')
    assert result['operation_refs'] == [] and len(requests) == (2 if outcome == 'recovered' else 1) and sdk.is_closed()
    assert all(request.headers[HEADER] == 'enabled' for request in requests)
    rows = [r for r in caplog.records if getattr(r, 'event', None) == 'managed_completion_observed']
    assert len(rows) == len(requests) and rows[0].router_selected_hint == 'NEXTBIT'
    assert rows[0].router_attempt_bucket == 'MULTIPLE' and rows[0].router_metadata_state == 'VALID'
    if outcome == 'recovered':
        assert requests[0].content == requests[1].content
        assert rows[0].branch == 'MODEL_FAILURE' and rows[1].branch == 'FINAL'
        assert rows[1].router_metadata_state == rows[1].router_selected_hint == rows[1].router_attempt_bucket == 'MISSING'
    assert all(SECRET not in r.getMessage() and SECRET not in repr(r.__dict__) for r in caplog.records)
