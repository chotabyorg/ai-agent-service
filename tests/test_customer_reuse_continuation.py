"""Real CRM adapter preparation must precede prose and one explicit consent."""
import json

import pytest
from sqlalchemy import func, select

from models.business import BusinessOperation, BusinessPlan
from schemas.managed_turn import ManagedTurnRequest
from services.llm_service import LLMService
from tests.fakes import build_llm_response, build_tool_call
from tests.test_business_runtime import later
from tests.test_chotacrm_managed_proposal_errors import (
    existing_customer_lookup, install_managed_crm, preparation_arguments,
)
from tests.test_managed_turns import execute
from tests.test_preparation_schema_correction import preparation


async def fixture(api_client, db_session, monkeypatch, channel, tool):
    value, args, _, calls, effect, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {},
        lookup_response=existing_customer_lookup(), allow_execute=True, channel=channel)
    corrected = {k: v for k, v in args.items() if k != 'customer'}
    corrected['customer_ref'] = 'customer_existing'
    initial = preparation(tool, preparation_arguments(tool, args), 'initial')
    return value, args, corrected, calls, effect, model, initial


@pytest.mark.integration
@pytest.mark.parametrize('channel', ['VOICE', 'WEBSITE_WIDGET'])
@pytest.mark.parametrize('tool', ['booking_create', 'business_propose'])
async def test_existing_customer_is_prepared_in_same_turn_then_confirmed_once(
        api_client, db_session, monkeypatch, channel, tool):
    value, _, corrected, calls, effect, model, initial = await fixture(
        api_client, db_session, monkeypatch, channel, tool)
    model.chat.completions.queue(initial,
        preparation(tool, preparation_arguments(tool, corrected), 'explicit-reference'))
    response = await execute(api_client, value)
    result = response.json()
    assert response.status_code == 200 and result['status'] == 'DRAFT_READY'
    assert 'Подтверждаете' in result['draft_text']
    assert calls['lookup'] == 1 and calls['facts'] >= 1 and calls['execute'] == 0
    assert not effect.permit_requests
    assert len(model.chat.completions.calls) == 2
    continuation = model.chat.completions.calls[1]
    assert 'tool_choice' not in continuation
    assert [t['function']['name'] for t in continuation['tools']] == [tool]
    assert continuation['messages'][-1]['role'] == 'system'
    async with db_session() as db:
        plan = (await db.scalars(select(BusinessPlan))).one()
        assert plan.proposal_state == 'PREPARED'
        proposal_id = str(plan.id)
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 1
    confirmed = later(ManagedTurnRequest.model_validate(value), 'Да, подтверждаю.')
    decision = build_tool_call('one-consent', 'business_decide_proposal', json.dumps({
        'proposal_id': proposal_id, 'intent': 'CONFIRM'}))
    decision.type = 'function'
    model.chat.completions.queue(build_llm_response(tool_calls=[decision]))
    confirmation = await execute(api_client, confirmed.model_dump(mode='json'))
    assert confirmation.status_code == 200 and confirmation.json()['status'] == 'DRAFT_READY'
    assert calls['execute'] == 1
    # The same managed request replay cannot create a second booking.
    replay = await execute(api_client, confirmed.model_dump(mode='json'))
    assert replay.status_code == 200 and calls['execute'] == 1
    async with db_session() as db:
        operation = (await db.scalars(select(BusinessOperation))).one()
        assert operation.status == 'SUCCEEDED' and operation.attempt_count == 1


@pytest.mark.integration
@pytest.mark.parametrize('tool', ['booking_create', 'business_propose'])
@pytest.mark.parametrize('bad', ['prose', 'wrong_tool', 'wrong_ref', 'mixed_customer',
                                'changed_time', 'same_id', 'multiple'])
async def test_reuse_continuation_cannot_publish_unprepared_prose_or_dispatch_other_actions(
        api_client, db_session, monkeypatch, bad, tool):
    value, args, corrected, calls, effect, model, initial = await fixture(
        api_client, db_session, monkeypatch, 'VOICE', tool)
    partials = []
    original = LLMService._execute_core
    async def on_text(text):
        partials.append(text)
    async def with_partials(self, *args, **kwargs):
        kwargs['partial_hook'] = on_text
        return await original(self, *args, **kwargs)
    monkeypatch.setattr(LLMService, '_execute_core', with_partials)
    second = preparation(tool, preparation_arguments(tool, corrected), 'continuation')
    call = second.choices[0].message.tool_calls[0]
    if bad == 'prose':
        second = build_llm_response(content='Всё подготовила. Подтверждаете запись?')
    elif bad == 'wrong_tool':
        call.function.name = 'customer_create'
    elif bad == 'wrong_ref':
        call.function.arguments = json.dumps(preparation_arguments(
            tool, {**corrected, 'customer_ref': 'untrusted'}))
    elif bad == 'mixed_customer':
        call.function.arguments = json.dumps(preparation_arguments(
            tool, {**corrected, 'customer': args['customer']}))
    elif bad == 'changed_time':
        call.function.arguments = json.dumps(preparation_arguments(
            tool, {**corrected, 'start_at': '2099-01-01T12:00:00Z'}))
    elif bad == 'same_id':
        call.id = 'initial'
    elif bad == 'multiple':
        second.choices[0].message.tool_calls.append(
            build_tool_call('extra', 'customer_create', '{}'))
    model.chat.completions.queue(initial, second, build_llm_response(content='Never reached'))
    response = await execute(api_client, value)
    assert response.status_code != 200 or response.json()['status'] != 'DRAFT_READY'
    assert calls['lookup'] == 1 and calls['facts'] == calls['execute'] == 0
    assert not effect.permit_requests and not partials
    assert len(model.chat.completions.calls) == 2
    assert model.chat.completions.calls[0].get('stream') is True
    assert not model.chat.completions.calls[1].get('stream')
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0


@pytest.mark.integration
async def test_schema_correction_and_customer_reuse_each_have_one_independent_preparation_step(
        api_client, db_session, monkeypatch):
    value, _, corrected, calls, _, model, initial = await fixture(
        api_client, db_session, monkeypatch, 'VOICE', 'booking_create')
    model.chat.completions.queue(preparation('booking_create', {}, 'bad-shape'), initial,
        preparation('booking_create', corrected, 'explicit-reference'))
    response = await execute(api_client, value)
    assert response.status_code == 200 and 'Подтверждаете' in response.json()['draft_text']
    assert calls['lookup'] == 1 and calls['execute'] == 0
    assert len(model.chat.completions.calls) == 3


@pytest.mark.integration
async def test_repeated_reuse_feedback_cannot_restart_continuation_loop(
        api_client, db_session, monkeypatch):
    from services.business_runtime import BusinessRuntime, BusinessToolResult
    value, _, corrected, calls, effect, model, initial = await fixture(
        api_client, db_session, monkeypatch, 'VOICE', 'booking_create')
    original = BusinessRuntime.call_tool
    async def repeated(self, name, arguments):
        if name == 'booking_create' and 'customer_ref' in arguments:
            return BusinessToolResult(tool_payload={
                'status': 'CUSTOMER_REUSE_REQUIRED', 'customer_ref': 'customer_existing'})
        return await original(self, name, arguments)
    monkeypatch.setattr(BusinessRuntime, 'call_tool', repeated)
    model.chat.completions.queue(initial, preparation('booking_create', corrected, 'explicit'),
                                 build_llm_response(content='Never reached'))
    response = await execute(api_client, value)
    assert response.status_code != 200 or response.json()['status'] != 'DRAFT_READY'
    assert len(model.chat.completions.calls) == 2
    assert calls['lookup'] == 1 and calls['facts'] == calls['execute'] == 0
    assert not effect.permit_requests
