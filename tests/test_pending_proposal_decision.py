"""Pending semantic replies must reach runtime before ordinary assistant prose."""
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from core.config import get_settings
from models.agent import AgentTemplate
from services.business_runtime import BusinessRuntime
from services.llm_service import LLMService, PrivacyProxyCompletions
from services.managed_runtime import ManagedRuntime
from services.managed_tools import ManagedExecutionError
from services.prompt_builder import ChatContext
from tests.fakes import ScriptedLLMClient, build_llm_response, build_tool_call
from tests.test_business_runtime import later
from tests.test_chotacrm_adapter import crm_settings, setup

CHOICE = {'type': 'function', 'function': {'name': 'business_decide_proposal'}}


def decision(proposal_id, intent):
    call = build_tool_call('decision', 'business_decide_proposal', json.dumps({
        'proposal_id': proposal_id, 'intent': intent}))
    call.type = 'function'
    return call


async def pending(factory, settings, monkeypatch, text='нет'):
    original, registry, provider, args = await setup(factory, settings)
    proposed = await original.call_tool('booking_create', args)
    request = later(original.request, text)
    runtime = ManagedRuntime(20, request=request)
    runtime.business = await BusinessRuntime.open(request, original.grant, factory, registry, execution=runtime)
    monkeypatch.setattr(get_settings(), 'knowledge_base_enabled', False)
    return original, runtime, provider, proposed.tool_payload['proposal_id']


async def run_core(factory, runtime, model):
    request = runtime.request
    async with factory() as session:
        agent = await session.get(AgentTemplate, request.agent_id)
        try:
            return await LLMService()._execute_core(session, agent=agent, llm_client=model,
                credentials_records=(), communication=[{
                    'role': 'user' if m.actor == 'CUSTOMER' else 'assistant', 'content': m.text
                } for m in request.messages], chat_context=ChatContext(channel='telegram', external_client_id='synthetic'),
                query='\n'.join(m.text for m in request.selected_messages), runtime=runtime,
                selected_indices=request.selected_local_indices, simple_selection=request.simple_selection)
        finally:
            await runtime.stack.aclose()


@pytest.mark.asyncio
async def test_reject_prose_without_runtime_decision_is_not_a_success(db_session, crm_settings, monkeypatch):
    original, runtime, provider, _ = await pending(db_session, crm_settings, monkeypatch)
    model = ScriptedLLMClient()
    model.chat.completions.queue(build_llm_response(content='Хорошо, запись отменена.'))
    with pytest.raises(ManagedExecutionError) as failure:
        await run_core(db_session, runtime, model)
    assert failure.value.code == 'INVALID_RESULT'
    plan = await original.repository.selected_plan(original.request, original.grant)
    assert plan.proposal_state == 'PREPARED' and plan.operations[0].status == 'RESERVED'
    assert provider['posts'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('intent,text,state,effects', [
    ('REJECT', 'нет', 'CANCELLED', 0), ('UNRELATED', 'Как вас найти?', 'PREPARED', 0),
    ('CHANGE', 'да, только на час позже', 'CANCELLED', 0), ('CONFIRM', '+', 'CONFIRMED', 1),
])
async def test_pending_reply_requires_one_decision_then_releases_choice(
        db_session, crm_settings, monkeypatch, intent, text, state, effects):
    original, runtime, provider, proposal_id = await pending(db_session, crm_settings, monkeypatch, text)
    model = ScriptedLLMClient()
    model.chat.completions.queue(build_llm_response(tool_calls=[decision(proposal_id, intent)]),
                                 build_llm_response(content='Ответ после решения runtime.'))
    assert await run_core(db_session, runtime, model) == 'Ответ после решения runtime.'
    assert model.chat.completions.calls[0]['tool_choice'] == CHOICE
    assert 'tool_choice' not in model.chat.completions.calls[1]
    plan = await original.repository.selected_plan(original.request, original.grant)
    assert plan.proposal_state == state and provider['posts'] == effects
    assert plan.operations[0].status == ('SUCCEEDED' if effects else 'RESERVED')


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', ['wrong_tool', 'wrong_proposal', 'multiple', 'missing_type', 'missing_id', 'extra_args'])
async def test_ignored_forced_decision_fails_before_any_dispatch(
        db_session, crm_settings, monkeypatch, invalid):
    original, runtime, provider, proposal_id = await pending(db_session, crm_settings, monkeypatch)
    call = decision(proposal_id, 'CONFIRM')
    calls = [call]
    if invalid == 'wrong_tool':
        call.function.name = 'catalog_search'
        call.function.arguments = '{}'
    elif invalid == 'wrong_proposal':
        call.function.arguments = json.dumps({'proposal_id': str(uuid4()), 'intent': 'CONFIRM'})
    elif invalid == 'multiple':
        calls.append(decision(proposal_id, 'REJECT'))
    elif invalid == 'missing_type':
        del call.type
    elif invalid == 'missing_id':
        del call.id
    elif invalid == 'extra_args':
        call.function.arguments = json.dumps({'proposal_id': proposal_id, 'intent': 'CONFIRM', 'arguments': {}})
    model = ScriptedLLMClient()
    model.chat.completions.queue(build_llm_response(tool_calls=calls))
    with pytest.raises(ManagedExecutionError, match='INVALID_RESULT'):
        await run_core(db_session, runtime, model)
    plan = await original.repository.selected_plan(original.request, original.grant)
    assert plan.proposal_state == 'PREPARED' and plan.operations[0].status == 'RESERVED'
    assert provider['posts'] == 0 and len(model.chat.completions.calls) == 1


@pytest.mark.asyncio
async def test_no_pending_proposal_does_not_force_a_decision(db_session, crm_settings, monkeypatch):
    original, _, provider, _ = await setup(db_session, crm_settings)
    runtime = ManagedRuntime(20, request=original.request)
    runtime.business = original
    monkeypatch.setattr(get_settings(), 'knowledge_base_enabled', False)
    model = ScriptedLLMClient()
    model.chat.completions.queue(build_llm_response(content='Обычный ответ.'))
    assert await run_core(db_session, runtime, model) == 'Обычный ответ.'
    assert 'tool_choice' not in model.chat.completions.calls[0] and provider['posts'] == 0


@pytest.mark.asyncio
async def test_pending_projection_without_decision_tool_fails_closed(db_session, crm_settings, monkeypatch):
    _, runtime, provider, _ = await pending(db_session, crm_settings, monkeypatch)
    advertised = runtime.business.openai_tools()
    monkeypatch.setattr(runtime.business, 'openai_tools', lambda: [
        tool for tool in advertised if tool['function']['name'] != 'business_decide_proposal'])
    model = ScriptedLLMClient()
    with pytest.raises(ManagedExecutionError, match='INVALID_RESULT'):
        await run_core(db_session, runtime, model)
    assert not model.chat.completions.calls and provider['posts'] == 0


def test_required_choice_counts_toward_envelope_and_history_trimming(monkeypatch):
    service = LLMService()
    agent = SimpleNamespace(model_name='synthetic', temperature=0)
    history = [{'role': 'user', 'content': 'old'}, {'role': 'user', 'content': 'selected'}]
    messages = [{'role': 'system', 'content': 'trusted'}, *history]
    size_without_choice = len(json.dumps(service._managed_envelope(agent, messages, []), ensure_ascii=False).encode())
    monkeypatch.setattr(service.settings, 'managed_ai_context_bytes', size_without_choice)
    service._check_managed_envelope(agent, messages, [])
    with pytest.raises(ManagedExecutionError, match='CONTEXT_LIMIT'):
        service._check_managed_envelope(agent, messages, [], tool_choice=CHOICE)
    minimal = [messages[0], history[1]]
    maximum = len(json.dumps(service._managed_envelope(agent, minimal, [], tool_choice=CHOICE), ensure_ascii=False).encode())
    monkeypatch.setattr(service.settings, 'managed_ai_context_bytes', maximum)
    assert service._budget_managed_messages(agent, messages, [], [1], history, True, tool_choice=CHOICE) == minimal
    monkeypatch.setattr(service.settings, 'managed_ai_context_bytes', maximum - 1)
    with pytest.raises(ManagedExecutionError, match='CONTEXT_LIMIT'):
        service._budget_managed_messages(agent, messages, [], [1], history, True, tool_choice=CHOICE)


@pytest.mark.asyncio
async def test_privacy_proxy_preserves_standard_required_function_choice(monkeypatch):
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': 'synthetic'}}]})
    original = httpx.AsyncClient
    monkeypatch.setattr('services.llm_service.httpx.AsyncClient', lambda **kwargs:
        original(transport=httpx.MockTransport(handler), **kwargs))
    proxy = PrivacyProxyCompletions(privacy_filter_base_url='https://fixture.invalid',
        internal_api_token='synthetic', privacy_filter_timeout_seconds=1,
        llm_base_url='https://fixture.invalid', agent_api_key='synthetic', privacy_session_id=uuid4())
    await proxy.create(model='synthetic', messages=[], tool_choice=CHOICE, parallel_tool_calls=False)
    assert captured[0]['chat_completion']['tool_choice'] == CHOICE
    assert captured[0]['chat_completion']['parallel_tool_calls'] is False
