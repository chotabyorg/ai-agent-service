"""Early identity READ removes a model round without preparing or consenting."""
from dataclasses import replace
import json

import httpx
import pytest
from sqlalchemy import func, select

from models.business import BusinessOperation, BusinessPlan
from models.business_connection import BusinessConnection
from schemas.managed_turn import ManagedTurnRequest
from services.booking_search_options import search_tool
from services.managed_preparation import PreparationSchema
from tests.test_booking_search_options import ARGUMENTS, install_reads
from tests.test_booking_workflow import tool
from tests.test_business_runtime import later
from tests.test_chotacrm_adapter import crm_settings as _crm_settings, setup
from tests.test_chotacrm_managed_proposal_errors import existing_customer_lookup, install_managed_crm
from tests.test_managed_turns import execute


CUSTOMER = {'display_name': 'Влад', 'identity': {'kind': 'PHONE', 'value': '+375291112233'}}
crm_settings = _crm_settings


async def managed_setup(api_client, db_session, monkeypatch, channel, lookup):
    value, arguments, registry, calls, effect, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, lookup_response=lookup,
        allow_execute=True, channel=channel)
    client = registry.get('chotacrm').adapter.client
    original = client.transport
    reads = []

    async def handle(request):
        capability = request.url.path.rsplit('/', 1)[-1]
        if '/execute/' in request.url.path:
            reads.append(capability)
        if capability in {'catalog.search', 'resource.search'}:
            item = (dict(ref='offering_test', name='Стрижка', type='SERVICE', active=True,
                         price='50.00', currency='BYN') if capability == 'catalog.search'
                    else dict(ref='resource_test', name='Анна', kind='PERSON', active=True))
            return httpx.Response(200, json={'status': 'OK', 'data': {'items': [item]}})
        return await original.handle_async_request(request)

    client.transport = httpx.MockTransport(handle)
    return value, arguments, calls, effect, model, reads


@pytest.mark.integration
@pytest.mark.parametrize('channel', ['VOICE', 'WEBSITE_WIDGET'])
async def test_existing_contact_search_and_explicit_proposal_take_two_model_rounds_then_one_consent(
        api_client, db_session, monkeypatch, channel):
    value, arguments, calls, effect, model, reads = await managed_setup(
        api_client, db_session, monkeypatch, channel, existing_customer_lookup())
    corrected = {key: val for key, val in arguments.items() if key != 'customer'}
    corrected['customer_ref'] = 'customer_existing'
    model.chat.completions.queue(
        tool('booking_search_options', {**ARGUMENTS, 'customer': arguments['customer']}, 'search'),
        tool('booking_create', corrected, 'explicit-proposal'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY' and 'Подтверждаете' in result['draft_text'], result
    assert len(model.chat.completions.calls) == 2
    assert reads[:3] == ['catalog.search', 'resource.search', 'customer.find']
    assert calls['lookup'] == 1 and calls['execute'] == 0 and not effect.permit_requests
    message = next(m for m in model.chat.completions.calls[1]['messages'] if m.get('tool_call_id') == 'search')
    assert json.loads(message['content'])['customer']['data']['candidates'][0]['ref'] == 'customer_existing'
    assert not any('preparation is INCOMPLETE' in str(m.get('content', ''))
                   for m in model.chat.completions.calls[1]['messages'])
    async with db_session() as db:
        plan = (await db.scalars(select(BusinessPlan))).one()
        operation = (await db.scalars(select(BusinessOperation))).one()
        assert plan.proposal_state == 'PREPARED' and plan.confirmed_turn_id is None
        assert operation.status == 'RESERVED' and operation.attempt_count == 0
        assert operation.request_payload['arguments']['customer_ref'] == 'customer_existing'
        proposal_id = str(plan.id)
    consent = later(ManagedTurnRequest.model_validate(value), 'Да, подтверждаю')
    model.chat.completions.queue(tool('business_decide_proposal',
        {'proposal_id': proposal_id, 'intent': 'CONFIRM'}, 'consent'))
    response = await execute(api_client, consent.model_dump(mode='json', exclude_unset=True))
    assert response.json()['status'] == 'DRAFT_READY'
    assert calls['execute'] == 1
    assert (await execute(api_client, consent.model_dump(mode='json', exclude_unset=True))).json() == response.json()
    assert calls['execute'] == 1


@pytest.mark.integration
async def test_none_is_read_again_during_inline_preparation_never_cached_as_authority(
        api_client, db_session, monkeypatch):
    lookup = {'status': 'OK', 'data': {'match': 'NONE', 'candidates': []}}
    value, arguments, calls, effect, model, _ = await managed_setup(
        api_client, db_session, monkeypatch, 'VOICE', lookup)
    model.chat.completions.queue(
        tool('booking_search_options', {**ARGUMENTS, 'customer': arguments['customer']}, 'search'),
        tool('booking_create', arguments, 'prepare'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY' and 'Подтверждаете' in result['draft_text'], result
    assert len(model.chat.completions.calls) == 2 and calls['lookup'] == 2
    assert calls['execute'] == 0 and not effect.permit_requests


@pytest.mark.integration
@pytest.mark.parametrize('lookup', [
    existing_customer_lookup(name='Другое имя'),
    existing_customer_lookup(archived=True),
    {'status': 'AMBIGUOUS', 'data': {'match': 'AMBIGUOUS', 'candidates': []}},
    {'status': 'ERROR', 'error': {'code': 'PROVIDER_UNAVAILABLE'}},
])
async def test_early_lookup_conflict_or_failure_never_becomes_none_or_a_proposal(
        api_client, db_session, monkeypatch, lookup):
    value, arguments, calls, effect, model, _ = await managed_setup(
        api_client, db_session, monkeypatch, 'VOICE', lookup)
    model.chat.completions.queue(
        tool('booking_search_options', {**ARGUMENTS, 'customer': arguments['customer']}, 'search'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY', result
    assert 'Подтверждаете' not in result['draft_text']
    assert len(model.chat.completions.calls) == calls['lookup'] == 1
    assert calls['facts'] == calls['execute'] == 0 and not effect.permit_requests
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('customer', [
    None, {}, {**CUSTOMER, 'display_name': ''}, {**CUSTOMER, 'workspace_id': 'untrusted'},
    {**CUSTOMER, 'identity': {'kind': 'OTHER', 'value': 'not-exact'}},
    {**CUSTOMER, 'identity': {**CUSTOMER['identity'], 'extra': 1}},
])
async def test_invalid_customer_is_rejected_before_any_of_the_three_reads(db_session, crm_settings, customer):
    business, _, provider, _ = await setup(db_session, crm_settings)
    reads = install_reads(business)
    result = await business.call_tool('booking_search_options', {**ARGUMENTS, 'customer': customer})
    assert result.halt_status == 'FAILED' and result.error_code == 'TOOL_SETUP_FAILED'
    assert reads == [] and provider['posts'] == 0


@pytest.mark.asyncio
async def test_customer_option_is_absent_and_uncallable_without_read_capability(db_session, crm_settings):
    business, _, provider, _ = await setup(db_session, crm_settings)
    business.binding = replace(business.binding,
        enabled_capabilities=tuple(c for c in business.binding.enabled_capabilities if c != 'customer.find'))
    advertised = next(t for t in business.openai_tools() if t['function']['name']=='booking_search_options')
    assert 'customer' not in advertised['function']['parameters']['properties']
    reads = install_reads(business)
    result = await business.call_tool('booking_search_options', {**ARGUMENTS, 'customer': CUSTOMER})
    assert result.halt_status == 'FAILED' and result.error_code == 'TOOL_SETUP_FAILED'
    assert reads == [] and provider['posts'] == 0


@pytest.mark.asyncio
async def test_revocation_after_resources_prevents_customer_read(db_session, crm_settings, monkeypatch):
    business, _, provider, _ = await setup(db_session, crm_settings)
    reads = install_reads(business)
    client = business.registration.adapter.client
    transport = client.transport
    customer_reads = []

    async def observe(request):
        if request.url.path.endswith('/execute/customer.find'):
            customer_reads.append(request)
        return await transport.handle_async_request(request)

    client.transport = httpx.MockTransport(observe)
    original = business.call_tool

    async def revoke(name, arguments):
        result = await original(name, arguments)
        if name == 'resource_search':
            async with db_session() as db:
                connection = await db.get(BusinessConnection, business.binding.connection_id)
                connection.active = False
                await db.commit()
        return result

    monkeypatch.setattr(business, 'call_tool', revoke)
    result = await business.call_tool('booking_search_options', {**ARGUMENTS, 'customer': CUSTOMER})
    assert result.halt_status in {'FAILED', 'UNKNOWN'} or result.safe_draft is not None
    assert not result.tool_payload
    assert [name for name, _ in reads] == ['catalog.search', 'resource.search']
    assert customer_reads == []
    assert provider['posts'] == 0


def test_customer_schema_is_closed_and_ordinary_two_read_schema_is_preserved():
    ordinary, early = search_tool(), search_tool(include_customer=True)
    plain = ordinary['function']['parameters']
    parameters = early['function']['parameters']
    assert set(plain['properties']) == {'catalog', 'resources'}
    assert parameters['required'] == plain['required'] == ['catalog', 'resources']
    assert PreparationSchema(parameters).feedback({**ARGUMENTS, 'customer': CUSTOMER}) is None
    assert PreparationSchema(plain).feedback({**ARGUMENTS, 'customer': CUSTOMER}) is not None
