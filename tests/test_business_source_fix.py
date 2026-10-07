"""Source-fix behavior proofs against real disposable PostgreSQL authority."""

from dataclasses import replace
from uuid import uuid4

import pytest

from schemas.business import BusinessSafetyError, MaterialFacts
from services.business_registry import BusinessRegistry, ExecutionContract
from services.business_runtime import BusinessRuntime
from tests.test_business_runtime import actions, later, setup_business


@pytest.mark.asyncio
@pytest.mark.parametrize("port", ["reserve", "dispatch", "confirm"])
async def test_admission_ports_reject_preread_raw_facts(db_session, port):
    runtime, provider, _, _ = await setup_business(db_session, confirmation=True)
    facts = await runtime._facts(actions("12"))
    if port == "reserve":
        call = runtime.repository.reserve_plan(runtime.request, runtime.grant, runtime.registration, actions("12"), facts)
    else:
        plan = await runtime.freeze_plan(actions("12"), propose=True)
        if port == "dispatch":
            call = runtime.repository.claim_dispatch(runtime.request, runtime.grant, plan.id,
                plan.operations[0].id, runtime.registration, facts)
        else:
            request = later(runtime.request, "confirm " + str(plan.id))
            call = runtime.repository.confirm_plan(request, runtime.grant, plan.id, runtime.registration, facts)
    with pytest.raises(BusinessSafetyError, match="FACTS_RESOLVER_REQUIRED"):
        await call
    assert provider.posts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("ordinal", range(32))
async def test_fresh_success_at_every_ordinal_consumes_no_old_token(db_session, ordinal):
    runtime, provider, _, _ = await setup_business(db_session)
    selected = actions(*(str(i) for i in range(32)))
    plan = await runtime.freeze_plan(selected)
    # Prefix success is real positive F; the captured candidate stays RESERVED.
    for operation in plan.operations[:ordinal]:
        decision = await runtime.repository.claim_dispatch(runtime.request, runtime.grant, plan.id,
            operation.id, runtime.registration, runtime.facts_resolver(selected))
        await runtime._send(decision.token)
    stale = await runtime.repository.selected_plan(runtime.request, runtime.grant)
    candidate = stale.operations[ordinal]
    decision = await runtime.repository.claim_dispatch(runtime.request, runtime.grant, plan.id,
        candidate.id, runtime.registration, runtime.facts_resolver(selected))
    assert decision.token is not None
    await runtime._send(decision.token)
    result = await runtime._execute_frozen(stale)
    fresh = await runtime.repository.selected_plan(runtime.request, runtime.grant)
    assert result.halt_status is None and result.tool_payload["status"] == "SUCCEEDED"
    assert len(fresh.operations) == provider.posts == len(provider.objects) == 32
    assert all(op.status == "SUCCEEDED" and op.attempt_count == 1 for op in fresh.operations)
    assert [op.id for op in fresh.operations] == [op.id for op in plan.operations]
    assert len(runtime.collector.snapshot()) == 32


@pytest.mark.asyncio
async def test_legal_failed_predecessor_stops_stale_reserved_residual(db_session):
    runtime, provider, _, _ = await setup_business(db_session)
    selected = actions("12", "13")
    stale = await runtime.freeze_plan(selected)
    provider.execute_outcome = "FAILED"
    decision = await runtime.repository.claim_dispatch(runtime.request, runtime.grant, stale.id,
        stale.operations[0].id, runtime.registration, runtime.facts_resolver(selected))
    await runtime._send(decision.token)
    result = await runtime._execute_frozen(stale)
    fresh = await runtime.repository.selected_plan(runtime.request, runtime.grant)
    assert result.halt_status == "FAILED" and provider.posts == 1
    assert [op.status for op in fresh.operations] == ["FAILED", "RESERVED"]
    assert fresh.operations[1].attempt_count == 0


@pytest.mark.asyncio
async def test_provider_conditional_rejection_after_d_is_only_attempt_evidence(db_session):
    runtime, provider, _, _ = await setup_business(db_session)
    selected = actions("12")
    plan = await runtime.freeze_plan(selected)
    decision = await runtime.repository.claim_dispatch(runtime.request, runtime.grant, plan.id,
        plan.operations[0].id, runtime.registration, runtime.facts_resolver(selected))
    provider.fact_revision += 1
    fresh = await runtime._send(decision.token)
    assert provider.posts == 1 and not provider.objects
    assert fresh.operations[0].status == "UNKNOWN" and fresh.operations[0].receipt_id is None
    # Native replay denial cannot rewrite an original possible B1 to FAILED.
    refs = await runtime.collect_refs()
    assert refs[0].outcome == "UNKNOWN"


@pytest.mark.asyncio
async def test_conservative_exact_legacy_bounded_profile_preserves_original_rows(db_session):
    runtime, provider, _, _ = await setup_business(db_session, mode="NATIVE", retention=60)
    entry = runtime.registration
    compatible = replace(entry, retained_catalog_revisions=(entry.legacy_catalog_revision,))
    await runtime.repository.configure_binding(runtime.grant, adapter_id=entry.adapter_id,
        connection_id=runtime.binding.connection_id, catalog_revision=entry.legacy_catalog_revision,
        enabled_capabilities=["booking.create"], authorization_generation=2)
    registry = BusinessRegistry((compatible,))
    original = await BusinessRuntime.open(runtime.request, runtime.grant, db_session, registry)
    provider.lose_next = True
    await original.execute_plan(actions("12"))
    before = await original.repository.selected_plan(original.request, original.grant)
    command = before.operations[0].command
    reopened = await BusinessRuntime.open(runtime.request, runtime.grant, db_session, registry)
    denied = await reopened.replay_native(command.operation_id)
    assert denied.operations[0].status == "UNKNOWN" and provider.posts == 1
    assert denied.operations[0].command == command and denied.catalog_revision == entry.legacy_catalog_revision
    reconciled = await reopened.reconcile(command.operation_id)
    assert reconciled.operations[0].status == "SUCCEEDED" and provider.posts == 1
    assert reconciled.operations[0].command == command
    with pytest.raises(ValueError, match="Exact conservative"):
        replace(compatible, execution_contract=ExecutionContract(expected_facts="CONDITIONAL_EXPECTED_FACTS_V1",
            bounded_acceptance="PROVIDER_ACCEPT_BEFORE_V1"))


@pytest.mark.asyncio
async def test_unavailable_conditional_effect_contract_never_dispatches(db_session):
    runtime, provider, _, _ = await setup_business(db_session)
    entry = replace(runtime.registration, execution_contract=ExecutionContract())
    await runtime.repository.configure_binding(runtime.grant, adapter_id=entry.adapter_id,
        connection_id=runtime.binding.connection_id, catalog_revision=entry.catalog_revision,
        enabled_capabilities=["booking.create"], authorization_generation=2)
    reopened = await BusinessRuntime.open(runtime.request, runtime.grant, db_session, BusinessRegistry((entry,)))
    result = await reopened.execute_plan(actions("12"))
    plan = await reopened.repository.selected_plan(reopened.request, reopened.grant)
    assert result.halt_status == "FAILED" and provider.posts == 0
    assert plan.operations[0].status == "RESERVED" and plan.operations[0].attempt_count == 0


@pytest.mark.asyncio
async def test_conflicting_replay_preserves_retained_refs_and_cannot_claim_draft(db_session):
    runtime, provider, _, _ = await setup_business(db_session)
    await runtime.execute_plan(actions("12"))
    changed = runtime.request.model_dump(mode="python")
    changed["messages"][0]["text"] = "Changed canonical input"
    request = type(runtime.request).model_validate(changed)
    gate = await runtime.repository.result_gate(request, runtime.grant)
    assert gate.disposition == "UNAVAILABLE" and gate.references[0].outcome == "SUCCEEDED"
    assert gate.references[0].operation_id == runtime.collector.snapshot()[0].operation_id
    with pytest.raises(BusinessSafetyError, match="TURN_CONFLICT"):
        await runtime.repository.reserve_plan(request, runtime.grant, runtime.registration,
            actions("13"), runtime.facts_resolver(actions("13")))
    assert provider.posts == 1
