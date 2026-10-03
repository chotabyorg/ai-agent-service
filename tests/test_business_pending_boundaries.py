"""Model-visible pending states at real persisted interruption boundaries."""
import copy
import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, func

from business_capabilities.context import failure
from business_capabilities.registry import DEFAULTS
from business_capabilities.runtime import utcnow
from models.business import PendingBusinessAction, BusinessOperation, BusinessConfiguration, CapabilityBinding, BusinessTurn
from tests.test_business_runtime import setup_business, turn, BOOKING
from tests.test_business_continuation import install_chat, runtime_projection
from tests.fakes import build_llm_response, build_tool_call


async def interrupt_pending(env, monkeypatch, shape, boundary='before_operation'):
    db, agent, _, provider, svc = env
    if shape != 'single':
        await svc.configure(db, agent.id, agent.user_id, 'cookie', enabled=list(DEFAULTS) + ['deal.create'])
    proposing = await turn(env, event_id='proposal')
    actions = [{'capability': 'booking.create', 'arguments': BOOKING}]
    if shape != 'single':
        actions.append({'capability': 'deal.create', 'arguments': {'customer_ref': 'c', 'title': 'Стрижка', 'currency': 'BYN',
            'items': [{'offering_ref': 'o', 'quantity': '1'}]}})
    if shape == 'reverse':
        actions.reverse()
    proposal = await proposing.call('business_propose', {'actions': actions})
    ref = proposal['pending_action']['ref']
    if boundary != 'unconfirmed':
        confirming = await turn(env, '+', event_id='consent')
        original_write, original_execute = confirming.write, provider.execute
        calls = 0
        async def interrupted_write(*args, **kwargs):
            nonlocal calls
            calls += 1
            if boundary == 'before_operation':
                raise RuntimeError('synthetic interruption')
            result = await original_write(*args, **kwargs)
            if (boundary == 'failed_operation' or (boundary == 'after_first_operation' and calls == 1)
                    or (boundary == 'after_last_operation' and calls == len(actions))):
                raise RuntimeError('synthetic interruption')
            return result
        async def interrupted_execute(*args, **kwargs):
            if boundary == 'failed_operation':
                return failure('CONFLICT')
            raise RuntimeError('synthetic interruption')
        monkeypatch.setattr(confirming, 'write', interrupted_write)
        if boundary in {'operation_inserted', 'failed_operation'}:
            monkeypatch.setattr(provider, 'execute', interrupted_execute)
        with pytest.raises(RuntimeError, match='synthetic interruption'):
            await confirming.call('business_confirm', {'pending_action_ref': ref})
        monkeypatch.setattr(provider, 'execute', original_execute)
    pending = await db.get(PendingBusinessAction, UUID(ref), populate_existing=True)
    return pending, copy.deepcopy(pending.actions), pending.confirmed_turn_id


@pytest.mark.asyncio
@pytest.mark.parametrize(('shape', 'boundary'), [('single', 'before_operation'), ('composition', 'before_operation'),
    ('reverse', 'before_operation'), ('composition', 'after_first_operation'), ('reverse', 'after_first_operation')])
async def test_model_continues_confirmed_action_after_interruption(api_client, setup_business, monkeypatch, shape, boundary):
    db, _, _, provider, _ = setup_business
    pending, frozen, confirmation = await interrupt_pending(setup_business, monkeypatch, shape, boundary)
    assert pending.state == 'DISPATCHING' and confirmation is not None
    completed = int(boundary == 'after_first_operation')
    assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == completed
    calls = 0
    async def complete(**kwargs):
        nonlocal calls
        calls += 1
        context, instructions = runtime_projection(kwargs['messages'])
        if calls == 1:
            action = context['pending_action']
            expected_state = 'PARTIALLY_COMPLETED' if completed else 'APPROVED'
            assert action['state'] == expected_state and action['continuable'] is True
            assert action['confirmable'] is False
            assert [step['status'] for step in action['progress']] == ['COMPLETED'] * completed + ['UNSTARTED'] * (len(frozen) - completed)
            assert expected_state in instructions and 'прежнее согласие' in instructions
            return build_llm_response(tool_calls=[build_tool_call('continue', 'business_confirm', json.dumps({'pending_action_ref': action['action_ref']}))])
        assert context['pending_action'] is None
        return build_llm_response(content='Оформление завершено.')
    headers, payload = install_chat(monkeypatch, setup_business, complete)
    response = await api_client.post('/v1/chat/completions', headers=headers, json={**payload, 'event_id': 'later', 'message': 'Продолжите оформление'})
    assert response.status_code == 200, response.text
    assert calls == 2 and len(provider.executions) == len(frozen)
    await db.refresh(pending)
    assert pending.actions == frozen and pending.confirmed_turn_id == confirmation
    assert all(effect[2]['confirmation']['confirmed_turn_id'] == str(confirmation) for effect in provider.executions)


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', ['single', 'composition'])
@pytest.mark.parametrize('blocked', ['unconfirmed', 'same_proposal_turn', 'expired', 'disabled', 'stale_generation',
                                    'operation_inserted', 'failed_operation', 'unrelated_unknown', 'recovery_fence'])
async def test_model_respects_complete_pending_state_partition(api_client, setup_business, monkeypatch, shape, blocked):
    import business_capabilities.runtime as runtime_module
    db, agent, _, provider, _ = setup_business
    boundary = blocked if blocked in {'unconfirmed', 'operation_inserted', 'failed_operation'} else 'before_operation'
    pending, _, _ = await interrupt_pending(setup_business, monkeypatch, shape, boundary)
    event, message = 'later', 'Продолжите оформление'
    if blocked == 'same_proposal_turn':
        event, message = 'proposal', 'Хочу записаться'
    elif blocked == 'expired':
        expired = utcnow() + timedelta(minutes=15)
        monkeypatch.setattr(runtime_module, 'utcnow', lambda: expired)
    elif blocked == 'disabled':
        (await db.get(CapabilityBinding, (agent.id, 'booking.create'))).enabled = False
        await db.commit()
    elif blocked == 'stale_generation':
        (await db.get(BusinessConfiguration, agent.id)).generation = uuid4()
        await db.commit()
    elif blocked in {'unrelated_unknown', 'recovery_fence'}:
        note = await turn(setup_business, 'Запишите просьбу', event_id='note')
        provider.lose_response = True
        assert (await note.call('customer_add_note', {'customer_ref': 'c', 'body': 'Просьба'}))['error']['code'] == 'UNKNOWN_WRITE_RESULT'
        provider.lose_response = False
        provider.receipt_available = blocked == 'recovery_fence'
    seen = []
    async def complete(**kwargs):
        context, _ = runtime_projection(kwargs['messages'])
        seen.append(context)
        action = context['pending_action']
        if blocked == 'stale_generation':
            assert action is None
        else:
            assert action['continuable'] is False
            assert action['confirmable'] is (blocked == 'unconfirmed')
            if blocked == 'operation_inserted':
                assert action['progress'][0]['status'] == 'UNRESOLVED'
            if blocked == 'failed_operation':
                assert action['progress'][0]['status'] == 'FAILED'
            if blocked == 'expired':
                assert action['state'] == 'EXPIRED'
        return build_llm_response(content='Уточним состояние оформления.')
    headers, payload = install_chat(monkeypatch, setup_business, complete)
    effects = len(provider.executions)
    response = await api_client.post('/v1/chat/completions', headers=headers, json={**payload, 'event_id': event, 'message': message})
    assert response.status_code == 200, response.text
    assert len(seen) == 1 and len(provider.executions) == effects
    assert all(effect[0] == 'customer.add_note' for effect in provider.executions)


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', ['single', 'composition'])
async def test_model_receives_completed_result_after_final_operation_before_pending_commit(api_client, setup_business, monkeypatch, shape):
    db, _, _, provider, _ = setup_business
    pending, frozen, confirmation = await interrupt_pending(setup_business, monkeypatch, shape, 'after_last_operation')
    assert pending.state == 'DISPATCHING'
    assert len(provider.executions) == len(frozen)
    async def complete(**kwargs):
        context, _ = runtime_projection(kwargs['messages'])
        assert context['pending_action'] is None
        assert len(context['recovered_outcomes']) == len(frozen)
        assert [item['tool_name'] for item in context['recovered_outcomes']] == [a['capability'].replace('.', '_') for a in frozen]
        return build_llm_response(content='Ранее подтверждённое оформление уже завершено.')
    headers, payload = install_chat(monkeypatch, setup_business, complete)
    response = await api_client.post('/v1/chat/completions', headers=headers, json={**payload, 'event_id': 'status', 'message': 'Получилось?'})
    assert response.status_code == 200, response.text
    await db.refresh(pending)
    assert pending.state == 'COMPLETED' and pending.actions == frozen and pending.confirmed_turn_id == confirmation
    status_turn = await db.scalar(select(BusinessTurn).where(BusinessTurn.event_id == 'status'))
    assert status_turn.write_suppressed is True
    assert len(provider.executions) == len(frozen)
    later = await turn(setup_business, 'Добавьте отдельную просьбу', event_id='new-intent')
    assert (await later.call('customer_add_note', {'customer_ref': 'c', 'body': 'Новая просьба'}))['status'] == 'OK'
    assert len(provider.executions) == len(frozen) + 1


@pytest.mark.asyncio
@pytest.mark.parametrize('shape', ['single', 'composition'])
@pytest.mark.parametrize('blocked', ['unavailable_receipt', 'disabled'])
async def test_completed_pending_waits_for_current_receipt_authority(api_client, setup_business, monkeypatch, shape, blocked):
    db, agent, _, provider, _ = setup_business
    pending, frozen, _ = await interrupt_pending(setup_business, monkeypatch, shape, 'after_last_operation')
    binding = await db.get(CapabilityBinding, (agent.id, 'booking.create'))
    if blocked == 'unavailable_receipt':
        provider.receipt_available = False
    else:
        binding.enabled = False
        await db.commit()
    calls = 0
    async def complete(**kwargs):
        nonlocal calls
        calls += 1
        context, _ = runtime_projection(kwargs['messages'])
        if calls == 1:
            assert context['recovered_outcomes'] == []
            action = context['pending_action']
            assert action['confirmable'] is False and action['continuable'] is False
            assert all(step['status'] == 'COMPLETED' for step in action['progress'])
            return build_llm_response(content='Оформление было выполнено; пока не удаётся получить актуальное подтверждение результата.')
        assert context['pending_action'] is None
        assert len(context['recovered_outcomes']) == len(frozen)
        return build_llm_response(content='Получено подтверждение ранее выполненного оформления.')
    headers, payload = install_chat(monkeypatch, setup_business, complete)
    first = await api_client.post('/v1/chat/completions', headers=headers, json={**payload, 'event_id': 'status-unavailable', 'message': 'Какой результат?'})
    assert first.status_code == 200, first.text
    await db.refresh(pending)
    assert pending.state == 'DISPATCHING' and len(provider.executions) == len(frozen)
    same_turn = await turn(setup_business, 'Какой результат?', event_id='status-unavailable')
    assert (await same_turn.call('customer_add_note', {'customer_ref': 'c', 'body': 'Повторная просьба'}))['error']['code'] == 'PREVIOUS_ACTION_COMPLETED'
    provider.receipt_available = binding.enabled = True
    await db.commit()
    later = await api_client.post('/v1/chat/completions', headers=headers, json={**payload, 'event_id': 'status-restored', 'message': 'Теперь есть результат?'})
    assert later.status_code == 200, later.text
    await db.refresh(pending)
    assert calls == 2 and pending.state == 'COMPLETED' and len(provider.executions) == len(frozen)
