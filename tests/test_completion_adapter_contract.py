"""The same SDK wire contract for router and explicit compatible endpoints."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError, AsyncOpenAI

from core.completion_adapter import CompletionAdapterProfile
from core.config import Settings
from core.voice_latency import VoiceRuntimeProfile
from schemas.managed_turn import ManagedTurnRequest
from services.completion_accounting import accounting_metrics
from services.llm_service import LLMService
from services.managed_completion import select_managed_completion
from services.managed_streaming import complete_with_partials
from services.managed_tools import ManagedExecutionError
from tests.test_managed_turns import payload


TOOL = {'type': 'function', 'function': {'name': 'echo', 'parameters': {
    'type': 'object', 'properties': {'value': {'type': 'string'}}, 'required': ['value']}}}


def settings(kind, **overrides):
    return Settings(_env_file=None,
        llm_base_url='https://openrouter.ai/api/v1' if kind == 'openrouter' else 'http://127.0.0.1:19581/v1',
        managed_completion_adapter=CompletionAdapterProfile(kind=kind), **overrides)


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['openrouter', 'openai_compatible'])
@pytest.mark.parametrize('case', ['text', 'tool', 'stream', 'broken_stream', 'timeout', 'cancel', 'absent_usage'])
async def test_completion_contract(kind, case):
    service = LLMService()
    service.settings = settings(kind)
    wire, entered = [], asyncio.Event()

    async def respond(request):
        wire.append(json.loads(request.content))
        entered.set()
        if case == 'cancel':
            await asyncio.Event().wait()
        if case == 'timeout':
            raise httpx.ReadTimeout('Synthetic timeout', request=request)
        common = {'id': 'synthetic', 'object': 'chat.completion', 'created': 0, 'model': 'synthetic'}
        if case in {'stream', 'broken_stream'}:
            events = [{'id': 'synthetic', 'object': 'chat.completion.chunk', 'created': 0,
                'model': 'synthetic', 'choices': [{'index': 0, 'delta': {'content': 'Ответ'}, 'finish_reason': None}]}]
            if case == 'stream':
                events.append({**events[0], 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                content=''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n')
        message = {'role': 'assistant', 'content': 'Ответ'}
        if case == 'tool':
            message = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'echo-1',
                'type': 'function', 'function': {'name': 'echo', 'arguments': '{"value":"test"}'}}]}
        body = {**common, 'choices': [{'index': 0, 'finish_reason': 'tool_calls' if case == 'tool' else 'stop',
                                      'message': message}]}
        if case != 'absent_usage':
            body['usage'] = {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5}
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(respond)
    async with AsyncOpenAI(base_url=service.settings.llm_base_url, api_key='synthetic-endpoint-token',
            max_retries=0, http_client=httpx.AsyncClient(transport=transport)) as client:
        async def create(**kwargs):
            return await service._managed_completion_create(client, **kwargs)

        async def partial(text):
            assert text == 'Ответ'

        request = dict(model='synthetic', messages=[{'role': 'user', 'content': 'test'}], tools=[TOOL],
                       parallel_tool_calls=False, max_tokens=32)
        pending = complete_with_partials(create, request,
            on_text=partial if case in {'stream', 'broken_stream'} else None)
        if case == 'timeout':
            with pytest.raises(APITimeoutError):
                await pending
        elif case == 'cancel':
            task = asyncio.create_task(pending)
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            result = await pending
            selected = select_managed_completion(result, lambda t: t.strip())
            assert selected.branch == ('TOOL_CALLS' if case == 'tool' else
                                       'INVALID_SHAPE' if case == 'broken_stream' else 'FINAL')
            if case == 'absent_usage':
                assert accounting_metrics(result.usage) == {}
            else:
                assert 'cost_nano_credits' not in accounting_metrics(getattr(result, 'usage', None))
        assert len(wire) == 1
        assert not {'provider', 'reasoning', 'openrouter_metadata'} & wire[0].keys()
    assert client.is_closed()


@pytest.mark.parametrize('capability,options', [
    ('streaming', {'stream': True}), ('named_tool_choice', {'tool_choice': {'type': 'function'}}),
    ('json_schema', {'response_format': {'type': 'json_schema'}}),
])
def test_unsupported_request_is_rejected_instead_of_downgraded(capability, options):
    profile = CompletionAdapterProfile(kind='openai_compatible', **{capability: False})
    with pytest.raises(ManagedExecutionError, match='TOOL_SETUP_FAILED'):
        profile.validate_request(options)


@pytest.mark.parametrize('capability', ['tools', 'cancellation'])
def test_unsupported_managed_configuration_fails_startup(capability):
    with pytest.raises(ValueError):
        CompletionAdapterProfile(kind='openai_compatible', **{capability: False})


def test_neutral_voice_budgets_scope_and_no_router_fields():
    value = payload(channel='VOICE')
    profile = VoiceRuntimeProfile(**{k: value[k] for k in ('owner_id', 'integration_id', 'agent_id')},
        model='synthetic', max_output_tokens=128, context_bytes=1024)
    service = LLMService()
    service.settings = settings('openai_compatible', managed_voice_profiles=[profile])
    agent = SimpleNamespace(model_name='synthetic', temperature=0.4)
    request = ManagedTurnRequest.model_validate(value)
    envelope = service._managed_envelope(agent, [], [], request=request)
    assert envelope['max_tokens'] == 128 and 'provider' not in envelope and 'reasoning' not in envelope
    with pytest.raises(ManagedExecutionError, match='CONTEXT_LIMIT'):
        service._check_managed_envelope(agent, [{'role': 'user', 'content': 'x'*1100}], [], request=request)
    value['channel'] = 'TELEGRAM'
    assert service._managed_voice_profile(agent, request=ManagedTurnRequest.model_validate(value)) is None


@pytest.mark.asyncio
async def test_compatible_client_never_receives_stored_cloud_key_or_router_headers(monkeypatch):
    service = LLMService()
    service.settings = settings('openai_compatible', llm_http_referer='https://synthetic.invalid', llm_app_title='test')
    client = service._create_llm_client('stored-cloud-key-do-not-transmit', None, managed=True)
    try:
        assert client.api_key == 'local-endpoint'
        assert service._completion_headers(client, managed=True) is None
    finally:
        await client.close()


@pytest.mark.integration
async def test_neutral_managed_voice_uses_endpoint_credential_without_key_warmup(
        api_client, db_session, monkeypatch):
    import services.llm_service as module
    from core.config import get_settings
    from tests.test_managed_turns import setup, execute

    agent, value = await setup(api_client, db_session, monkeypatch, channel='VOICE')
    profile = VoiceRuntimeProfile(**{k:value[k] for k in ('owner_id', 'integration_id', 'agent_id')},
        model=agent['model_name'], max_output_tokens=128)
    for key, val in dict(llm_base_url='http://127.0.0.1:19581/v1', llm_api_key='synthetic-endpoint-token',
            managed_completion_adapter=CompletionAdapterProfile(kind='openai_compatible'),
            managed_voice_profiles=[profile], managed_voice_latency_profiles=[]).items():
        monkeypatch.setattr(get_settings(), key, val)
    wire = []

    def respond(request):
        assert request.method == 'POST' and request.url.path == '/v1/chat/completions'
        assert request.headers['authorization'] == 'Bearer synthetic-endpoint-token'
        assert 'X-OpenRouter-Metadata' not in request.headers
        body = json.loads(request.content)
        wire.append(body)
        assert 'reasoning' not in body and 'provider' not in body and body['max_tokens'] == 128
        return httpx.Response(200, json={'id':'synthetic', 'object':'chat.completion', 'created':0,
            'model':agent['model_name'], 'choices':[{'index':0,'finish_reason':'stop',
                'message':{'role':'assistant','content':'Уточните время.'}}]})

    def client(**kwargs):
        return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))

    def no_cloud_key(self, agent):
        raise AssertionError('Compatible endpoint attempted cloud-key decryption')

    monkeypatch.setattr(module, 'AsyncOpenAI', client)
    monkeypatch.setattr(LLMService, '_get_agent_api_key', no_cloud_key)
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY' and result['draft_text'] == 'Уточните время.', result
    assert len(wire) == 1 and result['operation_refs'] == []
