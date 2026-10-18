"""Booking stays one confirmed aggregate across text and voice managed requests."""
import json

import pytest
from sqlalchemy import func, select

from models.business import BusinessOperation, BusinessPlan
from schemas.managed_turn import ManagedTurnRequest
from tests.fakes import build_llm_response, build_tool_call
from tests.test_business_runtime import later
from tests.test_chotacrm_managed_proposal_errors import install_managed_crm
from tests.test_managed_turns import execute


def tool(name, arguments, call_id):
    call = build_tool_call(call_id, name, json.dumps(arguments))
    call.type = 'function'
    return build_llm_response(tool_calls=[call])


@pytest.mark.integration
@pytest.mark.parametrize('channel', ['WEBSITE_WIDGET', 'VOICE'])
@pytest.mark.parametrize('wrong_prerequisite', ['none', 'availability', 'batch'])
async def test_aggregate_booking_prepares_confirms_and_replays_once(
        api_client, db_session, monkeypatch, channel, wrong_prerequisite):
    value, arguments, _, calls, effect_port, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, channel=channel, allow_execute=True)
    if wrong_prerequisite == 'availability':
        availability = {key: arguments[key] for key in ('offering_ref', 'resource_ref', 'start_at')}
        model.chat.completions.queue(tool('booking_check_availability', availability, 'availability'),
            tool('customer_create', {'display_name': arguments['customer']['display_name'],
                 'identities': [arguments['customer']['identity']]}, 'wrong-prerequisite'))
    proposal = tool('booking_create', arguments, 'aggregate')
    if wrong_prerequisite == 'batch':
        proposal.choices[0].message.tool_calls.insert(0, tool('customer_create', {
            'display_name': arguments['customer']['display_name'],
            'identities': [arguments['customer']['identity']]}, 'wrong-prerequisite'
        ).choices[0].message.tool_calls[0])
    model.chat.completions.queue(proposal)
    response = await execute(api_client, value)
    assert response.status_code == 200
    proposed = response.json()
    assert proposed['status'] == 'DRAFT_READY', proposed
    assert 'Подтверждаете' in proposed['draft_text']
    # Direct aggregate preparation does not need a separate model customer.find.
    assert len(model.chat.completions.calls) == (3 if wrong_prerequisite == 'availability' else 1)
    assert calls['lookup'] == 1 and calls['execute'] == 0
    assert effect_port.permit_requests == []
    async with db_session() as db:
        plans = (await db.scalars(select(BusinessPlan))).all()
        operations = (await db.scalars(select(BusinessOperation))).all()
        assert len(plans) == len(operations) == 1
        assert plans[0].proposal_state == 'PREPARED'
        assert operations[0].status == 'RESERVED'
        assert operations[0].request_payload['capability'] == 'booking.create'
        proposal_id = str(plans[0].id)
    if wrong_prerequisite == 'availability':
        feedback = [message for message in model.chat.completions.calls[-1]['messages']
                    if message.get('role') == 'tool' and message.get('name') == 'customer_create']
        assert json.loads(feedback[-1]['content'])['status'] == 'BOOKING_AGGREGATE_REQUIRED'
    confirmation = later(ManagedTurnRequest.model_validate(value), 'Да, подтверждаю')
    confirmation_value = confirmation.model_dump(mode='json', exclude_unset=True)
    model.chat.completions.queue(tool('business_decide_proposal',
        {'proposal_id': proposal_id, 'intent': 'CONFIRM'}, 'confirm'))
    completed = await execute(api_client, confirmation_value)
    assert completed.status_code == 200
    result = completed.json()
    assert result['status'] == 'DRAFT_READY', result
    assert 'Запись была подтверждена' in result['draft_text']
    assert 'Окрашивание' in result['draft_text']
    assert calls['execute'] == 1
    assert [reference['outcome'] for reference in result['operation_refs']] == ['SUCCEEDED']
    model_calls = len(model.chat.completions.calls)
    replay = await execute(api_client, confirmation_value)
    assert replay.status_code == 200
    assert replay.json()['operation_refs'] == result['operation_refs']
    assert calls['execute'] == 1 and len(model.chat.completions.calls) == model_calls
    async with db_session() as db:
        operation = await db.scalar(select(BusinessOperation))
        assert operation.attempt_count == 1 and operation.status == 'SUCCEEDED'
        assert operation.effect_permit_id is not None


@pytest.mark.integration
@pytest.mark.parametrize('channel', ['WEBSITE_WIDGET', 'VOICE'])
@pytest.mark.parametrize('lookup', [
    {'status': 'ERROR', 'error': {
        'code': 'PROVIDER_UNAVAILABLE', 'message': 'private detail', 'retry': 'NEVER'}},
    {'status': 'OK', 'data': {'match': 'SINGLE', 'candidates': []}},
])
async def test_failed_customer_lookup_cannot_fall_through_into_customer_create(
        api_client, db_session, monkeypatch, channel, lookup):
    value, arguments, _, calls, effect_port, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, channel=channel,
        lookup_response=lookup)
    model.chat.completions.queue(
        tool('customer_find', {'identity': arguments['customer']['identity']}, 'lookup'),
        tool('customer_create', {'display_name': 'Влад',
             'identities': [arguments['customer']['identity']]}, 'must-not-run'))
    response = await execute(api_client, value)
    result = response.json()
    assert response.status_code == 200 and result['status'] == 'DRAFT_READY', result
    assert 'Не удалось проверить контакт' in result['draft_text']
    assert 'private detail' not in result['draft_text']
    assert len(model.chat.completions.calls) == 1
    assert calls['lookup'] == 1 and calls['execute'] == calls['facts'] == 0
    assert effect_port.permit_requests == []
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
