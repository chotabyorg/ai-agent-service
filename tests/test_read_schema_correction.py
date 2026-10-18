"""Malformed read input is corrected once without dispatch or invented results."""
import json

import httpx
import pytest
from sqlalchemy import select

from models.business import BusinessOperation, BusinessPlan
from schemas.managed_turn import ManagedTurnRequest
from tests.fakes import build_llm_response
from tests.test_booking_workflow import tool
from tests.test_business_runtime import later
from tests.test_managed_turns import execute
from tests.test_voice_search_progress import install as install_search


async def install(api_client, db_session, monkeypatch):
    value = await install_search(api_client, db_session, monkeypatch)
    import main
    client = main.app.state.business_registry.get('chotacrm').adapter.client
    original = client.transport

    async def handle(request):
        if request.url.path.endswith('/execute/catalog.search'):
            return httpx.Response(200, json={'status': 'OK', 'data': {'items': []}})
        return await original.handle_async_request(request)

    client.transport = httpx.MockTransport(handle)
    return value


@pytest.mark.integration
@pytest.mark.parametrize('invalid', [{'cursor': None}, {'query': None}, {'limit': '10'}, {'service': 'private-sentinel'}])
async def test_resource_search_corrects_invalid_shape_after_catalog_read(
        api_client, db_session, monkeypatch, invalid):
    value, _, calls, model, searches = await install(api_client, db_session, monkeypatch)
    model.chat.completions.queue(
        tool('catalog_search', {'query': 'стрижка'}, 'catalog'),
        tool('resource_search', invalid, 'invalid'),
        tool('resource_search', {}, 'corrected'),
        build_llm_response(content='На какое время вас записать?'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY', result
    assert len(searches) == 1 and calls['execute'] == 0
    correction = model.chat.completions.calls[2]
    assert correction.get('tool_choice') is None
    assert [item['function']['name'] for item in correction['tools']] == ['resource_search']
    feedback = next(m for m in correction['messages'] if m.get('tool_call_id') == 'invalid')
    payload = json.loads(feedback['content'])
    assert payload['error']['code'] == 'TOOL_ARGUMENT_VALIDATION_FAILED'
    expected = payload['expected_parameters']
    assert set(expected['properties']) == {'query', 'limit', 'cursor'}
    assert expected['additionalProperties'] is False
    assert 'private-sentinel' not in feedback['content']
    assert len(model.chat.completions.calls[-1]['tools']) > 1
    assert any(m.get('tool_call_id') == 'corrected' and json.loads(m['content'])['data']['items'][0]['name'] == 'Анна'
               for m in model.chat.completions.calls[-1]['messages'])


@pytest.mark.integration
@pytest.mark.parametrize('bad_reply', ['invalid_again', 'text', 'wrong_tool', 'same_id'])
async def test_read_correction_is_bounded_and_cannot_become_an_effect(
        api_client, db_session, monkeypatch, bad_reply):
    value, arguments, calls, model, searches = await install(api_client, db_session, monkeypatch)
    next_reply = {
        'invalid_again': tool('resource_search', {'cursor': None}, 'corrected'),
        'text': build_llm_response(content='Вы записаны!'),
        'wrong_tool': tool('booking_create', arguments, 'corrected'),
        'same_id': tool('resource_search', {}, 'invalid'),
    }[bad_reply]
    model.chat.completions.queue(tool('resource_search', {'cursor': None}, 'invalid'), next_reply,
                                build_llm_response(content='never used'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'FAILED', result
    assert len(model.chat.completions.calls) == 2
    assert searches == [] and calls['execute'] == 0
    async with db_session() as db:
        assert await db.scalar(select(BusinessOperation)) is None


@pytest.mark.integration
async def test_corrected_search_keeps_the_booking_preparation_correction_then_confirms(
        api_client, db_session, monkeypatch):
    value, arguments, calls, model, searches = await install(api_client, db_session, monkeypatch)
    model.chat.completions.queue(
        tool('resource_search', {'cursor': None}, 'bad-read'),
        tool('resource_search', {}, 'correct-read'),
        tool('booking_create', {**arguments, 'customer_ref': ''}, 'bad-preparation'),
        tool('booking_create', arguments, 'correct-preparation'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY', result
    assert len(searches) == 1 and calls['execute'] == 0
    assert len(model.chat.completions.calls) == 4
    async with db_session() as db:
        plan = await db.scalar(select(BusinessPlan))
        assert plan.proposal_state == 'PREPARED'
        proposal_id = str(plan.id)
    confirmation = later(ManagedTurnRequest.model_validate(value), 'Да, подтверждаю')
    model.chat.completions.queue(tool('business_decide_proposal',
        {'proposal_id': proposal_id, 'intent': 'CONFIRM'}, 'confirm'))
    completed = (await execute(api_client, confirmation.model_dump(mode='json', exclude_unset=True))).json()
    assert completed['status'] == 'DRAFT_READY', completed
    assert [ref['outcome'] for ref in completed['operation_refs']] == ['SUCCEEDED']
    async with db_session() as db:
        operation = await db.scalar(select(BusinessOperation))
        assert operation.status == 'SUCCEEDED' and operation.attempt_count == 1
    assert calls['execute'] == 1


@pytest.mark.integration
async def test_read_budget_is_shared_across_read_tools(api_client, db_session, monkeypatch):
    value, _, calls, model, searches = await install(api_client, db_session, monkeypatch)
    model.chat.completions.queue(
        tool('resource_search', {'cursor': None}, 'bad-read'),
        tool('resource_search', {}, 'correct-read'),
        tool('booking_check_availability', {}, 'another-bad-read'),
        build_llm_response(content='never used'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'FAILED', result
    assert len(model.chat.completions.calls) == 3
    assert len(searches) == 1 and calls['execute'] == 0
    async with db_session() as db:
        assert await db.scalar(select(BusinessOperation)) is None
