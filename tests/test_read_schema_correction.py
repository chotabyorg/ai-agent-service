"""Malformed read input is corrected once without dispatch or invented results."""
import json

import httpx
import pytest
from sqlalchemy import select

from models.business import BusinessOperation
from tests.fakes import build_llm_response
from tests.test_booking_workflow import tool
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
    feedback = next(m for m in correction['messages'] if m.get('tool_call_id') == 'invalid')
    payload = json.loads(feedback['content'])
    assert payload['error']['code'] == 'TOOL_ARGUMENT_VALIDATION_FAILED'
    assert 'private-sentinel' not in feedback['content']
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
