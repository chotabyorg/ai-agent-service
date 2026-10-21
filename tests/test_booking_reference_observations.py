"""Reference provenance distinguishes selection errors without changing effects."""
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import func, select

from core.managed_reference_observations import booking_reference_sources, observe_read_references
from business_capabilities.registry import normalize_arguments
from models.business import BusinessOperation, BusinessPlan
from tests.fakes import build_llm_response
from tests.test_chotacrm_managed_proposal_errors import existing_customer_lookup, install_managed_crm
from tests.test_managed_turns import execute
from tests.test_preparation_schema_correction import preparation


def remember(runtime, role, refs, *, status='OK'):
    data = {'match': 'SINGLE', 'candidates': [{'ref': ref} for ref in refs]} if role == 'customer' else {
        'items': [{'ref': ref} for ref in refs]}
    capability = {'offering': 'catalog.search', 'resource': 'resource.search', 'customer': 'customer.find'}[role]
    observe_read_references(runtime, capability, {'status': status, 'data': data})


def test_reference_observations_are_private_bounded_and_turn_local():
    first, second = SimpleNamespace(), SimpleNamespace()
    for role in ('offering', 'resource', 'customer'):
        remember(first, role, [role + '-private-sentinel'])
    arguments = {role + '_ref': role + '-private-sentinel' for role in ('offering', 'resource', 'customer')}
    sources = booking_reference_sources(first, 'booking_create', arguments)
    assert set(sources.values()) == {'READ_THIS_TURN'}
    assert set(booking_reference_sources(second, 'booking_create', arguments).values()) == {'NOT_OBSERVED_THIS_TURN'}
    assert 'private-sentinel' not in repr(first._reference_observations)
    swapped = dict(arguments, resource_ref=arguments['offering_ref'])
    assert booking_reference_sources(first, 'business_propose', {'actions': [
        {'capability': 'booking.create', 'arguments': swapped}]} )['resource_ref_source'] == 'WRONG_KIND_THIS_TURN'
    for page in range(6):
        remember(first, 'resource', [f'bounded-{page}-{i}' for i in range(20)])
    bucket = first._reference_observations['resource']
    assert len(bucket['digests']) == 96 and bucket['limited']
    assert booking_reference_sources(first, 'booking_create', {'resource_ref': 'not-retained'})['resource_ref_source'] == 'OBSERVATION_LIMIT'
    assert booking_reference_sources(first, 'booking_create', arguments)['resource_ref_source'] == 'READ_THIS_TURN'


def test_failed_ambiguous_or_unsupported_reads_are_not_reference_evidence():
    runtime = SimpleNamespace()
    remember(runtime, 'resource', ['failed'], status='ERROR')
    remember(runtime, 'customer', ['ambiguous-one', 'ambiguous-two'])
    observe_read_references(runtime, 'booking.get', {'status': 'OK', 'data': {'ref': 'not-a-resource'}})
    assert not hasattr(runtime, '_reference_observations')
    assert booking_reference_sources(runtime, 'booking_create', {'customer': {'display_name': 'Private'}}) == {
        'offering_ref_source': 'NOT_SUPPLIED', 'resource_ref_source': 'NOT_SUPPLIED', 'customer_ref_source': 'NOT_SUPPLIED'}
    assert set(booking_reference_sources(runtime, 'business_propose', {'actions': [
        {'capability': 'booking.cancel', 'arguments': {'booking_ref': 'private'}}]}).values()) == {'NOT_APPLICABLE'}


@pytest.mark.integration
@pytest.mark.parametrize('channel', ['VOICE', 'WEBSITE_WIDGET'])
@pytest.mark.parametrize(('resource_ref', 'source'), [
    ('resource_test', 'READ_THIS_TURN'),
    ('offering_test', 'WRONG_KIND_THIS_TURN'),
    ('resource_typo', 'NOT_OBSERVED_THIS_TURN'),
])
async def test_not_found_preserves_effect_guards_and_records_only_ref_categories(
        api_client, db_session, monkeypatch, channel, resource_ref, source):
    value, arguments, registry, calls, effects, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200,
        {'status': 'ERROR', 'error': {'code': 'NOT_FOUND', 'message': 'synthetic', 'retry': 'NEVER'}},
        lookup_response=existing_customer_lookup(), channel=channel)
    client = registry.get('chotacrm').adapter.client
    original = client.transport

    async def read(request):
        if request.url.path.endswith('/execute/catalog.search'):
            return httpx.Response(200, json={'status': 'OK', 'data': {'items': [{
                'ref': 'offering_test', 'name': 'Synthetic offering', 'type': 'SERVICE',
                'price': '22.00', 'currency': 'BYN', 'active': True}]}})
        if request.url.path.endswith('/execute/resource.search'):
            return httpx.Response(200, json={'status': 'OK', 'data': {'items': [{
                'ref': 'resource_test', 'name': 'Synthetic resource', 'kind': 'PERSON', 'active': True}]}})
        return await original.handle_async_request(request)

    client.transport = httpx.MockTransport(read)
    observed = []
    monkeypatch.setattr('core.managed_business_diagnostics.emit_business_diagnostic', observed.append)
    arguments = dict(arguments, resource_ref=resource_ref)
    corrected = {key: val for key, val in arguments.items() if key != 'customer'}
    corrected['customer_ref'] = 'customer_existing'
    model.chat.completions.queue(
        preparation('booking_search_options', {'catalog': {}, 'resources': {}}, 'search'),
        preparation('booking_create', arguments, 'inline-customer'),
        preparation('booking_create', corrected, 'exact-customer'),
        build_llm_response(content='Предложение подготовлено. Подтверждаете?'))
    response = await execute(api_client, value)
    assert response.status_code == 200 and response.json()['status'] == 'DRAFT_READY'
    assert 'Не удалось подготовить предложение' in response.json()['draft_text']
    proposals = [event for event in observed if event.stage == 'PROPOSAL']
    assert len(proposals) == 1 and proposals[0].refusal_category == 'NOT_FOUND'
    event = proposals[0]
    assert event.resource_ref_source == source
    assert event.offering_ref_source == event.customer_ref_source == 'READ_THIS_TURN'
    assert not event.proposal_present and not event.operation_present
    assert all(ref not in repr(observed) for ref in ('offering_test', 'resource_test', 'customer_existing', '+375291112233'))
    assert calls['lookup'] == calls['facts'] == 1 and calls['execute'] == 0
    assert calls['fact_arguments'] == [normalize_arguments('booking.create', corrected)]
    assert not effects.permit_requests
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0
