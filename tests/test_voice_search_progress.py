"""Voice search must make progress into a confirmed booking, not burn 20 model calls."""
import json
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from core.config import get_settings
from models.agent import AgentTemplate
from models.business import BusinessOperation, BusinessPlan
from schemas.managed_turn import ManagedTurnRequest
from tests.fakes import build_llm_response
from tests.test_booking_workflow import tool
from tests.test_business_runtime import later
from tests.test_chotacrm_managed_proposal_errors import install_managed_crm
from tests.test_managed_turns import execute
from tests.test_voice_latency import profile


async def install(api_client, db_session, monkeypatch, channel="VOICE"):
    value, arguments, registry, calls, effect, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, channel=channel, allow_execute=True)
    async with db_session() as db:
        agent = await db.get(AgentTemplate, UUID(value['agent_id']))
        monkeypatch.setattr(get_settings(), 'managed_voice_latency_profiles',
                            [profile(value, agent.model_name)])
    client = registry.get('chotacrm').adapter.client
    original = client.transport
    searches = []

    async def handle(request):
        if request.url.path.endswith('/execute/resource.search'):
            query = json.loads(request.content)['arguments']
            searches.append(query)
            items = ([] if query.get('query') else [dict(
                ref='resource_test', name='Анна', kind='PERSON', active=True)])
            result = {'status': 'OK', 'data': {'items': items}}
            if query.get('limit') == 1 and query.get('cursor') != 'issued-page-three':
                result['next_cursor'] = ('issued-page-three' if query.get('cursor')
                                         else 'issued-page-two')
            return httpx.Response(200, json=result)
        return await original.handle_async_request(request)

    client.transport = httpx.MockTransport(handle)
    return value, arguments, calls, model, searches


@pytest.mark.integration
async def test_changed_search_can_reach_confirmed_booking(
        api_client, db_session, monkeypatch):
    value, arguments, calls, model, searches = await install(api_client, db_session, monkeypatch)
    model.chat.completions.queue(
        tool('resource_search', {'query': 'стрижка'}, 'service-name'),
        tool('resource_search', {'query': 'мужская'}, 'broader-name'),
        tool('resource_search', {}, 'browse'),
        tool('booking_create', arguments, 'prepare'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY', result
    assert [q['query'] for q in searches] == ['стрижка', 'мужская', '']
    browse = next(m for m in model.chat.completions.calls[-1]['messages']
                  if m.get('tool_call_id') == 'browse')
    assert json.loads(browse['content'])['data']['items'][0]['ref'] == 'resource_test'
    assert calls['execute'] == 0 and 'Подтверждаете' in result['draft_text']
    async with db_session() as db:
        proposal_id = str((await db.scalar(select(BusinessPlan))).id)
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
async def test_three_distinct_pages_remain_available(api_client, db_session, monkeypatch):
    value, _, _, model, searches = await install(api_client, db_session, monkeypatch)
    model.chat.completions.queue(
        tool('resource_search', {'limit': 1}, 'page-one'),
        tool('resource_search', {'limit': 1, 'cursor': 'issued-page-two'}, 'page-two'),
        tool('resource_search', {'limit': 1, 'cursor': 'issued-page-three'}, 'page-three'),
        build_llm_response(content='Нашёл специалистов. Какое время вам удобно?'))
    assert (await execute(api_client, value)).json()['status'] == 'DRAFT_READY'
    assert [q.get('cursor') for q in searches] == [None, 'issued-page-two', 'issued-page-three']
    assert 'resource_search' in [t['function']['name'] for t in model.chat.completions.calls[-1]['tools']]


@pytest.mark.integration
@pytest.mark.parametrize('channel', ['VOICE', 'WEBSITE_WIDGET'])
async def test_identical_voice_search_is_retired_but_keeps_real_results(
        api_client, db_session, monkeypatch, channel):
    value, _, _, model, searches = await install(api_client, db_session, monkeypatch, channel)
    model.chat.completions.queue(tool('resource_search', {}, 'first'),
        tool('resource_search', {'query': '', 'limit': 10}, 'same-with-defaults'),
        build_llm_response(content='Есть специалист Анна. Во сколько вам удобно?'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY', result
    assert len(searches) == 2
    advertised = [t['function']['name'] for t in model.chat.completions.calls[-1]['tools']]
    assert ('resource_search' in advertised) is (channel != 'VOICE')
    for call_id in ('first', 'same-with-defaults'):
        message = next(m for m in model.chat.completions.calls[-1]['messages']
                       if m.get('tool_call_id') == call_id)
        assert json.loads(message['content'])['data']['items'][0]['name'] == 'Анна'


@pytest.mark.integration
async def test_model_ignoring_retirement_stops_before_twenty_round_trips(
        api_client, db_session, monkeypatch):
    value, _, calls, model, searches = await install(api_client, db_session, monkeypatch)
    model.chat.completions.queue(*(tool('resource_search', {}, f'repeat-{i}') for i in range(20)))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'FAILED'
    assert len(model.chat.completions.calls) == 3
    assert len(searches) == 2 and calls['execute'] == 0
