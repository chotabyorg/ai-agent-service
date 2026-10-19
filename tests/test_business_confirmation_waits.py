"""Observe real durable confirmation boundaries without changing effect ownership."""
import asyncio
from collections import Counter

import pytest

from schemas.conversation_effects import EffectReason
from services.managed_runtime import ManagedRuntime
from tests.business_effect_helpers import FakeConversationEffectPort, open_business_runtime
from tests.test_business_runtime import actions, later, setup_business
from tests.test_managed_wait_diagnostics import logging_boundary, observations, SECRET  # noqa: F401


PHASES = {
    'BUSINESS_CONFIRM_REPOSITORY', 'BUSINESS_EFFECT_ACQUIRE', 'BUSINESS_EFFECT_PERMIT',
    'BUSINESS_EFFECT_BIND', 'BUSINESS_DISPATCH_CLAIM', 'BUSINESS_FACTS_ADAPTER',
    'BUSINESS_PROVIDER_EXECUTE', 'BUSINESS_RECEIPT_FINALIZE',
}


async def confirmation(factory):
    original, provider, registry, _ = await setup_business(factory, mode='NATIVE', confirmation=True)
    proposed = await original.propose_plan(actions('12:00'))
    request = later(original.request, 'confirm ' + proposed.tool_payload['proposal_id'])
    execution = ManagedRuntime(60, request=request)
    business = await open_business_runtime(request, original.grant, factory, registry, execution=execution)
    await business.prompt_context()
    return business, execution, provider, proposed.tool_payload['proposal_id']


@pytest.mark.integration
async def test_confirmation_retains_three_fresh_facts_and_one_effect_on_repeat(db_session, caplog):
    business, execution, provider, proposal_id = await confirmation(db_session)
    deadline = execution.deadline
    caplog.clear()
    result = await execution.operation(business.confirm(proposal_id))
    assert result.halt_status is None and provider.posts == 1
    rows = [row for row in observations(caplog) if row.phase in PHASES]
    expected = Counter({phase: 1 for phase in PHASES})
    expected['BUSINESS_FACTS_ADAPTER'] = 3
    assert Counter(row.phase for row in rows if row.state == 'START') == expected
    assert Counter(row.phase for row in rows if row.state == 'COMPLETED') == expected
    assert all(row.remaining_ms <= 60000 for row in rows)
    # Facts are nested inside their repository stages, not extra serial time.
    facts = [row for row in rows if row.phase == 'BUSINESS_FACTS_ADAPTER' and row.state == 'START']
    assert [row.step for row in facts] == [1, 2, 3]
    assert execution.deadline == deadline and execution.active_scope is None
    caplog.clear()
    replay = await execution.operation(business.confirm(proposal_id))
    assert replay.halt_status is None and provider.posts == 1
    assert not any(row.phase in {'BUSINESS_PROVIDER_EXECUTE', 'BUSINESS_EFFECT_PERMIT'}
                   for row in observations(caplog))
    plan = await business.repository.selected_plan(business.request, business.grant)
    assert plan.operations[0].status == 'SUCCEEDED' and plan.operations[0].attempt_count == 1


@pytest.mark.integration
async def test_denied_permit_stops_before_dispatch_and_retains_no_effect(db_session, caplog):
    business, execution, provider, proposal_id = await confirmation(db_session)
    business.effect_port = FakeConversationEffectPort(permit_denial=EffectReason.INPUT_SUPERSEDED)
    caplog.clear()
    result = await execution.operation(business.confirm(proposal_id))
    assert result.safe_draft == 'Операция не выполнялась: разрешение на действие не получено.'
    assert provider.posts == 0
    rows = observations(caplog)
    assert [(row.state, row.step) for row in rows if row.phase == 'BUSINESS_EFFECT_PERMIT'] == [
        ('START', 1), ('ERROR', 1)]
    assert not any(row.phase in {'BUSINESS_EFFECT_BIND', 'BUSINESS_DISPATCH_CLAIM',
                                'BUSINESS_PROVIDER_EXECUTE', 'BUSINESS_RECEIPT_FINALIZE'} for row in rows)
    plan = await business.repository.selected_plan(business.request, business.grant)
    assert plan.operations[0].status == 'FAILED' and plan.operations[0].attempt_count == 0


@pytest.mark.integration
@pytest.mark.parametrize('phase', sorted(PHASES))
async def test_parent_cancel_joins_original_owner_at_each_boundary(
        db_session, monkeypatch, caplog, phase):
    business, execution, provider, proposal_id = await confirmation(db_session)
    entered = asyncio.Event()
    method = {
        'BUSINESS_CONFIRM_REPOSITORY': 'confirm_plan',
        'BUSINESS_EFFECT_ACQUIRE': 'prepare_effect_acquisition',
        'BUSINESS_EFFECT_BIND': 'bind_effect_permit',
        'BUSINESS_DISPATCH_CLAIM': 'claim_dispatch',
        'BUSINESS_RECEIPT_FINALIZE': 'finalize',
    }.get(phase)

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    if method:
        monkeypatch.setattr(business.repository, method, blocked)
    elif phase == 'BUSINESS_EFFECT_PERMIT':
        monkeypatch.setattr(business.effect_port, 'permit', blocked)
    else:
        adapter_call = business._adapter_call
        selected = 'resolve_facts' if phase == 'BUSINESS_FACTS_ADAPTER' else 'execute'

        async def adapter(method, *args):
            if method == selected:
                return await blocked()
            return await adapter_call(method, *args)

        monkeypatch.setattr(business, '_adapter_call', adapter)
    deadline = execution.deadline
    caplog.clear()
    owner = asyncio.create_task(execution.operation(business.confirm(proposal_id)))
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        execution.signal_cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(owner), timeout=5)
    finally:
        if not owner.done():
            execution.signal_cancel()
            await asyncio.gather(owner, return_exceptions=True)
    assert execution.active_scope is None and execution.deadline == deadline
    assert provider.posts == (1 if phase == 'BUSINESS_RECEIPT_FINALIZE' else 0)
    assert [row.state for row in observations(caplog) if row.phase == phase] == ['START', 'CANCELLED']
    if phase in {'BUSINESS_PROVIDER_EXECUTE', 'BUSINESS_RECEIPT_FINALIZE'}:
        plan = await business.repository.selected_plan(business.request, business.grant)
        assert plan.operations[0].status == 'UNKNOWN' and plan.operations[0].attempt_count == 1
    assert SECRET not in caplog.text
