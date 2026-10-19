"""Managed Voice/text continue from one combined search to a separately confirmed effect."""
import json

import httpx
import pytest
from sqlalchemy import select

import main
from models.business import BusinessOperation, BusinessPlan
from schemas.managed_turn import ManagedTurnRequest
from tests.fakes import build_llm_response
from tests.test_booking_search_options import ARGUMENTS
from tests.test_booking_workflow import tool
from tests.test_business_runtime import later
from tests.test_managed_turns import execute
from tests.test_voice_search_progress import install as install_resource_search


async def install(api_client, db_session, monkeypatch, channel):
    result = await install_resource_search(api_client, db_session, monkeypatch, channel)
    client = main.app.state.business_registry.get('chotacrm').adapter.client
    original = client.transport
    async def handle(request):
        if request.url.path.endswith('/execute/catalog.search'):
            return httpx.Response(200, json={'status': 'OK', 'data': {'items': [dict(
                ref='offering_test', name='Окрашивание', type='SERVICE', active=True,
                price='50.00', currency='BYN')]}, 'next_cursor': 'issued-catalog-page'})
        response = await original.handle_async_request(request)
        if request.url.path.endswith('/execute/resource.search'):
            payload = response.json()
            payload.setdefault('next_cursor', 'issued-resource-page')
            return httpx.Response(200, json=payload)
        return response
    client.transport = httpx.MockTransport(handle)
    return result


@pytest.mark.integration
@pytest.mark.parametrize('channel', ['VOICE', 'WEBSITE_WIDGET'])
async def test_combined_search_then_proposal_and_later_consent_executes_once(
        api_client, db_session, monkeypatch, channel):
    value, arguments, calls, model, searches = await install(api_client, db_session, monkeypatch, channel)
    model.chat.completions.queue(tool('booking_search_options', ARGUMENTS, 'search'),
        tool('booking_create', arguments, 'prepare'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY' and 'Подтверждаете' in result['draft_text'], result
    assert calls['execute'] == 0 and len(searches) == 1
    assert len(model.chat.completions.calls) == 2
    combined = next(m for m in model.chat.completions.calls[-1]['messages'] if m.get('tool_call_id') == 'search')
    assert json.loads(combined['content'])['resources']['data']['items'][0]['ref'] == 'resource_test'
    async with db_session() as db:
        plan = await db.scalar(select(BusinessPlan))
        operation = await db.scalar(select(BusinessOperation))
        assert plan.proposal_state == 'PREPARED' and operation.status == 'RESERVED'
        proposal_id = str(plan.id)
    confirmation = later(ManagedTurnRequest.model_validate(value), 'Да, подтверждаю')
    model.chat.completions.queue(tool('business_decide_proposal',
        {'proposal_id': proposal_id, 'intent': 'CONFIRM'}, 'confirm'))
    response = await execute(api_client, confirmation.model_dump(mode='json', exclude_unset=True))
    completed = response.json()
    assert completed['status'] == 'DRAFT_READY'
    assert [ref['outcome'] for ref in completed['operation_refs']] == ['SUCCEEDED']
    assert (await execute(api_client, confirmation.model_dump(mode='json', exclude_unset=True))).json() == completed
    async with db_session() as db:
        operation = await db.scalar(select(BusinessOperation))
        assert operation.status == 'SUCCEEDED' and operation.attempt_count == 1
    assert calls['execute'] == 1


@pytest.mark.integration
@pytest.mark.parametrize('channel', ['VOICE', 'WEBSITE_WIDGET'])
async def test_equivalent_compound_searches_retire_only_in_voice_and_keep_real_payloads(
        api_client, db_session, monkeypatch, channel):
    value, _, calls, model, searches = await install(api_client, db_session, monkeypatch, channel)
    model.chat.completions.queue(tool('booking_search_options', ARGUMENTS, 'first'),
        tool('booking_search_options', {'catalog': {**ARGUMENTS['catalog'], 'limit': 10},
             'resources': {'query': '', 'limit': 10}}, 'same'),
        build_llm_response(content='Есть мастер Анна. Во сколько вам удобно?'))
    assert (await execute(api_client, value)).json()['status'] == 'DRAFT_READY'
    assert len(searches) == 2 and calls['execute'] == 0
    names = {t['function']['name'] for t in model.chat.completions.calls[-1]['tools']}
    assert ('booking_search_options' in names) is (channel != 'VOICE')
    assert {'catalog_search', 'resource_search'} <= names
    for call_id in ('first', 'same'):
        message = next(m for m in model.chat.completions.calls[-1]['messages'] if m.get('tool_call_id') == call_id)
        assert json.loads(message['content'])['resources']['data']['items'][0]['name'] == 'Анна'


@pytest.mark.integration
@pytest.mark.parametrize('change', ['query', 'catalog_cursor', 'resource_cursor'])
async def test_changed_search_or_page_remains_available(api_client, db_session, monkeypatch, change):
    value, _, calls, model, searches = await install(api_client, db_session, monkeypatch, 'VOICE')
    changed = {'catalog': dict(ARGUMENTS['catalog']), 'resources': {}}
    if change == 'query':
        changed['catalog']['query'] = 'окрашивание'
    elif change == 'catalog_cursor':
        changed['catalog']['cursor'] = 'issued-catalog-page'
    else:
        changed['resources']['cursor'] = 'issued-resource-page'
    model.chat.completions.queue(tool('booking_search_options', ARGUMENTS, 'first'),
        tool('booking_search_options', changed, 'different'),
        build_llm_response(content='Какое время вам удобно?'))
    assert (await execute(api_client, value)).json()['status'] == 'DRAFT_READY'
    assert len(searches) == 2 and calls['execute'] == 0
    assert 'booking_search_options' in {t['function']['name'] for t in model.chat.completions.calls[-1]['tools']}


@pytest.mark.integration
async def test_ignored_retirement_cannot_send_third_compound_read(api_client, db_session, monkeypatch):
    value, _, calls, model, searches = await install(api_client, db_session, monkeypatch, 'VOICE')
    model.chat.completions.queue(*(tool('booking_search_options', ARGUMENTS, f'repeat-{i}') for i in range(20)))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'FAILED' and len(model.chat.completions.calls) == 3
    assert len(searches) == 2 and calls['execute'] == 0
