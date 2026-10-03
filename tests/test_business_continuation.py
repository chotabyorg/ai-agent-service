"""Model-facing partial composition behavior with deterministic model/provider."""
import copy
import json
from datetime import timedelta
from html import unescape
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from business_capabilities.registry import DEFAULTS
from business_capabilities.runtime import utcnow
from models.business import BusinessConfiguration, BusinessOperation, PendingBusinessAction, CapabilityBinding
from tests.test_business_runtime import setup_business, turn, BOOKING
from tests.fakes import build_llm_response, build_tool_call


def runtime_projection(messages):
    system = messages[0]['content']
    body = system.split('<runtime_capability_context>', 1)[1].split('</runtime_capability_context>', 1)[0]
    return json.loads(unescape(body[body.index('{'):]).strip()), system


async def partial(env, interrupted_state):
    db, agent, _, provider, svc = env
    await svc.configure(db, agent.id, agent.user_id, 'cookie', enabled=list(DEFAULTS) + ['deal.create'])
    first = await turn(env, event_id='proposal')
    proposal = await first.call('business_propose', {'actions': [
        {'capability': 'booking.create', 'arguments': BOOKING},
        {'capability': 'deal.create', 'arguments': {'customer_ref': 'c', 'title': 'Стрижка', 'currency': 'BYN',
            'items': [{'offering_ref': 'o', 'quantity': '1'}]}}]})
    confirmation = await turn(env, '+', event_id='confirmation')
    provider.lose_response = True
    ref = proposal['pending_action']['ref']
    assert (await confirmation.call('business_confirm', {'pending_action_ref': ref}))['error']['code'] == 'UNKNOWN_WRITE_RESULT'
    provider.lose_response = False
    pending = await db.get(PendingBusinessAction, UUID(ref))
    operation = await db.scalar(select(BusinessOperation))
    pending.state = operation.state = interrupted_state
    await db.commit()
    return pending, copy.deepcopy(pending.actions), pending.confirmed_turn_id


def install_chat(monkeypatch, env, completion):
    import api.v1.chat as chat
    import business_capabilities.runtime as runtime_module
    import services.llm_service as llm_module
    _, agent, settings, provider, _ = env
    settings.chat_s2s_callers_json = SecretStr(json.dumps({'website-gateway': 'synthetic'}))
    monkeypatch.setattr(chat, 'get_settings', lambda: settings)
    monkeypatch.setattr(chat.llm_service, 'settings', settings)
    monkeypatch.setattr(runtime_module, 'ChotaCRMAdapter', lambda _: provider)
    model = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
    monkeypatch.setattr(llm_module, 'AsyncOpenAI', lambda **kw: model)
    return {'X-Service-Token': 'synthetic', 'X-User-Id': str(agent.user_id)}, {
        'agent_id': str(agent.id), 'external_client_id': 'client-a', 'channel': 'website'}


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupted_state', ['UNKNOWN', 'DISPATCHING'])
async def test_model_sees_recovery_fence_then_exact_frozen_continuation(api_client, setup_business, monkeypatch, interrupted_state):
    db, agent, _, provider, _ = setup_business
    pending, frozen, original_confirmation = await partial(setup_business, interrupted_state)
    observed = []
    async def complete(**kwargs):
        context, system = runtime_projection(kwargs['messages'])
        observed.append(context)
        assert 'continuable' in system and 'только ещё не начатые' in system
        if len(observed) <= 2:
            action = context['pending_action']
            assert action['action_ref'] == str(pending.id)
            assert action['state'] == 'PARTIALLY_COMPLETED'
            assert [step['status'] for step in action['progress']] == ['COMPLETED', 'UNSTARTED']
            assert [step['tool_name'] for step in action['progress']] == ['booking_create', 'deal_create']
            assert action['continuable'] is (len(observed) == 2)
            if len(observed) == 1:
                return build_llm_response(content='Запись создана. Оформление ещё не завершено.')
            assert 'business_confirm' in [t['function']['name'] for t in kwargs['tools']]
            return build_llm_response(tool_calls=[build_tool_call('continue', 'business_confirm', json.dumps({'pending_action_ref': action['action_ref']}))])
        assert context['pending_action'] is None
        return build_llm_response(content='Оставшееся оформление завершено.')
    headers, payload = install_chat(monkeypatch, setup_business, complete)
    recovery = await api_client.post('/v1/chat/completions', headers=headers, json={**payload, 'message': 'Что получилось?', 'event_id': 'recovery'})
    assert recovery.status_code == 200, recovery.text
    assert len(provider.executions) == 1
    later = await api_client.post('/v1/chat/completions', headers=headers, json={**payload, 'message': 'Завершите оставшееся оформление', 'event_id': 'later'})
    assert later.status_code == 200, later.text
    assert len(observed) == 3
    assert [entry[0] for entry in provider.executions] == ['booking.create', 'deal.create']
    await db.refresh(pending)
    assert pending.actions == frozen and pending.confirmed_turn_id == original_confirmation
    assert provider.executions[1][2]['confirmation']['confirmed_turn_id'] == str(original_confirmation)


@pytest.mark.asyncio
@pytest.mark.parametrize('interrupted_state', ['UNKNOWN', 'DISPATCHING'])
@pytest.mark.parametrize('blocked', ['unresolved', 'expired', 'disabled', 'stale_generation'])
async def test_model_never_sees_unresolved_expired_or_stale_continuation(api_client, setup_business, monkeypatch, interrupted_state, blocked):
    import business_capabilities.runtime as runtime_module
    db, agent, _, provider, _ = setup_business
    pending, _, _ = await partial(setup_business, interrupted_state)
    if blocked == 'unresolved':
        provider.receipt_available = False
    else:
        recovery = await turn(setup_business, 'Статус', event_id='recovered')
        await recovery.prompt_context()
        if blocked == 'expired':
            later_time = utcnow() + timedelta(minutes=15)
            monkeypatch.setattr(runtime_module, 'utcnow', lambda: later_time)
        elif blocked == 'disabled':
            binding = await db.get(CapabilityBinding, (agent.id, 'deal.create'))
            binding.enabled = False
            await db.commit()
        else:
            (await db.get(BusinessConfiguration, agent.id)).generation = uuid4()
            await db.commit()
    seen = []
    async def complete(**kwargs):
        context, system = runtime_projection(kwargs['messages'])
        seen.append(context)
        action = context['pending_action']
        if blocked == 'stale_generation':
            assert action is None
        else:
            assert action['continuable'] is False and action['confirmable'] is False
            if blocked == 'unresolved':
                assert action['progress'][0]['status'] == 'UNRESOLVED'
                assert action['state'] in {'UNKNOWN', 'DISPATCHING'}
            elif blocked == 'expired':
                assert action['state'] == 'EXPIRED' and action['progress'][1]['status'] == 'EXPIRED'
        return build_llm_response(content='Пока нельзя завершить оформление; уточним актуальное состояние.')
    headers, payload = install_chat(monkeypatch, setup_business, complete)
    result = await api_client.post('/v1/chat/completions', headers=headers, json={**payload, 'message': 'Завершите оформление', 'event_id': 'later'})
    assert result.status_code == 200, result.text
    assert len(seen) == 1 and len(provider.executions) == 1
    if blocked == 'expired':
        current = await turn(setup_business, 'Завершите оформление', event_id='later')
        assert (await current.call('business_confirm', {'pending_action_ref': str(pending.id)}))['error']['code'] == 'STALE_PROPOSAL'
        assert len(provider.executions) == 1
