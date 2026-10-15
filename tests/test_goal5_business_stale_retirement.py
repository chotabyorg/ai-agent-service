"""Atomic stale split-proposal retirement and non-retirement evidence."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from sqlalchemy import delete
from sqlalchemy.exc import DBAPIError, IntegrityError

from models.business import BusinessOperation, BusinessPlan, BusinessScope
from schemas.business import BusinessSafetyError, fingerprint
from services.business_registry import BusinessRegistry
from tests.test_business_concurrency import held, observe_blocked
from tests.business_effect_helpers import open_business_runtime, prepare_effect_permit
from tests.test_business_runtime import actions, later, setup_business


async def semantic_business(factory, *, slots=("18:00",)):
    original, provider, _, binding = await setup_business(
        factory, confirmation=True
    )
    registration = replace(
        original.registration, confirmation_mode="SEMANTIC_V1"
    )
    registry = BusinessRegistry((registration,))
    await original.repository.configure_binding(
        original.grant,
        adapter_id=registration.adapter_id,
        connection_id=binding.connection_id,
        catalog_revision=registration.catalog_revision,
        enabled_capabilities=["booking.create"],
        authorization_generation=2,
    )
    runtime = await open_business_runtime(
        original.request, original.grant, factory, registry
    )
    await runtime.propose_plan(actions(*slots))
    plan = await runtime.repository.selected_plan(runtime.request, runtime.grant)
    assert plan is not None and plan.proposal_state == "PREPARED"
    assert all(operation.status == "RESERVED" for operation in plan.operations)
    return runtime, provider, registry, plan


async def open_later(factory, origin, registry, message="A distinct later customer request"):
    request = later(origin.request, message)
    runtime = await open_business_runtime(
        request, origin.grant, factory, registry
    )
    return request, runtime


def enable_observable_lock_waits(runtime):
    """Keep production lock semantics but let PostgreSQL expose the queue."""
    original_session = runtime.repository._session

    @asynccontextmanager
    async def session(*, decisive=False):
        async with original_session(decisive=decisive) as connection:
            # The repository normally uses NOWAIT on decisive writes. The
            # barrier tests temporarily use ordinary row waits so PostgreSQL
            # itself proves independent transactions queued in the intended
            # order before the holder commits.
            connection.info["business_nowait"] = False
            yield connection

    runtime.repository._session = session


async def confirm_without_dispatch(factory, origin, registry, plan, message="Да"):
    _, confirmer = await open_later(factory, origin, registry, message)
    confirmed = await confirmer.repository.confirm_plan(
        confirmer.request,
        confirmer.grant,
        plan.id,
        confirmer.registration,
        confirmer.facts_resolver(actions("18:00")),
        semantic=True,
    )
    assert confirmed.proposal_state == "CONFIRMED"
    assert confirmed.confirmed_turn_id == confirmer.request.turn_id
    assert confirmer.effect_port.permit_requests == []
    return confirmer, confirmed


async def enqueue_actual_scope_race(factory, confirmer, plan, order):
    """Queue two separate transactions behind one scope lock in known order."""
    stale_request, stale = await open_later(
        factory, confirmer, confirmer.registry, "A later distinct customer instruction"
    )
    # Resolve all external/facts inputs before acquiring the contested lock.
    # Calling _confirm_plan directly avoids a second admission-probe transaction
    # that could otherwise reorder the two actual scope-lock contenders.
    probe = await confirmer.repository._admission_probe(
        confirmer.request, confirmer.grant, plan.id
    )
    facts = await confirmer.repository._resolve_fresh(
        confirmer.facts_resolver(actions("18:00"))
    )
    enable_observable_lock_waits(confirmer)
    enable_observable_lock_waits(stale)

    async def confirm():
        return await confirmer.repository._confirm_plan(
            confirmer.request,
            confirmer.grant,
            plan.id,
            confirmer.registration,
            facts,
            probe=probe,
            semantic=True,
        )

    async def retire():
        return await stale.repository.retire_stale_split_proposal(
            stale_request, stale.grant, plan.id, stale.registration
        )

    contenders = {"confirm": confirm, "retire": retire}
    async with held(
        factory,
        BusinessScope,
        BusinessScope.id == plan.scope.scope_id,
    ) as (blocker, blocker_pid):
        first = asyncio.create_task(contenders[order[0]]())
        await observe_blocked(factory, blocker_pid)
        second = asyncio.create_task(contenders[order[1]]())
        waits = await observe_blocked(factory, blocker_pid, count=2)
        assert len({item[0] for item in waits}) == 2
        await blocker.rollback()
        results = await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), 5
        )
    by_name = dict(zip(order, results, strict=True))
    return stale, stale_request, by_name


@pytest.mark.asyncio
async def test_retirement_tombstone_is_exact_and_idempotent(db_session):
    origin, provider, registry, plan = await semantic_business(db_session)
    stale_request, stale = await open_later(
        db_session, origin, registry, "Later independent instruction"
    )

    candidate = await stale.repository.stale_split_proposal_context(
        stale_request, stale.grant, stale.registration
    )
    assert candidate is not None and candidate.id == plan.id
    assert candidate.proposal_state == "PREPARED"
    assert provider.posts == 0

    tombstone = await stale.repository.retire_stale_split_proposal(
        stale_request, stale.grant, plan.id, stale.registration
    )
    assert tombstone.proposal_state == "INVALIDATED"
    assert tombstone.id == plan.id and tombstone.origin_turn_id == plan.origin_turn_id
    assert len(tombstone.operations) == len(plan.operations) == 1
    evidence = {"kind": "NO_DISPATCH", "reason_code": "STALE_INTERVENING_SPLIT"}
    async with db_session() as session:
        stored_operations = [
            await session.get(BusinessOperation, operation.id)
            for operation in tombstone.operations
        ]
    for operation, stored in zip(tombstone.operations, stored_operations, strict=True):
        assert operation.status == "FAILED"
        assert operation.state_version == 1
        assert operation.dispatch_epoch == 0
        assert operation.dispatch_nonce is None
        assert operation.attempt_count == 0
        assert operation.receipt_id == f"no-dispatch:{operation.id}"
        assert operation.object_id is None
        assert operation.receipt_evidence == evidence
        assert operation.receipt_fingerprint == fingerprint(
            "chota.business.receipt.v1", evidence
        )
        assert stored.failure_code == "STALE_INTERVENING_SPLIT"
        assert stored.dispatch_started_at is None
        assert stored.dispatch_lease_expires_at is None
        assert stored.effect_managed_request_fingerprint is None
        assert stored.effect_operation_fingerprint is None
        assert operation.effect_request_turn_id is None
        assert operation.effect_request_fingerprint is None
        assert operation.effect_requested_at is None
        assert operation.effect_permit_id is None

    retained = await stale.repository.stale_split_proposal_context(
        stale_request, stale.grant, stale.registration
    )
    replay = await stale.repository.retire_stale_split_proposal(
        stale_request, stale.grant, plan.id, stale.registration
    )
    assert retained == tombstone
    assert replay == tombstone
    assert stale.effect_port.permit_requests == [] and provider.posts == 0


@pytest.mark.asyncio
async def test_generic_invalidated_proposal_is_not_a_stale_split_tombstone(db_session):
    origin, provider, registry, plan = await semantic_business(db_session)
    _, confirmer = await open_later(db_session, origin, registry, "Да")

    # A facts revision change exercises the repository's ordinary invalidation
    # path, whose no-dispatch reason is not the split-retirement provenance.
    provider.fact_revision += 1
    invalidated = await confirmer.repository.confirm_plan(
        confirmer.request,
        confirmer.grant,
        plan.id,
        confirmer.registration,
        confirmer.facts_resolver(actions("18:00")),
        semantic=True,
    )
    assert invalidated.proposal_state == "INVALIDATED"
    assert invalidated.operations[0].status == "FAILED"
    assert invalidated.operations[0].receipt_evidence == {
        "kind": "NO_DISPATCH", "reason_code": "STALE_CONFIRMATION"
    }

    stale_request, stale = await open_later(
        db_session, confirmer, registry, "A distinct later fresh instruction"
    )
    assert await stale.repository.stale_split_proposal_context(
        stale_request, stale.grant, stale.registration
    ) is None
    with pytest.raises(BusinessSafetyError, match="PLAN_DENIED"):
        await stale.repository.retire_stale_split_proposal(
            stale_request, stale.grant, plan.id, stale.registration
        )
    assert stale.effect_port.permit_requests == [] and provider.posts == 0


@pytest.mark.asyncio
async def test_operation_cardinality_guard_and_database_immutability(db_session):
    origin, provider, registry, plan = await semantic_business(
        db_session, slots=("18:00", "19:00")
    )
    stale_request, stale = await open_later(
        db_session, origin, registry, "Later complete customer instruction"
    )
    assert len(plan.operations) == 2

    assert not stale.repository._unacquired_reservations(plan.operations[:1], 2)
    with pytest.raises(IntegrityError):
        async with db_session() as session:
            await session.execute(
                delete(BusinessOperation).where(
                    BusinessOperation.id == plan.operations[1].id
                )
            )
            await session.commit()

    candidate = await stale.repository.stale_split_proposal_context(
        stale_request, stale.grant, stale.registration
    )
    assert candidate is not None and candidate.id == plan.id
    retired = await stale.repository.retire_stale_split_proposal(
        stale_request, stale.grant, plan.id, stale.registration
    )
    assert retired.proposal_state == "INVALIDATED"
    assert len(retired.operations) == 2
    async with db_session() as session:
        stored_plan = await session.get(BusinessPlan, plan.id)
        assert stored_plan.proposal_state == "INVALIDATED"
    assert provider.posts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["confirm", "retire"])
async def test_confirmation_prerequisite_and_retirement_serialize_both_orders(
    db_session, first
):
    origin, provider, registry, plan = await semantic_business(db_session)
    _, confirmer = await open_later(db_session, origin, registry, "Да, подтверждаю")

    stale, stale_request, results = await enqueue_actual_scope_race(
        db_session, confirmer, plan, (first, "retire" if first == "confirm" else "confirm")
    )
    confirmation = results["confirm"]
    retirement = results["retire"]

    if first == "retire":
        # Retirement commits the exact no-dispatch marker first. The old
        # confirmation prerequisite must then fail closed, and direct
        # acquisition cannot ask Conversation for a permit.
        assert not isinstance(retirement, Exception)
        assert retirement.proposal_state == "INVALIDATED"
        assert isinstance(confirmation, BusinessSafetyError)
        assert confirmation.code in {"PLAN_DENIED", "CONFIRMATION_REQUIRED", "STALE_CONFIRMATION"}
        with pytest.raises(BusinessSafetyError):
            await confirmer.repository.prepare_effect_acquisition(
                confirmer.request,
                confirmer.grant,
                plan.id,
                plan.operations[0].id,
                confirmer.registration,
                confirmer.facts_resolver(actions("18:00")),
            )
        retained = await stale.repository.lookup_plan(
            stale_request, stale.grant, plan.id
        )
        assert retained.proposal_state == "INVALIDATED"
        assert retained.operations[0].status == "FAILED"
        assert confirmer.effect_port.permit_requests == []
        assert stale.effect_port.permit_requests == []
        assert provider.posts == 0
    else:
        # Confirmation commits first, so retirement sees a linked confirmed
        # proposal and cannot replace its identity. Only after that successful
        # prerequisite may this exact operation reach REQUESTED and GRANTED.
        assert not isinstance(confirmation, Exception)
        assert confirmation.proposal_state == "CONFIRMED"
        assert isinstance(retirement, BusinessSafetyError)
        assert retirement.code in {"PLAN_CONFLICT", "PLAN_DENIED", "STALE_CONFIRMATION"}
        acquisition = await confirmer.repository.prepare_effect_acquisition(
            confirmer.request,
            confirmer.grant,
            plan.id,
            plan.operations[0].id,
            confirmer.registration,
            confirmer.facts_resolver(actions("18:00")),
        )
        assert acquisition.reason == "REQUESTED"
        assert acquisition.request is not None
        assert provider.posts == 0
        permit = await confirmer.effect_port.permit(acquisition.request)
        await confirmer.repository.bind_effect_permit(
            confirmer.request,
            confirmer.grant,
            plan.id,
            plan.operations[0].id,
            acquisition.request,
            permit.permit_id,
        )
        retained = await confirmer.repository.lookup_plan(
            confirmer.request, confirmer.grant, plan.id
        )
        assert retained.id == plan.id
        assert retained.proposal_state == "CONFIRMED"
        assert retained.operations[0].status == "RESERVED"
        assert retained.operations[0].effect_permit_id == permit.permit_id
        assert confirmer.effect_port.permit_requests == [acquisition.request]
        assert stale.effect_port.permit_requests == [] and provider.posts == 0


@pytest.mark.asyncio
async def test_default_nowait_contention_cannot_retire_or_replace(db_session):
    origin, provider, registry, plan = await semantic_business(db_session)
    stale_request, stale = await open_later(
        db_session, origin, registry, "A distinct later fresh instruction"
    )

    # The two-order barrier test temporarily disables NOWAIT only so
    # PostgreSQL exposes an ordered waiter chain. The production decisive
    # session policy remains fail-closed when it meets a held scope row.
    async with held(
        db_session, BusinessScope, BusinessScope.id == plan.scope.scope_id
    ):
        with pytest.raises(DBAPIError):
            await stale.repository.retire_stale_split_proposal(
                stale_request, stale.grant, plan.id, stale.registration
            )

    retained = await stale.repository.lookup_plan(
        stale_request, stale.grant, plan.id
    )
    assert retained.proposal_state == "PREPARED"
    assert all(operation.status == "RESERVED" for operation in retained.operations)
    assert all(operation.effect_request_turn_id is None for operation in retained.operations)
    assert provider.posts == 0 and stale.effect_port.permit_requests == []


async def retained_confirmation(factory, *, slots=("18:00",)):
    origin, provider, registry, plan = await semantic_business(factory, slots=slots)
    confirmer, confirmed = await confirm_without_dispatch(
        factory, origin, registry, plan
    )
    stale_request, stale = await open_later(
        factory, confirmer, registry, "A distinct later customer instruction"
    )
    return origin, provider, registry, plan, confirmer, confirmed, stale_request, stale


async def put_operation_in_state(runtime, plan, state, *, multi=False):
    operation = plan.operations[0]
    actions_for_plan = actions("18:00", "19:00") if multi else actions("18:00")
    if state == "REQUESTED":
        decision = await runtime.repository.prepare_effect_acquisition(
            runtime.request,
            runtime.grant,
            plan.id,
            operation.id,
            runtime.registration,
            runtime.facts_resolver(actions_for_plan),
        )
        assert decision.reason == "REQUESTED" and decision.request is not None
    elif state in {"GRANTED", "DISPATCHING", "UNKNOWN", "SUCCEEDED", "FAILED"}:
        request = await prepare_effect_permit(runtime, plan, operation, actions=actions_for_plan)
        assert request is not None
        if state != "GRANTED":
            if state == "UNKNOWN":
                runtime.registration.adapter.provider.lose_next = True
            if state == "FAILED":
                runtime.registration.adapter.provider.execute_outcome = "FAILED"
            decision = await runtime.repository.claim_dispatch(
                runtime.request,
                runtime.grant,
                plan.id,
                operation.id,
                runtime.registration,
                runtime.facts_resolver(actions_for_plan),
                effect_request=request,
            )
            assert decision.token is not None
            if state == "DISPATCHING":
                return
            result = await runtime._send(decision.token)
            assert result.operations[0].status == (
                "UNKNOWN" if state == "UNKNOWN" else "FAILED" if state == "FAILED" else "SUCCEEDED"
            )
    else:
        raise AssertionError(f"Unexpected operation state: {state}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state",
    ["REQUESTED", "GRANTED", "DISPATCHING", "UNKNOWN", "SUCCEEDED", "FAILED"],
)
async def test_nonretirable_acquisition_and_dispatch_states_preserve_original_plan(
    db_session, state
):
    (
        origin,
        provider,
        registry,
        plan,
        confirmer,
        confirmed,
        stale_request,
        stale,
    ) = await retained_confirmation(db_session)
    await put_operation_in_state(confirmer, plan, state)
    before = await confirmer.repository.lookup_plan(
        confirmer.request, confirmer.grant, plan.id
    )
    assert before.proposal_state == "CONFIRMED"

    with pytest.raises(BusinessSafetyError) as denied:
        await stale.repository.retire_stale_split_proposal(
            stale_request, stale.grant, plan.id, stale.registration
        )
    assert denied.value.code == (
        "BLOCKED_UNRESOLVED_EFFECT"
        if state in {"REQUESTED", "GRANTED", "DISPATCHING", "UNKNOWN"}
        else "PLAN_DENIED"
    )
    after = await confirmer.repository.lookup_plan(
        confirmer.request, confirmer.grant, plan.id
    )
    assert after.id == before.id == confirmed.id == plan.id
    assert after.proposal_state == before.proposal_state == "CONFIRMED"
    assert after.confirmed_turn_id == before.confirmed_turn_id == confirmer.request.turn_id
    assert after.operations == before.operations
    assert await stale.repository.stale_split_proposal_context(
        stale_request, stale.grant, stale.registration
    ) is None
    assert provider.posts == (1 if state in {"UNKNOWN", "SUCCEEDED", "FAILED"} else 0)
    if state == "UNKNOWN":
        assert after.operations[0].attempt_count == 1
        assert after.operations[0].dispatch_epoch == 1
    elif state in {"SUCCEEDED", "FAILED"}:
        assert after.operations[0].attempt_count == 1
        assert after.operations[0].dispatch_epoch == 1
    else:
        assert after.operations[0].effect_request_turn_id == confirmer.request.turn_id
        if state == "REQUESTED":
            assert after.operations[0].effect_permit_id is None
        else:
            assert after.operations[0].effect_permit_id is not None


@pytest.mark.asyncio
async def test_partially_acquired_multioperation_plan_is_not_retired(db_session):
    (
        _,
        provider,
        _,
        plan,
        confirmer,
        confirmed,
        stale_request,
        stale,
    ) = await retained_confirmation(db_session, slots=("18:00", "19:00"))
    assert len(plan.operations) == 2
    await put_operation_in_state(confirmer, plan, "REQUESTED", multi=True)

    before = await confirmer.repository.lookup_plan(
        confirmer.request, confirmer.grant, plan.id
    )
    with pytest.raises(BusinessSafetyError, match="BLOCKED_UNRESOLVED_EFFECT"):
        await stale.repository.retire_stale_split_proposal(
            stale_request, stale.grant, plan.id, stale.registration
        )
    after = await confirmer.repository.lookup_plan(
        confirmer.request, confirmer.grant, plan.id
    )
    assert after.id == confirmed.id == plan.id
    assert [operation.status for operation in after.operations] == ["RESERVED", "RESERVED"]
    assert after.operations[0].effect_request_turn_id == confirmer.request.turn_id
    assert after.operations[0].effect_request_fingerprint is not None
    assert after.operations[1].effect_request_turn_id is None
    assert after.operations[1].effect_permit_id is None
    assert after.operations == before.operations
    assert provider.posts == 0
