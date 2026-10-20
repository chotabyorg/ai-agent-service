"""A consumed exact-argument hint cannot constrain later provider recovery."""
from copy import deepcopy

import pytest
from sqlalchemy import func, select

from models.business import BusinessOperation, BusinessPlan
from tests.test_chotacrm_managed_proposal_errors import (
    existing_customer_lookup, install_managed_crm, preparation_arguments,
)
from tests.test_managed_turns import execute
from tests.test_preparation_schema_correction import preparation


@pytest.mark.integration
@pytest.mark.parametrize('channel', ['VOICE', 'WEBSITE_WIDGET'])
@pytest.mark.parametrize('tool', ['booking_create', 'business_propose'])
@pytest.mark.parametrize('error_code', ['NOT_FOUND', 'BOOKING_CONFLICT', 'PROVIDER_UNAVAILABLE'])
async def test_consumed_reuse_hint_does_not_survive_refused_facts(
        api_client, db_session, monkeypatch, channel, tool, error_code):
    value, arguments, _, calls, effects, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200,
        {'status': 'ERROR', 'error': {'code': error_code, 'message': 'synthetic', 'retry': 'NEVER'}},
        recover_after_first=True, lookup_response=existing_customer_lookup(), channel=channel)
    corrected = {key: val for key, val in arguments.items() if key != 'customer'}
    corrected['customer_ref'] = 'customer_existing'
    snapshots = []
    original = model.chat.completions.create

    async def capture(**kwargs):
        # Actual outgoing input must be copied before later conversation edits.
        snapshots.append(deepcopy(kwargs))
        return await original(**kwargs)

    monkeypatch.setattr(model.chat.completions, 'create', capture)
    model.chat.completions.queue(
        preparation(tool, preparation_arguments(tool, arguments), 'inline-customer'),
        preparation(tool, preparation_arguments(tool, corrected), 'exact-reuse'),
        preparation('booking_check_availability', {key: corrected[key] for key in
                    ('resource_ref', 'offering_ref', 'start_at')}, 'fresh-advisory-read'),
        preparation(tool, preparation_arguments(tool, corrected), 'fresh-proposal'))
    response = await execute(api_client, value)
    assert response.status_code == 200 and response.json()['status'] == 'DRAFT_READY'
    assert 'Подтверждаете' in response.json()['draft_text']
    assert calls['lookup'] == 1 and calls['facts'] >= 2 and calls['execute'] == 0
    assert not effects.permit_requests
    assert len(snapshots) == 4
    hints = [[message for message in call['messages'] if message['role'] == 'system'
              and 'preparation is INCOMPLETE' in message.get('content', '')] for call in snapshots]
    assert not hints[0] and len(hints[1]) == 1
    assert not hints[2] and not hints[3], 'Consumed exact-argument instruction leaked into recovery'
    assert [entry['function']['name'] for entry in snapshots[1]['tools']] == [tool]
    assert 'booking_check_availability' in {entry['function']['name'] for entry in snapshots[2]['tools']}
    async with db_session() as db:
        plan = (await db.scalars(select(BusinessPlan))).one()
        operation = (await db.scalars(select(BusinessOperation))).one()
        assert plan.proposal_state == 'PREPARED' and not plan.confirmed_turn_id
        assert operation.status == 'RESERVED' and operation.attempt_count == 0
        assert operation.request_payload['arguments']['customer_ref'] == 'customer_existing'
        assert 'customer' not in operation.request_payload['arguments']
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 1
