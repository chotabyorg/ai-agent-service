"""Ordinary timeout/recovery regressions with a synthetic deterministic provider."""
from uuid import UUID

import pytest
from sqlalchemy import select, func

from business_capabilities.runtime import CapabilityRuntime
from business_capabilities.registry import DEFAULTS
from models.agent import AgentTemplate
from models.business import BusinessConversation, BusinessOperation, PendingBusinessAction
from tests.test_business_runtime import setup_business, turn, BOOKING


NOTE = {'customer_ref': 'c', 'body': 'Перезвонить завтра'}


@pytest.mark.asyncio
@pytest.mark.parametrize('recovery_path', ['automatic', 'same_payload', 'same_intent', 'changed_payload', 'predecessor'])
async def test_recovered_turn_blocks_fresh_writes_but_not_reads_or_later_intent(setup_business, db_session, recovery_path):
    db, agent, settings, provider, _ = setup_business
    original = await turn(setup_business, 'Запишите просьбу', event_id='original')
    provider.lose_response = True
    provider.receipt_available = False
    assert (await original.call('customer_add_note', NOTE))['error']['code'] == 'UNKNOWN_WRITE_RESULT'
    if recovery_path == 'predecessor':
        # Controlled lifecycle setup only: keep the same lineage while creating
        # a fresh current context. Real identity evidence is provider-owned.
        conversation = await db.get(BusinessConversation, original.context.session_id)
        conversation.retired = True
        await db.commit()
    message, event = ('Запишите просьбу', 'original') if recovery_path == 'same_intent' else ('Уточните результат', 'recovery')
    current = await turn(setup_business, message, event_id=event)
    provider.lose_response = False
    provider.receipt_available = True
    if recovery_path == 'automatic':
        assert len((await current.prompt_context()).recovered_outcomes) == 1
    else:
        arguments = {**NOTE, 'body': 'Другая формулировка'} if recovery_path == 'changed_payload' else NOTE
        recovered = await current.call('customer_add_note', arguments)
        assert recovered['status'] == 'OK' or recovered['error']['code'] == 'PREVIOUS_ACTION_COMPLETED'
    # Different calls in the same response and later model iterations may vary
    # input spelling. Recovery never authorizes another business effect this turn.
    for arguments in [NOTE, {**NOTE, 'body': '  Перезвонить завтра  '}, {**NOTE, 'body': 'Другая просьба'}]:
        result = await current.call('customer_add_note', arguments)
        if recovery_path == 'same_intent' and arguments != {**NOTE, 'body': 'Другая просьба'}:
            assert result['status'] == 'OK'  # original operation receipt replay
        else:
            assert result['error']['code'] == 'PREVIOUS_ACTION_COMPLETED'
    assert (await current.call('catalog_search', {}))['status'] == 'OK'
    assert len(provider.executions) == 1
    assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 1

    # No in-memory runtime object survives this request/session boundary.
    async with db_session() as reopened_db:
        reopened_agent = await reopened_db.get(AgentTemplate, agent.id)
        reopened = await CapabilityRuntime.open(reopened_db, settings, reopened_agent, 'client-a', 'website',
            'website-gateway', agent.user_id, event_id=event, user_message=message, adapter=provider)
        assert reopened.turn_id == current.turn_id
        assert (await reopened.call('customer_add_note', {**NOTE, 'body': 'После перезапуска'}))['error']['code'] == 'PREVIOUS_ACTION_COMPLETED'
        assert (await reopened.call('catalog_search', {}))['status'] == 'OK'
    assert len(provider.executions) == 1
    later = await turn(setup_business, 'Добавьте ещё такую просьбу', event_id='deliberate-later')
    assert (await later.call('customer_add_note', NOTE))['status'] == 'OK'
    assert len(provider.executions) == 2


@pytest.mark.asyncio
async def test_recovery_does_not_prepare_or_consume_unconfirmed_proposal(setup_business):
    db, agent, settings, provider, _ = setup_business
    previous = await turn(setup_business, event_id='proposal')
    ref = (await previous.call('booking_create', BOOKING))['pending_action']['ref']
    provider.lose_response = True
    assert (await previous.call('customer_add_note', NOTE))['error']['code'] == 'UNKNOWN_WRITE_RESULT'
    current = await turn(setup_business, 'Получилось?', event_id='status')
    provider.lose_response = False
    await current.prompt_context()
    prepared = len(provider.preparations)
    assert (await current.call('booking_create', {**BOOKING, 'start_at': '2026-10-15T17:00:00Z'}))['error']['code'] == 'PREVIOUS_ACTION_COMPLETED'
    assert len(provider.preparations) == prepared
    assert (await current.call('business_confirm', {'pending_action_ref': ref}))['error']['code'] == 'PREVIOUS_ACTION_COMPLETED'
    assert (await db.get(PendingBusinessAction, UUID(ref), populate_existing=True)).state == 'PROPOSED'
    assert (await current.call('business_cancel', {'pending_action_ref': ref}))['status'] == 'OK'
    assert len(provider.executions) == 1


@pytest.mark.asyncio
async def test_partial_composition_recovery_waits_for_later_turn_then_continues_frozen_action(setup_business):
    db, agent, _, provider, svc = setup_business
    await svc.configure(db, agent.id, agent.user_id, 'cookie', enabled=list(DEFAULTS) + ['deal.create'])
    proposed = await turn(setup_business, event_id='composition')
    proposal = await proposed.call('business_propose', {'actions': [
        {'capability': 'booking.create', 'arguments': BOOKING},
        {'capability': 'deal.create', 'arguments': {'customer_ref': 'c', 'title': 'Стрижка', 'currency': 'BYN',
            'items': [{'offering_ref': 'o', 'quantity': '1'}]}}]})
    reference = {'pending_action_ref': proposal['pending_action']['ref']}
    confirmed = await turn(setup_business, '+', event_id='consent')
    provider.lose_response = True
    assert (await confirmed.call('business_confirm', reference))['error']['code'] == 'UNKNOWN_WRITE_RESULT'
    provider.lose_response = False
    recovery = await turn(setup_business, 'Что получилось?', event_id='recovery')
    await recovery.prompt_context()
    for _ in range(2):
        result = await recovery.call('business_confirm', reference)
        assert result['error']['code'] == 'PREVIOUS_ACTION_COMPLETED'
        assert len(result['completed_actions']) == 1
    assert len(provider.executions) == 1
    pending = await db.get(PendingBusinessAction, UUID(reference['pending_action_ref']), populate_existing=True)
    assert pending.state == 'UNKNOWN' and pending.confirmed_turn_id == confirmed.turn_id
    later = await turn(setup_business, 'Завершите оставшееся оформление', event_id='finish-composition')
    assert (await later.call('business_confirm', reference))['status'] == 'OK'
    assert [entry[0] for entry in provider.executions] == ['booking.create', 'deal.create']
    assert (await later.call('business_confirm', reference))['status'] == 'OK'
    assert len(provider.executions) == 2
