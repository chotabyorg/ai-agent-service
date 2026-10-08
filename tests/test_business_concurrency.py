"""Independent PostgreSQL connections with observed blocked transactions."""

import asyncio
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import func, select, text, update

from models.agent import AgentTemplate
from models.business import BusinessBinding, BusinessOperation, BusinessPlan, BusinessScope
from schemas.business import BusinessSafetyError, ProvenSucceeded, SucceededReceipt
from services.business_runtime import BusinessRuntime
from tests.test_business_runtime import actions, later, setup_business


async def observe_blocked(factory, blocker_pid, *, count=1):
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        # New connection/statistics snapshot each time, never a cached observer.
        async with factory() as observer:
            rows = (await observer.execute(text(
                "SELECT pid, pg_blocking_pids(pid), wait_event_type, xact_start IS NOT NULL "
                "FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid)) > 0"
            ))).all()
        # PostgreSQL may queue a second row waiter behind the first waiter,
        # rather than directly behind the original holder. Prove the actual
        # lock dependency chain to our owned holder, not merely simultaneous
        # scheduling or an unrelated wait elsewhere in the disposable DB.
        connected, observed = {blocker_pid}, []
        changed = True
        while changed:
            changed = False
            for row in rows:
                if row[0] not in connected and connected.intersection(row[1]):
                    connected.add(row[0])
                    observed.append(row)
                    changed = True
        if len(observed) >= count:
            assert all(row[2] == "Lock" and row[3] for row in observed)
            return observed
        await asyncio.sleep(0.01)
    raise AssertionError("Expected actual PostgreSQL blocked transactions were not observed")


@asynccontextmanager
async def held(factory, entity, predicate):
    async with factory() as blocker:
        await blocker.execute(select(entity).where(predicate).with_for_update())
        pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
        try:
            yield blocker, pid
        finally:
            await blocker.rollback()


@pytest.mark.asyncio
async def test_reservation_and_exact_replay_have_actual_overlap(db_session):
    runtime, provider, registry, _ = await setup_business(db_session)
    duplicate = await BusinessRuntime.open(runtime.request, runtime.grant, db_session, registry)
    async with held(db_session, AgentTemplate, AgentTemplate.id == runtime.request.agent_id) as (blocker, pid):
        one = asyncio.create_task(runtime.freeze_plan(actions("12")))
        two = asyncio.create_task(duplicate.freeze_plan(actions("12")))
        waits = await observe_blocked(db_session, pid, count=2)
        assert len({row[0] for row in waits}) == 2
        await blocker.rollback()
        results = await asyncio.wait_for(asyncio.gather(one, two, return_exceptions=True), 5)
    committed = [item for item in results if not isinstance(item, Exception)]
    refused = [item for item in results if isinstance(item, Exception)]
    assert committed and all(isinstance(item, BusinessSafetyError) and item.code == "ADMISSION_CONTENDED" for item in refused)
    first = committed[0]
    second = await duplicate.freeze_plan(actions("12"))
    assert first.id == second.id and first.operations[0].id == second.operations[0].id and provider.posts == 0
    async with held(db_session, BusinessScope, BusinessScope.id == first.scope.scope_id) as (blocker, pid):
        replay = asyncio.create_task(runtime.freeze_plan(actions("12")))
        await observe_blocked(db_session, pid)
        await blocker.rollback()
        assert (await asyncio.wait_for(replay, 5)).id == first.id


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_same_operation_and_distinct_turn_dispatch_permission_overlap(db_session, reverse):
    runtime, _, registry, _ = await setup_business(db_session)
    other = await BusinessRuntime.open(later(runtime.request), runtime.grant, db_session, registry)
    plan1, plan2 = await runtime.freeze_plan(actions("12")), await other.freeze_plan(actions("13"))
    contenders = [(runtime, plan1), (other, plan2)]
    if reverse:
        contenders.reverse()
    winner, winning_plan = contenders[0]
    loser, losing_plan = contenders[1]
    async with held(db_session, BusinessScope, BusinessScope.id == plan1.scope.scope_id) as (blocker, pid):
        first = asyncio.create_task(winner.repository.claim_dispatch(winner.request, winner.grant,
            winning_plan.id, winning_plan.operations[0].id, winner.registration, winner.facts_resolver(actions("12"))))
        await observe_blocked(db_session, pid)
        await blocker.rollback()
        allowed = await asyncio.wait_for(first, 5)
    assert allowed.token is not None
    async with held(db_session, BusinessScope, BusinessScope.id == plan1.scope.scope_id) as (blocker, pid):
        losing = asyncio.create_task(loser.repository.claim_dispatch(loser.request, loser.grant,
            losing_plan.id, losing_plan.operations[0].id, loser.registration, loser.facts_resolver(actions("13"))))
        await observe_blocked(db_session, pid)
        await blocker.rollback()
        blocked = await asyncio.wait_for(losing, 5)
    assert blocked.token is None and blocked.reason == "BLOCKED_UNRESOLVED_EFFECT"
    assert blocked.plan.operations[0].status == "RESERVED" and blocked.plan.operations[0].attempt_count == 0
    duplicate = await winner.repository.claim_dispatch(winner.request, winner.grant, winning_plan.id,
        winning_plan.operations[0].id, winner.registration, winner.facts_resolver(actions("12")))
    assert duplicate.token is None and duplicate.reason == "IN_FLIGHT"


@pytest.mark.asyncio
async def test_receipt_finalize_overlap_and_stale_token_cannot_overwrite(db_session):
    runtime, provider, _, _ = await setup_business(db_session)
    plan = await runtime.freeze_plan(actions("12"))
    decision = await runtime.repository.claim_dispatch(runtime.request, runtime.grant, plan.id, plan.operations[0].id,
                                                       runtime.registration, runtime.facts_resolver(actions("12")))
    receipt = await runtime.registration.adapter.execute(decision.token.dispatch, execution=None)
    async with held(db_session, BusinessScope, BusinessScope.id == plan.scope.scope_id) as (blocker, pid):
        finalizer = asyncio.create_task(runtime.repository.finalize(runtime.request, runtime.grant, decision.token, receipt))
        await observe_blocked(db_session, pid)
        await blocker.rollback()
        succeeded = await asyncio.wait_for(finalizer, 5)
    await runtime.repository.mark_unknown(runtime.request, runtime.grant, decision.token, lookup_available=True)
    assert (await runtime.repository.finalize(runtime.request, runtime.grant, decision.token, receipt)).operations[0].status == "SUCCEEDED"
    assert succeeded.operations[0].receipt_id == receipt.receipt_id and provider.posts == 1


@pytest.mark.asyncio
async def test_reconciliation_permission_and_finalization_overlap(db_session):
    runtime, provider, _, _ = await setup_business(db_session)
    provider.lose_next = True
    await runtime.execute_plan(actions("12"))
    plan = await runtime.repository.selected_plan(runtime.request, runtime.grant)
    async with held(db_session, BusinessScope, BusinessScope.id == plan.scope.scope_id) as (blocker, pid):
        claim = asyncio.create_task(runtime.repository.claim_reconcile(runtime.request, runtime.grant,
            plan.id, plan.operations[0].id, timeout_seconds=1))
        await observe_blocked(db_session, pid)
        await blocker.rollback()
        token = await asyncio.wait_for(claim, 5)
    assert token is not None
    assert await runtime.repository.claim_reconcile(runtime.request, runtime.grant, plan.id, plan.operations[0].id,
                                                    timeout_seconds=1) is None
    receipt = await runtime.registration.adapter.lookup(token.command, execution=None)
    async with held(db_session, BusinessScope, BusinessScope.id == plan.scope.scope_id) as (blocker, pid):
        finalize = asyncio.create_task(runtime.repository.finalize_reconcile(runtime.request, runtime.grant, token, receipt))
        await observe_blocked(db_session, pid)
        await blocker.rollback()
        resolved = await asyncio.wait_for(finalize, 5)
    assert resolved.operations[0].status == "SUCCEEDED" and provider.posts == 1 and provider.lookups == 1


@pytest.mark.asyncio
async def test_confirmation_competitors_reload_after_actual_scope_wait(db_session):
    origin, provider, registry, _ = await setup_business(db_session, confirmation=True)
    proposal = await origin.freeze_plan(actions("12"), propose=True)
    request = later(origin.request, "confirm " + str(proposal.id))
    one = await BusinessRuntime.open(request, origin.grant, db_session, registry)
    two = await BusinessRuntime.open(request, origin.grant, db_session, registry)
    facts = one.facts_resolver(actions("12"))
    async with held(db_session, AgentTemplate, AgentTemplate.id == request.agent_id) as (blocker, pid):
        confirmations = [asyncio.create_task(runtime.repository.confirm_plan(request, origin.grant,
                            proposal.id, runtime.registration, facts)) for runtime in (one, two)]
        await observe_blocked(db_session, pid, count=2)
        await blocker.rollback()
        results = await asyncio.wait_for(asyncio.gather(*confirmations, return_exceptions=True), 5)
    confirmed = [item for item in results if not isinstance(item, Exception)]
    refused = [item for item in results if isinstance(item, Exception)]
    assert confirmed and all(isinstance(item, BusinessSafetyError) and item.code == "ADMISSION_CONTENDED" for item in refused)
    assert all(p.id == proposal.id and p.proposal_state == "CONFIRMED" for p in confirmed)
    assert (await two.repository.confirm_plan(request, origin.grant, proposal.id, two.registration,
                                              two.facts_resolver(actions("12")))).id == proposal.id
    await one.confirm(proposal.id)
    await two.confirm(proposal.id)
    assert provider.posts == 1


@pytest.mark.asyncio
async def test_generation_change_wins_post_lock_dispatch_read(db_session):
    runtime, provider, _, binding = await setup_business(db_session)
    plan = await runtime.freeze_plan(actions("12"))
    facts = runtime.facts_resolver(actions("12"))
    async with db_session() as changer:
        await changer.execute(select(AgentTemplate).where(AgentTemplate.id == runtime.request.agent_id).with_for_update())
        await changer.execute(select(BusinessBinding).where(BusinessBinding.id == binding.id).with_for_update())
        await changer.execute(update(BusinessBinding).where(BusinessBinding.id == binding.id)
                              .values(authorization_generation=2))
        pid = await changer.scalar(text("SELECT pg_backend_pid()"))
        dispatch = asyncio.create_task(runtime.repository.claim_dispatch(runtime.request, runtime.grant,
            plan.id, plan.operations[0].id, runtime.registration, facts))
        await observe_blocked(db_session, pid)
        await changer.commit()
        denied = await asyncio.wait_for(dispatch, 5)
    assert denied.token is None and denied.reason == "AUTHORITY_DENIED"
    assert denied.plan.operations[0].status == "FAILED" and provider.posts == 0


@pytest.mark.asyncio
async def test_expiry_normalization_overlap_blocks_an_already_reserved_plan(db_session, monkeypatch):
    first, _, registry, _ = await setup_business(db_session)
    second = await BusinessRuntime.open(later(first.request), first.grant, db_session, registry)
    plan1, plan2 = await first.freeze_plan(actions("12")), await second.freeze_plan(actions("13"))
    permitted = await first.repository.claim_dispatch(first.request, first.grant, plan1.id, plan1.operations[0].id,
                                                       first.registration, first.facts_resolver(actions("12")))
    async with db_session() as session:
        await session.execute(update(BusinessOperation).where(BusinessOperation.id == permitted.token.operation_id)
                              .values(dispatch_lease_expires_at=func.clock_timestamp() - text("interval '1 second'")))
        await session.commit()
    locked, release = asyncio.Event(), asyncio.Event()
    real_lock = first.repository._lock_scope
    owner = {}
    async def gated_lock(session, scope_id):
        row = await real_lock(session, scope_id)
        owner["pid"] = await session.scalar(text("SELECT pg_backend_pid()"))
        locked.set()
        await release.wait()
        return row
    monkeypatch.setattr(first.repository, "_lock_scope", gated_lock)
    # Isolate normalization's COMMIT. The runtime wrapper also performs a
    # separate result-gate read whose scope lock can legitimately cause the
    # contender's decisive NOWAIT phase to return ADMISSION_CONTENDED.
    recovery = asyncio.create_task(first.repository.recover_attempts(first.request, first.grant))
    await asyncio.wait_for(locked.wait(), 5)
    contender = asyncio.create_task(second.repository.claim_dispatch(second.request, second.grant, plan2.id,
        plan2.operations[0].id, second.registration, second.facts_resolver(actions("13"))))
    await observe_blocked(db_session, owner["pid"])
    release.set()
    assert await asyncio.wait_for(recovery, 5) == 1
    blocked = await asyncio.wait_for(contender, 5)
    assert blocked.reason == "BLOCKED_UNRESOLVED_EFFECT" and blocked.token is None
    assert blocked.plan.operations[0].status == "RESERVED"
    retained = await first.repository.selected_plan(first.request, first.grant)
    assert retained.operations[0].status == "UNKNOWN"


@pytest.mark.asyncio
async def test_no_database_lock_spans_provider_and_an_independent_scope_progresses(db_session):
    runtime, provider, _, _ = await setup_business(db_session)
    plan = await runtime.freeze_plan(actions("12"))
    decision = await runtime.repository.claim_dispatch(runtime.request, runtime.grant, plan.id,
        plan.operations[0].id, runtime.registration, runtime.facts_resolver(actions("12")))
    # Permission returned COMMIT. A distinct connection can acquire every row
    # before the fake external response is available; no network transaction.
    async with db_session() as observer:
        for entity, predicate in ((AgentTemplate, AgentTemplate.id == runtime.request.agent_id),
                                  (BusinessBinding, BusinessBinding.id == runtime.binding.id),
                                  (BusinessScope, BusinessScope.id == plan.scope.scope_id),
                                  (BusinessPlan, BusinessPlan.id == plan.id),
                                  (BusinessOperation, BusinessOperation.id == decision.token.operation_id)):
            await observer.execute(select(entity).where(predicate).with_for_update(nowait=True))
        await observer.rollback()
    independent, independent_provider, _, _ = await setup_business(db_session)
    await independent.execute_plan(actions("12"))
    assert independent_provider.posts == 1 and provider.posts == 0


@pytest.mark.asyncio
async def test_new_plan_freeze_races_dispatch_commit_without_current_state_gap(db_session, monkeypatch):
    first, _, registry, _ = await setup_business(db_session)
    second = await BusinessRuntime.open(later(first.request), first.grant, db_session, registry)
    plan = await first.freeze_plan(actions("12"))
    locked, release, owner = asyncio.Event(), asyncio.Event(), {}
    real_lock = first.repository._lock_scope
    async def gate(session, scope_id):
        scope = await real_lock(session, scope_id)
        if not session.info.get("business_nowait"):
            return scope
        owner["pid"] = await session.scalar(text("SELECT pg_backend_pid()"))
        locked.set()
        await release.wait()
        return scope
    monkeypatch.setattr(first.repository, "_lock_scope", gate)
    admission = asyncio.create_task(first.repository.claim_dispatch(first.request, first.grant, plan.id,
        plan.operations[0].id, first.registration, first.facts_resolver(actions("12"))))
    await asyncio.wait_for(locked.wait(), 5)
    freezing = asyncio.create_task(second.freeze_plan(actions("13")))
    await observe_blocked(db_session, owner["pid"])
    release.set()
    assert (await asyncio.wait_for(admission, 5)).token is not None
    from schemas.business import BusinessSafetyError
    with pytest.raises(BusinessSafetyError, match="BLOCKED_UNRESOLVED_EFFECT"):
        await asyncio.wait_for(freezing, 5)
    assert await second.repository.selected_plan(second.request, second.grant) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["SUCCEEDED", "FAILED"])
async def test_terminal_commit_releases_slot_before_waiting_candidate_admission(db_session, monkeypatch, terminal):
    first, _, registry, _ = await setup_business(db_session)
    second = await BusinessRuntime.open(later(first.request), first.grant, db_session, registry)
    plan1, plan2 = await first.freeze_plan(actions("12")), await second.freeze_plan(actions("13"))
    decision = await first.repository.claim_dispatch(first.request, first.grant, plan1.id, plan1.operations[0].id,
        first.registration, first.facts_resolver(actions("12")))
    from schemas.business import DefinitiveNoEffect
    receipt = (SucceededReceipt(binding=decision.token.command.receipt_binding(),receipt_id="resolved-1",object_id="object-1",evidence={"proof":"SUCCEEDED"})
               if terminal == "SUCCEEDED" else DefinitiveNoEffect(binding=decision.token.command.receipt_binding(),
                   receipt_id="rejected-1", reason_code="DEFINITIVE_NO_EFFECT", evidence={"proof":"NO_EFFECT"}))
    locked, release, owner = asyncio.Event(), asyncio.Event(), {}
    real_lock = first.repository._lock_scope
    async def gate(session, scope_id):
        scope = await real_lock(session, scope_id)
        owner["pid"] = await session.scalar(text("SELECT pg_backend_pid()"))
        locked.set()
        await release.wait()
        return scope
    monkeypatch.setattr(first.repository, "_lock_scope", gate)
    finishing = asyncio.create_task(first.repository.finalize(first.request, first.grant, decision.token, receipt))
    await asyncio.wait_for(locked.wait(), 5)
    contender = asyncio.create_task(second.repository.claim_dispatch(second.request, second.grant, plan2.id,
        plan2.operations[0].id, second.registration, second.facts_resolver(actions("13"))))
    await observe_blocked(db_session, owner["pid"])
    release.set()
    assert (await asyncio.wait_for(finishing,5)).operations[0].status == terminal
    admitted = await asyncio.wait_for(contender,5)
    assert admitted.token.operation_id == plan2.operations[0].id
    assert admitted.token.command.issued_at == plan2.operations[0].command.issued_at
    assert admitted.plan.expires_at == plan2.expires_at


@pytest.mark.asyncio
async def test_another_turn_can_win_between_ordered_actions_without_erasing_residual(db_session):
    first, _, registry, _ = await setup_business(db_session)
    second = await BusinessRuntime.open(later(first.request), first.grant, db_session, registry)
    plan1, plan2 = await first.freeze_plan(actions("12","13")), await second.freeze_plan(actions("14"))
    first_permission = await first.repository.claim_dispatch(first.request,first.grant,plan1.id,plan1.operations[0].id,
        first.registration,first.facts_resolver(actions("12","13")))
    await first._send(first_permission.token)
    other_permission = await second.repository.claim_dispatch(second.request,second.grant,plan2.id,plan2.operations[0].id,
        second.registration,second.facts_resolver(actions("14")))
    blocked = await first.repository.claim_dispatch(first.request,first.grant,plan1.id,plan1.operations[1].id,
        first.registration,first.facts_resolver(actions("12","13")))
    assert blocked.reason == "BLOCKED_UNRESOLVED_EFFECT" and blocked.token is None
    assert [op.status for op in blocked.plan.operations] == ["SUCCEEDED","RESERVED"]
    await second._send(other_permission.token)
    await first.execute_plan(actions("12","13"))
    assert {ref.outcome for ref in first.collector.snapshot()} == {"SUCCEEDED"}


@pytest.mark.asyncio
async def test_native_original_unknown_is_the_only_exception_and_competing_tokens_are_fenced(db_session):
    first, provider, registry, _ = await setup_business(db_session,mode="NATIVE")
    second = await BusinessRuntime.open(later(first.request),first.grant,db_session,registry)
    plan1, plan2 = await first.freeze_plan(actions("12")),await second.freeze_plan(actions("13"))
    provider.lose_next = True
    await first.execute_plan(actions("12"))
    facts = first.facts_resolver(actions("12"))
    async with held(db_session,BusinessScope,BusinessScope.id==plan1.scope.scope_id) as (blocker,pid):
        recovering = asyncio.create_task(first.repository.claim_dispatch(first.request,first.grant,plan1.id,
            plan1.operations[0].id,first.registration,facts,native_replay=True))
        await observe_blocked(db_session,pid)
        await blocker.rollback()
        admitted = await asyncio.wait_for(recovering,5)
    assert admitted.token.command.provider_idempotency_key == plan1.operations[0].command.provider_idempotency_key
    assert admitted.token.command.issued_at == plan1.operations[0].command.issued_at
    repeat = await first.repository.claim_dispatch(first.request,first.grant,plan1.id,plan1.operations[0].id,
        first.registration,facts,native_replay=True)
    assert repeat.token is None and repeat.reason == "IN_FLIGHT"
    contender = await second.repository.claim_dispatch(second.request,second.grant,plan2.id,plan2.operations[0].id,
        second.registration,second.facts_resolver(actions("13")),native_replay=True)
    assert contender.token is None
    await first._send(admitted.token)
    assert provider.posts == 2 and len(provider.objects)==1


@pytest.mark.asyncio
async def test_blind_unknown_restarts_keep_all_candidates_blocked(db_session):
    first,provider,registry,_ = await setup_business(db_session,mode="BLIND")
    second = await BusinessRuntime.open(later(first.request),first.grant,db_session,registry)
    await first.freeze_plan(actions("12"))
    plan2 = await second.freeze_plan(actions("13"))
    provider.lose_next=True
    await first.execute_plan(actions("12"))
    for _ in range(3):
        restarted = await BusinessRuntime.open(second.request,second.grant,db_session,registry)
        result=await restarted.execute_plan(actions("13"))
        assert result.halt_status=="UNKNOWN"
        current=await restarted.repository.selected_plan(second.request,second.grant)
        assert current.operations[0].id==plan2.operations[0].id and current.operations[0].state_version==0
        assert current.expires_at==plan2.expires_at
    assert provider.posts==1 and provider.lookups==0
