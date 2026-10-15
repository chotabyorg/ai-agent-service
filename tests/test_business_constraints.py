"""Database scope/immutability/attempt-slot authority, not SQLite simulation."""

from tests.business_effect_helpers import claim_dispatch_with_test_permit, open_business_runtime
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError

from models.business import BusinessOperation, BusinessPlan
from schemas.business import BusinessSafetyError
from services.business_runtime import BusinessRuntime
from tests.test_business_runtime import actions, later, setup_business


@pytest.mark.asyncio
async def test_database_attempt_slot_rejects_second_plan_and_rolls_back(db_session):
    first, provider, registry, _ = await setup_business(db_session)
    second = await open_business_runtime(later(first.request), first.grant, db_session, registry)
    plan1, plan2 = await first.freeze_plan(actions("12")), await second.freeze_plan(actions("13"))
    await claim_dispatch_with_test_permit(first, plan1, plan1.operations[0], actions=actions("12"))
    async with db_session() as outsider:
        with pytest.raises(IntegrityError):
            await outsider.execute(update(BusinessOperation).where(BusinessOperation.id == plan2.operations[0].id).values(
                status="DISPATCHING", dispatch_epoch=1, dispatch_nonce=uuid4(), dispatch_started_at=func.clock_timestamp(),
                dispatch_lease_expires_at=func.clock_timestamp() + text("interval '8 seconds'"), attempt_count=1, state_version=1))
            await outsider.commit()
        await outsider.rollback()
    unchanged = await second.repository.selected_plan(second.request, second.grant)
    assert unchanged.operations[0].status == "RESERVED" and unchanged.operations[0].attempt_count == 0
    assert provider.posts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("column", ["request_payload", "request_fingerprint", "provider_idempotency_key", "issued_at"])
async def test_database_rejects_changed_immutable_operation_intent(db_session, column):
    runtime, provider, _, _ = await setup_business(db_session, mode="NATIVE")
    plan = await runtime.freeze_plan(actions("12"))
    values = {"request_payload": {"wrong": True}, "request_fingerprint": "e" * 64,
              "provider_idempotency_key": "another-key", "issued_at": func.clock_timestamp()}
    async with db_session() as outsider:
        with pytest.raises(IntegrityError):
            await outsider.execute(update(BusinessOperation).where(BusinessOperation.id == plan.operations[0].id)
                                   .values(**{column: values[column]}))
            await outsider.commit()
        await outsider.rollback()
    assert provider.posts == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_hash", [
    "effect_request_fingerprint",
    "effect_managed_request_fingerprint",
    "effect_operation_fingerprint",
])
async def test_effect_request_shape_rejects_each_missing_hash(db_session, missing_hash):
    runtime, _, _, _ = await setup_business(db_session)
    plan = await runtime.freeze_plan([{"capability": "booking.create", "arguments": {"slot": "12"}}])
    values = {
        "effect_request_turn_id": runtime.request.turn_id,
        "effect_request_fingerprint": "a" * 64,
        "effect_managed_request_fingerprint": "b" * 64,
        "effect_operation_fingerprint": "c" * 64,
        "effect_requested_at": func.clock_timestamp(),
    }
    values[missing_hash] = None
    async with db_session() as outsider:
        with pytest.raises(IntegrityError, match="effect_re"):
            await outsider.execute(update(BusinessOperation).where(
                BusinessOperation.id == plan.operations[0].id).values(**values))
            await outsider.commit()
        await outsider.rollback()


@pytest.mark.asyncio
async def test_effect_request_and_permit_are_one_time_guarded_assignments(db_session):
    runtime, _, _, _ = await setup_business(db_session)
    plan = await runtime.freeze_plan([{"capability": "booking.create", "arguments": {"slot": "12"}}])
    operation_id = plan.operations[0].id
    request_values = {
        "effect_request_turn_id": runtime.request.turn_id,
        "effect_request_fingerprint": "a" * 64,
        "effect_managed_request_fingerprint": "b" * 64,
        "effect_operation_fingerprint": "c" * 64,
        "effect_requested_at": func.clock_timestamp(),
    }
    async with db_session() as db:
        await db.execute(update(BusinessOperation).where(BusinessOperation.id == operation_id)
                         .values(**request_values))
        await db.commit()
    permit_id = uuid4()
    async with db_session() as db:
        await db.execute(update(BusinessOperation).where(BusinessOperation.id == operation_id)
                         .values(effect_permit_id=permit_id))
        await db.commit()
    for values in (
        {"effect_request_fingerprint": "d" * 64},
        {"effect_permit_id": uuid4()},
    ):
        async with db_session() as db:
            with pytest.raises(IntegrityError):
                await db.execute(update(BusinessOperation).where(
                    BusinessOperation.id == operation_id).values(**values))
                await db.commit()
            await db.rollback()


@pytest.mark.asyncio
async def test_effect_request_assignment_cannot_share_a_dispatch_transition(db_session):
    runtime, _, _, _ = await setup_business(db_session)
    plan = await runtime.freeze_plan([{"capability": "booking.create", "arguments": {"slot": "12"}}])
    now = func.clock_timestamp()
    with pytest.raises(IntegrityError):
        async with db_session() as db:
            await db.execute(update(BusinessOperation).where(
                BusinessOperation.id == plan.operations[0].id).values(
                    effect_request_turn_id=runtime.request.turn_id,
                    effect_request_fingerprint="a" * 64,
                    effect_managed_request_fingerprint="b" * 64,
                    effect_operation_fingerprint="c" * 64,
                    effect_requested_at=now,
                    status="DISPATCHING", state_version=1, dispatch_epoch=1,
                    dispatch_nonce=uuid4(), dispatch_started_at=now,
                    dispatch_lease_expires_at=func.clock_timestamp() + text("interval '8 seconds'"),
                    attempt_count=1,
                ))
            await db.commit()


@pytest.mark.asyncio
async def test_effect_permit_assignment_cannot_share_a_dispatch_transition(db_session):
    runtime, _, _, _ = await setup_business(db_session)
    plan = await runtime.freeze_plan([{"capability": "booking.create", "arguments": {"slot": "12"}}])
    from tests.business_effect_helpers import prepare_effect_permit

    await prepare_effect_permit(runtime, plan, plan.operations[0])
    now = func.clock_timestamp()
    with pytest.raises(IntegrityError):
        async with db_session() as db:
            await db.execute(update(BusinessOperation).where(
                BusinessOperation.id == plan.operations[0].id).values(
                    effect_permit_id=uuid4(), status="DISPATCHING", state_version=1,
                    dispatch_epoch=1, dispatch_nonce=uuid4(), dispatch_started_at=now,
                    dispatch_lease_expires_at=func.clock_timestamp() + text("interval '8 seconds'"),
                    attempt_count=1,
                ))
            await db.commit()


@pytest.mark.asyncio
async def test_terminal_evidence_and_retained_row_cannot_be_overwritten_or_deleted(db_session):
    runtime, provider, _, _ = await setup_business(db_session)
    await runtime.execute_plan(actions("12"))
    plan = await runtime.repository.selected_plan(runtime.request, runtime.grant)
    async with db_session() as outsider:
        with pytest.raises(IntegrityError):
            await outsider.execute(update(BusinessOperation).where(BusinessOperation.id == plan.operations[0].id)
                                   .values(receipt_id="forged"))
            await outsider.commit()
        await outsider.rollback()
        with pytest.raises(IntegrityError):
            operation = await outsider.get(BusinessOperation, plan.operations[0].id)
            await outsider.delete(operation)
            await outsider.commit()
        await outsider.rollback()
    assert provider.posts == 1


@pytest.mark.asyncio
async def test_composite_provenance_rejects_foreign_connection_and_scope(db_session):
    first, _, _, _ = await setup_business(db_session)
    foreign, _, _, _ = await setup_business(db_session)
    plan1, plan2 = await first.freeze_plan(actions("12")), await foreign.freeze_plan(actions("12"))
    # Insert through a raw independent SQL connection, bypassing repository DTOs;
    # composite plan provenance must itself reject the foreign scope/connection.
    async with db_session() as outsider:
        original = await outsider.get(BusinessOperation, plan1.operations[0].id)
        values = {column.name: getattr(original, column.name) for column in BusinessOperation.__table__.columns}
        values.update(id=uuid4(), ordinal=1, scope_id=plan2.scope.scope_id, connection_id=plan2.operations[0].command.connection_id)
        outsider.add(BusinessOperation(**values))
        with pytest.raises(IntegrityError):
            await outsider.commit()
        await outsider.rollback()


@pytest.mark.asyncio
async def test_foreign_scope_evidence_and_operation_selection_are_denied(db_session):
    first, _, registry, _ = await setup_business(db_session)
    plan = await first.freeze_plan(actions("12"))
    foreign, _, _, _ = await setup_business(db_session)
    with pytest.raises(BusinessSafetyError, match="SCOPE_DENIED"):
        await first.repository.read_evidence(first.request, foreign.grant, db_session)
    with pytest.raises(BusinessSafetyError, match="PLAN_DENIED"):
        await foreign.select_recovery_plan(plan.id)
    with pytest.raises(BusinessSafetyError, match="OPERATION_DENIED"):
        await foreign.reconcile(plan.operations[0].id)
