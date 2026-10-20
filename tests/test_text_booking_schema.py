"""Website schema reduction retains canonical inputs and later-consent effects."""
import json

import pytest
from sqlalchemy import func, select

from business_capabilities.registry import DEFAULTS
from models.business import BusinessOperation, BusinessPlan
from schemas.managed_turn import ManagedTurnRequest
from tests.fakes import build_llm_response
from tests.test_booking_workflow import tool
from tests.test_business_runtime import later
from tests.test_chotacrm_managed_proposal_errors import install_managed_crm
from tests.test_managed_turns import execute


def names(tools):
    return {row['function']['name'] for row in tools}


@pytest.mark.integration
async def test_website_duplicate_schema_removed_and_direct_booking_confirms_once(
        api_client, db_session, monkeypatch):
    from services import voice_booking_tools

    observed = []
    original = voice_booking_tools.booking_tools

    def capture(tools, *, enabled):
        selected = original(tools, enabled=enabled)
        observed.append((tools, selected))
        return selected

    monkeypatch.setattr(voice_booking_tools, 'booking_tools', capture)
    value, arguments, _, calls, effect, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, channel='WEBSITE_WIDGET', allow_execute=True)
    model.chat.completions.queue(tool('booking_create', arguments, 'prepare'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY' and 'Подтверждаете' in result['draft_text']
    first = model.chat.completions.calls[0]
    assert 'business_propose' not in names(first['tools'])
    assert {'booking_create', 'booking_reschedule', 'booking_cancel'} <= names(first['tools'])
    assert 'business_decide_proposal' not in names(first['tools'])
    baseline, selected = observed[0]
    assert names(baseline)-names(selected) == {'business_propose'}
    assert [row for row in baseline if row['function']['name'] != 'business_propose'] == selected
    before = len(json.dumps([row for row in baseline if row['function']['name'] != 'business_decide_proposal'],
                            ensure_ascii=False, separators=(',', ':')).encode())
    after = len(json.dumps(first['tools'], ensure_ascii=False, separators=(',', ':')).encode())
    assert after < before
    print(json.dumps(dict(schema_baseline_bytes=before, schema_candidate_bytes=after,
                          bytes_removed=before-after, model='SYNTHETIC', actual_provider_tokens_measured=False)))
    assert calls['execute'] == 0 and effect.permit_requests == []
    async with db_session() as db:
        proposal = await db.scalar(select(BusinessPlan))
        assert proposal.proposal_state == 'PREPARED' and proposal.requires_confirmation
        assert proposal.confirmed_turn_id is None
        operation = await db.scalar(select(BusinessOperation))
        assert operation.status == 'RESERVED' and operation.attempt_count == 0
        proposal_id = str(proposal.id)
    confirmation = later(ManagedTurnRequest.model_validate(value), 'Да, подтверждаю')
    body = confirmation.model_dump(mode='json', exclude_unset=True)
    model.chat.completions.queue(tool('business_decide_proposal',
        {'proposal_id': proposal_id, 'intent': 'CONFIRM'}, 'confirm'))
    confirmed = (await execute(api_client, body)).json()
    assert [row['outcome'] for row in confirmed['operation_refs']] == ['SUCCEEDED']
    assert names(model.chat.completions.calls[-1]['tools']) == {'business_decide_proposal'}
    assert model.chat.completions.calls[-1]['tool_choice']['function']['name'] == 'business_decide_proposal'
    assert (await execute(api_client, body)).json()['operation_refs'] == confirmed['operation_refs']
    assert len(model.chat.completions.calls) == 2 and calls['execute'] == 1
    assert len(effect.permit_requests) == 1
    async with db_session() as db:
        operation = await db.scalar(select(BusinessOperation))
        assert operation.status == 'SUCCEEDED' and operation.attempt_count == 1


@pytest.mark.integration
async def test_website_hidden_generic_proposal_cannot_reach_preparation(
        api_client, db_session, monkeypatch):
    value, arguments, _, calls, effect, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, channel='WEBSITE_WIDGET', allow_execute=True)
    model.chat.completions.queue(tool('business_propose', {
        'actions': [{'capability': 'booking.create', 'arguments': arguments}]}, 'hidden'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'FAILED'
    assert calls['execute'] == calls['lookup'] == calls['facts'] == 0 and effect.permit_requests == []
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0


@pytest.mark.integration
@pytest.mark.parametrize('capability', ['deal.create', 'deal.move'])
async def test_website_generic_proposal_preserved_for_non_booking_capability(
        api_client, db_session, monkeypatch, capability):
    value, _, _, calls, effect, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, channel='WEBSITE_WIDGET',
        enabled_capabilities=[*DEFAULTS, capability], allow_execute=True)
    model.chat.completions.queue(build_llm_response(content='Какой вопрос вас интересует?'))
    assert (await execute(api_client, value)).json()['status'] == 'DRAFT_READY'
    advertised = model.chat.completions.calls[0]['tools']
    assert {'business_propose', 'booking_create', 'booking_cancel'} <= names(advertised)
    proposal = next(row for row in advertised if row['function']['name'] == 'business_propose')
    branches = proposal['function']['parameters']['properties']['actions']['items']['oneOf']
    assert capability in {row['properties']['capability']['const'] for row in branches}
    assert calls['execute'] == 0 and effect.permit_requests == []
