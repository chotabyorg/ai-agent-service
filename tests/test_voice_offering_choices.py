"""Fresh catalog choices guide the model without admitting or repairing a write."""
import json

import httpx
import pytest
from sqlalchemy import select

import main
from models.business import BusinessOperation, BusinessPlan
from schemas.managed_turn import ManagedTurnRequest
from services.llm_service import LLMService
from tests.fakes import build_llm_response
from tests.test_booking_search_options import ARGUMENTS
from tests.test_booking_search_options_managed import install
from tests.test_booking_workflow import tool
from tests.test_business_runtime import later
from tests.test_managed_turns import execute


def offering_schema(call):
    return next(t for t in call['tools'] if t['function']['name'] == 'booking_create')[
        'function']['parameters']['properties']['offering_ref']


@pytest.mark.integration
@pytest.mark.parametrize('channel', ['VOICE', 'WEBSITE_WIDGET'])
async def test_combined_read_advertises_real_choices_without_mutating_canonical_schema(
        api_client, db_session, monkeypatch, channel):
    value, arguments, calls, model, _ = await install(api_client, db_session, monkeypatch, channel)
    model.chat.completions.queue(tool('booking_search_options', ARGUMENTS, 'search'),
                                 tool('booking_create', arguments, 'prepare'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY' and calls['execute'] == 0
    assert 'enum' not in offering_schema(model.chat.completions.calls[0])
    actual = offering_schema(model.chat.completions.calls[1]).get('enum')
    assert actual == (['offering_test'] if channel == 'VOICE' else None)
    from business_capabilities.registry import REGISTRY
    assert 'enum' not in REGISTRY['booking.create'].tool()['function']['parameters']['properties']['offering_ref']


@pytest.mark.integration
@pytest.mark.parametrize('second_refs', [[], ['offering_second', 'offering_other']])
async def test_new_catalog_search_replaces_choices_including_an_empty_result(
        api_client, db_session, monkeypatch, second_refs):
    value, _, calls, model, _ = await install(api_client, db_session, monkeypatch)
    client = main.app.state.business_registry.get('chotacrm').adapter.client
    original = client.transport
    async def handle(request):
        if request.url.path.endswith('/execute/catalog.search'):
            query = json.loads(request.content)['arguments']['query']
            refs = second_refs if query == 'другая услуга' else ['offering_first']
            return httpx.Response(200, json={'status': 'OK', 'data': {'items': [dict(
                ref=ref, name='Услуга', type='SERVICE', active=True, price='50.00', currency='BYN')
                for ref in refs]}})
        return await original.handle_async_request(request)
    client.transport = httpx.MockTransport(handle)
    model.chat.completions.queue(tool('catalog_search', {'query': 'первая услуга'}, 'first'),
        tool('catalog_search', {'query': 'другая услуга'}, 'second'),
        build_llm_response(content='Какую услугу выбираете?'))
    assert (await execute(api_client, value)).json()['status'] == 'DRAFT_READY'
    assert offering_schema(model.chat.completions.calls[1])['enum'] == ['offering_first']
    assert offering_schema(model.chat.completions.calls[2]).get('enum') == (second_refs or None)
    assert calls['execute'] == 0


@pytest.mark.integration
async def test_model_ignoring_choices_reaches_fresh_crm_without_argument_repair(
        api_client, db_session, monkeypatch):
    value, arguments, calls, model, _ = await install(api_client, db_session, monkeypatch)
    client = main.app.state.business_registry.get('chotacrm').adapter.client
    original, facts = client.transport, []
    async def handle(request):
        if '/facts/' in request.url.path:
            args = json.loads(request.content)['arguments']
            facts.append(args['offering_ref'])
            if args['offering_ref'] == 'invented_ref':
                return httpx.Response(200, json={'status': 'ERROR', 'error': {
                    'code': 'NOT_FOUND', 'message': 'synthetic', 'retry': 'NEVER'}})
        return await original.handle_async_request(request)
    client.transport = httpx.MockTransport(handle)
    model.chat.completions.queue(tool('booking_search_options', ARGUMENTS, 'search'),
        tool('booking_create', {**arguments, 'offering_ref': 'invented_ref'}, 'bad'),
        tool('booking_create', arguments, 'corrected'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY' and 'Подтверждаете' in result['draft_text']
    assert facts == ['invented_ref', 'offering_test'] and calls['execute'] == 0
    bad = next(m for m in model.chat.completions.calls[2]['messages'] if m.get('tool_call_id') == 'bad')
    assert json.loads(bad['content'])['error']['code'] == 'NOT_FOUND'
    async with db_session() as db:
        plans = (await db.scalars(select(BusinessPlan))).all()
        operations = (await db.scalars(select(BusinessOperation))).all()
        assert len(plans) == len(operations) == 1
        assert operations[0].status == 'RESERVED' and operations[0].attempt_count == 0


@pytest.mark.integration
async def test_choices_do_not_carry_into_a_later_turn(api_client, db_session, monkeypatch):
    value, _, calls, model, _ = await install(api_client, db_session, monkeypatch)
    model.chat.completions.queue(tool('booking_search_options', ARGUMENTS, 'search'),
        build_llm_response(content='Какое время вам удобно?'))
    assert (await execute(api_client, value)).json()['status'] == 'DRAFT_READY'
    following = later(ManagedTurnRequest.model_validate(value), 'А какие есть другие услуги?')
    model.chat.completions.queue(build_llm_response(content='Уточните, какая услуга нужна.'))
    assert (await execute(api_client, following.model_dump(mode='json', exclude_unset=True))).json()['status'] == 'DRAFT_READY'
    assert 'enum' not in offering_schema(model.chat.completions.calls[-1]) and calls['execute'] == 0


@pytest.mark.integration
async def test_added_choice_bytes_are_budgeted_before_next_model_request(
        api_client, db_session, monkeypatch):
    value, _, calls, model, _ = await install(api_client, db_session, monkeypatch)
    original = LLMService._check_managed_envelope
    checked = []
    def check(self, agent, messages, tools, **kwargs):
        selected = offering_schema({'tools': tools})
        if selected.get('enum'):
            checked.append(selected['enum'])
            envelope = self._managed_envelope(agent, messages, tools, **kwargs)
            size = len(json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode('utf-8'))
            monkeypatch.setattr(self.settings, 'managed_ai_context_bytes', size - 1)
        return original(self, agent, messages, tools, **kwargs)
    monkeypatch.setattr(LLMService, '_check_managed_envelope', check)
    model.chat.completions.queue(tool('booking_search_options', ARGUMENTS, 'search'),
        build_llm_response(content='Какое время вам удобно?'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'FAILED' and result['error_code'] == 'CONTEXT_LIMIT'
    assert checked == [['offering_test']] and len(model.chat.completions.calls) == 1
    assert calls['execute'] == 0
