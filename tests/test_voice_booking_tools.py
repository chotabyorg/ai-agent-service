"""Model surface reduction preserves booking confirmation and other capabilities."""
from copy import deepcopy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from business_capabilities.registry import REGISTRY
from core.config import get_settings
from models.business import BusinessOperation, BusinessPlan
from schemas.managed_turn import ManagedTurnRequest
from services.voice_booking_tools import booking_tools
from services.managed_preparation import _observe_lookup_failure
from schemas.business import BusinessSafetyError
from tests.test_chotacrm_managed_proposal_errors import install_managed_crm
from tests.test_booking_workflow import tool
from tests.test_business_runtime import later
from tests.test_managed_turns import execute
from tests.test_voice_search_progress import install


def surface(capabilities=('booking.create', 'booking.cancel')):
    tools = [REGISTRY[name].tool() for name in capabilities]
    tools.extend([
        {'function': {'name': 'business_decide_proposal', 'parameters': {}}},
        {'function': {'name': 'business_propose', 'parameters': {'properties': {
            'actions': {'items': {'oneOf': [
                {'properties': {'capability': {'const': name},
                                'arguments': REGISTRY[name].tool()['function']['parameters']}}
                for name in capabilities]}}}}}},
    ])
    return tools


@pytest.mark.parametrize('case', ['disabled', 'bundle', 'missing_direct', 'changed_schema', 'missing_decide'])
def test_unfamiliar_or_unselected_surface_is_preserved(case):
    tools = surface(('booking.create', 'deal.create') if case == 'bundle' else ('booking.create',))
    if case == 'missing_direct':
        tools = tools[1:]
    elif case == 'changed_schema':
        tools[0]['function']['parameters']['required'].append('unexpected')
    elif case == 'missing_decide':
        tools = [t for t in tools if t['function']['name'] != 'business_decide_proposal']
    assert booking_tools(tools, enabled=case != 'disabled') is tools


def test_only_duplicate_schema_is_removed_without_mutation():
    tools = surface()
    original = deepcopy(tools)
    selected = booking_tools(tools, enabled=True)
    assert [t['function']['name'] for t in selected] == [
        'booking_create', 'booking_cancel', 'business_decide_proposal']
    assert tools == original
    assert all(t is tools[i] for i, t in enumerate(selected))


@pytest.mark.parametrize('error,category', [
    (BusinessSafetyError('BINDING_UNAVAILABLE'), 'BINDING_UNAVAILABLE'),
    (ValueError('private-contact-sentinel'), 'LOOKUP_INVALID'),
    (TimeoutError('private-contact-sentinel'), 'PROVIDER_UNAVAILABLE'),
    (RuntimeError('private-contact-sentinel'), 'OTHER'),
])
def test_lookup_failure_diagnostic_never_contains_exception_text(error, category):
    observed = []
    _observe_lookup_failure(SimpleNamespace(observe_business_diagnostic=lambda **v: observed.append(v)), error)
    assert observed[0]['refusal_category'] == category
    assert 'private-contact-sentinel' not in repr(observed)


@pytest.mark.integration
async def test_selected_voice_direct_booking_confirms_and_replays_once(api_client, db_session, monkeypatch):
    value, arguments, calls, model, _ = await install(api_client, db_session, monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, 'managed_voice_latency_profiles', [
        p.model_copy(update={'concise_speech': True}) for p in settings.managed_voice_latency_profiles])
    model.chat.completions.queue(tool('booking_create', arguments, 'prepare'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY' and 'Подтверждаете' in result['draft_text']
    names = [t['function']['name'] for t in model.chat.completions.calls[0]['tools']]
    assert 'business_propose' not in names and 'booking_create' in names
    assert 'business_decide_proposal' not in names
    assert calls['execute'] == 0
    async with db_session() as db:
        proposal = await db.scalar(select(BusinessPlan))
        assert proposal.proposal_state == 'PREPARED'
        proposal_id = str(proposal.id)
    confirmation = later(ManagedTurnRequest.model_validate(value), 'Да, подтверждаю')
    payload = confirmation.model_dump(mode='json', exclude_unset=True)
    model.chat.completions.queue(tool('business_decide_proposal',
        {'proposal_id': proposal_id, 'intent': 'CONFIRM'}, 'confirm'))
    confirmed = (await execute(api_client, payload)).json()
    assert [ref['outcome'] for ref in confirmed['operation_refs']] == ['SUCCEEDED']
    decision_call = model.chat.completions.calls[-1]
    assert 'tool_choice' not in decision_call
    assert [t['function']['name'] for t in decision_call['tools']] == ['business_decide_proposal']
    assert (await execute(api_client, payload)).json()['operation_refs'] == confirmed['operation_refs']
    async with db_session() as db:
        operation = await db.scalar(select(BusinessOperation))
        assert operation.status == 'SUCCEEDED' and operation.attempt_count == 1
    assert calls['execute'] == 1


@pytest.mark.integration
async def test_hidden_generic_tool_cannot_dispatch(api_client, db_session, monkeypatch):
    value, arguments, calls, model, _ = await install(api_client, db_session, monkeypatch)
    settings = get_settings()
    monkeypatch.setattr(settings, 'managed_voice_latency_profiles', [
        p.model_copy(update={'concise_speech': True}) for p in settings.managed_voice_latency_profiles])
    model.chat.completions.queue(tool('business_propose', {
        'actions': [{'capability': 'booking.create', 'arguments': arguments}]}, 'hidden'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'FAILED'
    assert calls['execute'] == calls['facts'] == calls['lookup'] == 0
    async with db_session() as db:
        assert await db.scalar(select(BusinessPlan)) is None


@pytest.mark.integration
async def test_invalid_contact_requests_clarification_without_effects(api_client, db_session, monkeypatch):
    value, arguments, _, calls, effect, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, channel='VOICE',
        lookup_response={'status': 'ERROR', 'error': {
            'code': 'VALIDATION_ERROR', 'message': 'private-contact-sentinel', 'retry': 'NEVER'}})
    model.chat.completions.queue(tool('booking_create', arguments, 'invalid-contact'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY'
    assert 'номер с кодом страны' in result['draft_text']
    assert 'private-contact-sentinel' not in result['draft_text']
    assert 'позже' not in result['draft_text']
    assert calls['lookup'] == 1 and calls['facts'] == calls['execute'] == 0
    assert effect.permit_requests == []
