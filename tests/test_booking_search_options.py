"""Combined selection keeps the two existing guarded READ boundaries and no effect."""
import asyncio
import json
from dataclasses import replace

import anyio
import httpx
import pytest
from sqlalchemy import func, select

from business_capabilities.registry import CAPABILITIES, REGISTRY, WIRE_REGISTRY
from models.business import BusinessOperation, BusinessPlan
from models.business_connection import BusinessConnection
from schemas.business import BusinessSafetyError, BusinessToolResult
from services.booking_search_options import normalize_search_arguments, search_tool
from services.managed_preparation import PreparationSchema
from services.managed_tools import ManagedExecutionError
from tests.fakes import ScriptedLLMClient, build_llm_response
from tests.test_chotacrm_adapter import crm_settings, setup
from tests.test_pending_proposal_decision import run_core
from tests.test_preparation_schema_correction import local_runtime, preparation


ARGUMENTS = {'catalog': {'query': 'стрижка', 'type': 'SERVICE'}, 'resources': {}}


def install_reads(business, *, stop=None):
    client = business.registration.adapter.client
    original = client.transport
    reads = []

    async def handle(request):
        capability = request.url.path.rsplit('/', 1)[-1]
        if capability in {'catalog.search', 'resource.search'}:
            arguments = json.loads(request.content)['arguments']
            reads.append((capability, arguments))
            if stop is not None:
                response = await stop(capability)
                if response is not None:
                    return response
            item = (dict(ref='offering_test', name='Стрижка', type='SERVICE', active=True,
                         price='50.00', currency='BYN') if capability == 'catalog.search'
                    else dict(ref='resource_test', name='Анна', kind='PERSON', active=True))
            return httpx.Response(200, json={'status': 'OK', 'data': {'items': [item]},
                                            'next_cursor': 'issued-next-page'})
        return await original.handle_async_request(request)

    client.transport = httpx.MockTransport(handle)
    return reads


def test_closed_nested_schemas_keep_canonical_registry_unchanged():
    parameters = search_tool()['function']['parameters']
    assert parameters['additionalProperties'] is False
    assert set(parameters['required']) == {'catalog', 'resources'}
    for key, capability in [('catalog', 'catalog.search'), ('resources', 'resource.search')]:
        assert parameters['properties'][key] == REGISTRY[capability].tool()['function']['parameters']
    assert len(CAPABILITIES) == len(WIRE_REGISTRY) == 18
    assert 'booking_search_options' not in WIRE_REGISTRY
    assert PreparationSchema(parameters).feedback(ARGUMENTS) is None


@pytest.mark.asyncio
async def test_two_sequential_real_reads_keep_refs_pages_guards_deadline_and_no_plan(
        db_session, crm_settings, monkeypatch):
    business, runtime, provider, _ = await local_runtime(db_session, crm_settings, monkeypatch)
    reads = install_reads(business)
    guard_calls = []
    original_guard = business.execution_guard
    async def guard(**kwargs):
        guard_calls.append(len(reads))
        return await original_guard(**kwargs)
    monkeypatch.setattr(business, 'execution_guard', guard)
    deadline = runtime.deadline
    result = await business.call_tool('booking_search_options', ARGUMENTS)
    assert result.halt_status is result.safe_draft is None
    assert result.tool_payload['status'] == 'OK'
    assert [name for name, _ in reads] == ['catalog.search', 'resource.search']
    assert reads[0][1] == {'query': 'стрижка', 'type': 'SERVICE', 'limit': 10}
    assert reads[1][1] == {'query': '', 'limit': 10}
    assert guard_calls == [0, 1, 1, 2]
    for key, ref in [('catalog', 'offering_test'), ('resources', 'resource_test')]:
        assert result.tool_payload[key]['data']['items'][0]['ref'] == ref
        assert result.tool_payload[key]['next_cursor'] == 'issued-next-page'
    assert runtime.deadline == deadline and runtime.active_scope is None
    assert provider['posts'] == 0
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid', [
    {'catalog': {}}, {'resources': {}}, {'catalog': {}, 'resources': {}, 'extra': 1},
    {'catalog': {'workspace_id': 'injected'}, 'resources': {}},
    {'catalog': {}, 'resources': {'offering_ref': 'injected'}},
    {'catalog': {}, 'resources': {'limit': 0}},
    {'catalog': {}, 'resources': None},
])
async def test_all_arguments_are_validated_before_either_read(db_session, crm_settings, invalid):
    business, _, provider, _ = await setup(db_session, crm_settings)
    reads = install_reads(business)
    result = await business.call_tool('booking_search_options', invalid)
    assert result.halt_status == 'FAILED' and result.error_code == 'TOOL_SETUP_FAILED'
    assert reads == [] and provider['posts'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('capability', ['catalog.search', 'resource.search'])
async def test_not_advertised_or_callable_without_both_capabilities(db_session, crm_settings, capability):
    business, _, provider, _ = await setup(db_session, crm_settings)
    business.binding = replace(business.binding,
        enabled_capabilities=tuple(c for c in business.binding.enabled_capabilities if c != capability))
    assert 'booking_search_options' not in {t['function']['name'] for t in business.openai_tools()}
    with pytest.raises(BusinessSafetyError, match='TOOL_UNAVAILABLE'):
        await business.call_tool('booking_search_options', ARGUMENTS)
    assert provider['posts'] == 0


@pytest.mark.asyncio
async def test_recovery_has_no_compound_tool_but_keeps_existing_reads(db_session, crm_settings):
    business, _, _, _ = await setup(db_session, crm_settings)
    business.recovery_mode = True
    names = {t['function']['name'] for t in business.openai_tools()}
    assert {'catalog_search', 'resource_search', 'business_operation_status'} <= names
    assert 'booking_search_options' not in names


@pytest.mark.asyncio
@pytest.mark.parametrize('failed_read', ['catalog.search', 'resource.search'])
async def test_provider_error_is_preserved_never_returns_partial_success(
        db_session, crm_settings, failed_read):
    business, _, provider, _ = await setup(db_session, crm_settings)
    async def fail(capability):
        if capability == failed_read:
            return httpx.Response(503)
    reads = install_reads(business, stop=fail)
    result = await business.call_tool('booking_search_options', ARGUMENTS)
    assert result.tool_payload['status'] == 'ERROR'
    assert result.tool_payload['error']['code'] == 'PROVIDER_UNAVAILABLE'
    assert 'catalog' not in result.tool_payload and 'resources' not in result.tool_payload
    assert len(reads) == (1 if failed_read == 'catalog.search' else 2)
    assert provider['posts'] == 0


@pytest.mark.asyncio
async def test_fresh_authority_change_between_reads_blocks_second_provider_call(
        db_session, crm_settings, monkeypatch):
    business, _, provider, _ = await setup(db_session, crm_settings)
    reads = install_reads(business)
    original = business.call_tool
    async def revoke_after_first(name, arguments):
        result = await original(name, arguments)
        if name == 'catalog_search':
            async with db_session() as db:
                row = await db.get(BusinessConnection, business.binding.connection_id)
                row.active = False
                await db.commit()
        return result
    monkeypatch.setattr(business, 'call_tool', revoke_after_first)
    result = await business.call_tool('booking_search_options', ARGUMENTS)
    assert result.halt_status in {'FAILED', 'UNKNOWN'}
    assert len(reads) == 1 and provider['posts'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('stop_kind', ['cancel', 'deadline'])
async def test_same_owner_deadline_or_cancellation_between_reads_never_starts_second_read(
        db_session, crm_settings, monkeypatch, stop_kind):
    business, runtime, provider, _ = await local_runtime(db_session, crm_settings, monkeypatch)
    reads = install_reads(business)
    original = business.call_tool
    async def stop_after_first(name, arguments):
        result = await original(name, arguments)
        if name == 'catalog_search':
            if stop_kind == 'cancel':
                runtime.signal_cancel()
            else:
                runtime.deadline = anyio.current_time() - 1
        return result
    monkeypatch.setattr(business, 'call_tool', stop_after_first)
    error = asyncio.CancelledError if stop_kind == 'cancel' else ManagedExecutionError
    with pytest.raises(error):
        await business.call_tool('booking_search_options', ARGUMENTS)
    assert len(reads) == 1 and provider['posts'] == 0 and runtime.active_scope is None


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked', [BusinessToolResult(halt_status='UNKNOWN'),
    BusinessToolResult(safe_draft='Не удалось проверить данные.')])
async def test_first_read_guard_result_is_returned_exactly_without_second_read(
        db_session, crm_settings, monkeypatch, blocked):
    business, _, _, _ = await setup(db_session, crm_settings)
    seen = []
    original = business.call_tool
    async def block(name, arguments):
        if name != 'booking_search_options':
            seen.append(name)
            return blocked
        return await original(name, arguments)
    monkeypatch.setattr(business, 'call_tool', block)
    assert await business.call_tool('booking_search_options', ARGUMENTS) is blocked
    assert seen == ['catalog_search']


@pytest.mark.asyncio
async def test_nested_input_gets_one_model_correction_before_any_read(
        db_session, crm_settings, monkeypatch):
    business, runtime, provider, _ = await local_runtime(db_session, crm_settings, monkeypatch)
    reads = install_reads(business)
    model = ScriptedLLMClient()
    model.chat.completions.queue(
        preparation('booking_search_options', {'catalog': {}, 'resources': {'service': 'injected'}}, 'bad'),
        preparation('booking_search_options', ARGUMENTS, 'corrected'),
        build_llm_response(content='Есть стрижка и специалист Анна. Во сколько вам удобно?'))
    original = model.chat.completions.create
    async def check_no_early_dispatch(**kwargs):
        if len(model.chat.completions.calls) < 2:
            assert reads == []
        return await original(**kwargs)
    monkeypatch.setattr(model.chat.completions, 'create', check_no_early_dispatch)
    assert 'Анна' in await run_core(db_session, runtime, model)
    assert len(reads) == 2 and provider['posts'] == 0
    assert [t['function']['name'] for t in model.chat.completions.calls[1]['tools']] == ['booking_search_options']


def test_both_queries_keep_independent_cursor_and_normalization():
    arguments = {'catalog': {'query': 'стр', 'limit': 1, 'cursor': 'catalog-page'},
                 'resources': {'query': 'Анна', 'limit': 2, 'cursor': 'resource-page'}}
    assert normalize_search_arguments(arguments) == arguments
