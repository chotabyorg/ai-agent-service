"""Substage observations keep the real ledger/READ path and failure boundaries."""
import asyncio

import pytest

from core import managed_wait_diagnostics as diagnostics
from schemas.business import BusinessSafetyError, BusinessToolResult
from services.business_runtime import BusinessRuntime
from services.managed_runtime import ManagedRuntime
from tests.business_effect_helpers import FakeConversationEffectPort
from tests.test_chotacrm_adapter import crm_settings, setup  # noqa: F401


def read_stages(rows):
    return [(r.phase, r.state, r.step) for r in rows if r.phase.startswith('READ_')]


@pytest.mark.asyncio
async def test_open_observes_existing_startup_calls_without_acquiring_effect_authority(
        db_session, crm_settings, monkeypatch):
    original, registry, provider, _ = await setup(db_session, crm_settings)
    execution = ManagedRuntime(20, request=original.request)
    deadline, rows = execution.deadline, []
    monkeypatch.setattr(diagnostics, 'emit_managed_wait', rows.append)
    port = FakeConversationEffectPort()
    current = await BusinessRuntime.open(original.request, original.grant, db_session, registry,
        execution=execution, effect_port=port)
    phases = ['BUSINESS_BINDING_SYNC', 'BUSINESS_OPEN_CONTEXT', 'BUSINESS_RECOVER_ATTEMPTS',
        'BUSINESS_COLLECT_REFS', 'BUSINESS_INVALIDATE_PROPOSAL', 'BUSINESS_SELECT_RECOVERY']
    assert [(r.phase, r.state) for r in rows] == [
        (phase, state) for phase in phases for state in ('START', 'COMPLETED')]
    assert all(r.turn_id == original.request.turn_id and r.step == r.attempt == 1 for r in rows)
    assert execution.deadline == deadline and execution.active_scope is None
    assert current.scope == original.scope and not current.collector.snapshot()
    assert await current.repository.selected_plan(current.request, current.grant) is None
    assert not port.eligibility_requests and not port.permit_requests and provider['posts'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_startup_failure_keeps_original_exception_and_does_not_continue(
        db_session, crm_settings, monkeypatch, cancel):
    from business_capabilities import connections
    original, registry, provider, _ = await setup(db_session, crm_settings)
    execution = ManagedRuntime(20, request=original.request)
    rows, entered = [], asyncio.Event()
    error = asyncio.CancelledError('private-sentinel') if cancel else BusinessSafetyError('AUTHORITY_DENIED')

    async def blocked(*args):
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise error

    monkeypatch.setattr(connections, 'sync_business_binding', blocked)
    monkeypatch.setattr(diagnostics, 'emit_managed_wait', rows.append)
    task = asyncio.create_task(BusinessRuntime.open(original.request, original.grant, db_session,
        registry, execution=execution, effect_port=FakeConversationEffectPort()))
    await asyncio.wait_for(entered.wait(), 5)
    if cancel:
        task.cancel()
    with pytest.raises(type(error)) as caught:
        await task
    if not cancel:
        assert caught.value is error
    assert [(r.phase, r.state) for r in rows] == [
        ('BUSINESS_BINDING_SYNC', 'START'), ('BUSINESS_BINDING_SYNC', 'CANCELLED' if cancel else 'ERROR')]
    assert provider['posts'] == 0 and 'private-sentinel' not in repr(rows)


@pytest.mark.asyncio
async def test_guarded_read_order_and_ordinals_use_real_authority_and_adapter(
        db_session, crm_settings, monkeypatch):
    business, _, provider, _ = await setup(db_session, crm_settings)
    execution = business.execution = ManagedRuntime(20, request=business.request)
    deadline, rows = execution.deadline, []
    monkeypatch.setattr(diagnostics, 'emit_managed_wait', rows.append)
    for _ in range(2):
        result = await business.call_tool('catalog_search', {})
        assert result.tool_payload == {'status': 'OK', 'data': {'items': []}}
    assert read_stages(rows) == [(phase, state, step) for step in (1, 2)
        for phase in ('READ_PRE_GUARD', 'READ_AUTHORITY', 'READ_ADAPTER', 'READ_POST_GUARD')
        for state in ('START', 'COMPLETED')]
    assert provider['posts'] == 0 and not business.collector.snapshot()
    assert await business.repository.selected_plan(business.request, business.grant) is None
    assert execution.deadline == deadline and execution.active_scope is None


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['pre_guard', 'authority', 'adapter'])
async def test_read_short_circuit_or_error_keeps_the_original_boundary(
        db_session, crm_settings, monkeypatch, stage):
    from business_capabilities import runtime_tools
    from business_capabilities.context import BusinessRuntimeError
    business, _, provider, _ = await setup(db_session, crm_settings)
    business.execution = ManagedRuntime(20, request=business.request)
    rows, calls = [], []
    monkeypatch.setattr(diagnostics, 'emit_managed_wait', rows.append)
    original_adapter = business._adapter_call

    async def counted_adapter(*args):
        calls.append(args[0])
        if stage == 'adapter':
            raise BusinessRuntimeError('FACTS_UNAVAILABLE')
        return await original_adapter(*args)

    monkeypatch.setattr(business, '_adapter_call', counted_adapter)
    if stage == 'pre_guard':
        halted = BusinessToolResult(halt_status='UNKNOWN', error_code='TOOL_EXECUTION_UNKNOWN')

        async def blocked(**kwargs):
            return halted

        monkeypatch.setattr(business, 'execution_guard', blocked)
        assert await runtime_tools._call_capability_impl(business, 'catalog_search', {}) is halted
        expected = [('READ_PRE_GUARD', 'START', 1), ('READ_PRE_GUARD', 'COMPLETED', 1)]
    elif stage == 'authority':
        error = BusinessSafetyError('CAPABILITY_DISABLED')

        async def denied(*args):
            raise error

        monkeypatch.setattr(runtime_tools, '_authorize_read', denied)
        with pytest.raises(BusinessSafetyError) as caught:
            await runtime_tools._call_capability_impl(business, 'catalog_search', {})
        assert caught.value is error
        expected = [(phase, state, 1) for phase in ('READ_PRE_GUARD', 'READ_AUTHORITY')
            for state in ('START', 'ERROR' if phase == 'READ_AUTHORITY' else 'COMPLETED')]
    else:
        result = await runtime_tools._call_capability_impl(business, 'catalog_search', {})
        assert result.tool_payload['status'] == 'ERROR'
        assert result.tool_payload['error']['code'] == 'FACTS_UNAVAILABLE'
        expected = [(phase, state, 1)
            for phase in ('READ_PRE_GUARD', 'READ_AUTHORITY', 'READ_ADAPTER', 'READ_POST_GUARD')
            for state in ('START', 'ERROR' if phase == 'READ_ADAPTER' else 'COMPLETED')]
    assert read_stages(rows) == expected
    assert calls == (['read'] if stage == 'adapter' else [])
    assert provider['posts'] == 0 and not business.collector.snapshot()


@pytest.mark.asyncio
async def test_cancelled_read_identifies_adapter_without_swallowing_parent_cancel(
        db_session, crm_settings, monkeypatch):
    business, _, provider, _ = await setup(db_session, crm_settings)
    business.execution = ManagedRuntime(20, request=business.request)
    deadline, rows, entered = business.execution.deadline, [], asyncio.Event()
    monkeypatch.setattr(diagnostics, 'emit_managed_wait', rows.append)

    async def stalled(method, *args):
        assert method == 'read'
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(business, '_adapter_call', stalled)
    task = asyncio.create_task(business.call_tool('catalog_search', {}))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert read_stages(rows) == [(phase, state, 1)
        for phase in ('READ_PRE_GUARD', 'READ_AUTHORITY', 'READ_ADAPTER')
        for state in ('START', 'CANCELLED' if phase == 'READ_ADAPTER' else 'COMPLETED')]
    assert business.execution.deadline == deadline and provider['posts'] == 0
    assert not business.collector.snapshot()


@pytest.mark.asyncio
async def test_observer_failure_or_bound_does_not_prevent_guarded_read(
        db_session, crm_settings, monkeypatch):
    business, _, provider, _ = await setup(db_session, crm_settings)
    business.execution = ManagedRuntime(20, request=business.request)
    rows = []

    def broken(value):
        raise RuntimeError('private-sentinel')

    monkeypatch.setattr(diagnostics, 'emit_managed_wait', broken)
    first = await business.call_tool('catalog_search', {})
    monkeypatch.setattr(diagnostics, 'emit_managed_wait', rows.append)
    business._read_observation_step = 20
    second = await business.call_tool('catalog_search', {})
    assert first == second and first.tool_payload['status'] == 'OK'
    assert not read_stages(rows) and business._read_observation_step == 21
    # The outer READ observer and request-local CRM timers have independent
    # bounds. Saturating READ must not erase the nested HTTP measurements.
    assert [(row.phase, row.state) for row in rows] == [
        (phase, state) for request_phase in ('CRM_CONTEXT_HTTP', 'CRM_EXECUTE_HTTP')
        for phase in ('CRM_ASSERTION', 'CRM_CLIENT_SETUP', request_phase, 'CRM_CLIENT_CLOSE')
        for state in ('START', 'COMPLETED')]
    assert all(1 <= row.step <= 20 and row.attempt == 1 for row in rows)
    assert provider['posts'] == 0 and not business.collector.snapshot()
