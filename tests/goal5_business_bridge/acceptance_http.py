"""Paired Conversation/AI/provider acceptance through owned real HTTP processes."""

import asyncio
import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.goal3_business_cross_service.fake_provider import completion, tool_call


pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_real_http_same_turn_write_safe_has_conversation_permit(bridge):
    await bridge.script(slots=("12:00",))
    accepted = await bridge.ingest()
    claim = await bridge.claim(await bridge.turn(accepted))

    response = await bridge.execute(claim)
    assert response.status_code == 200, response.text[:300]
    turn = response.json()["turn"]
    assert turn["status"] == "COMPLETED"

    operations = await bridge.operations(turn["id"])
    assert len(operations) == 1
    operation = operations[0]
    assert operation["status"] == "SUCCEEDED"
    assert operation["effect_permit_id"] is not None
    permits = await bridge.permit_rows()
    assert len(permits) == 1
    assert str(permits[0]["id"]) == str(operation["effect_permit_id"])
    assert str(permits[0]["turn_id"]) == str(turn["id"])
    assert str(permits[0]["origin_turn_id"]) == str(turn["id"])

    counts, objects = await bridge.provider_counts()
    assert counts.get("POST", 0) == objects == 1
    assert any(
        row["path"] == "/internal/v1/external-effect-permits"
        and row["headers"].get("X-Service-Token") == bridge.tokens["effect"]
        and "Authorization" not in row["headers"]
        for row in bridge.effect_proxy.snapshot()
    )


async def _propose_confirmation(bridge, *, thread=None, telegram=False):
    action = {"capability": "booking.confirm_create", "arguments": {"slot": "12:00"}}
    await bridge.script(model=[
        {"body": completion(None, [tool_call("business_propose", {"actions": [action]})])},
        {"body": completion("Подтвердите запись.")},
    ])
    accepted = (await bridge.telegram_ingest("Запиши меня на 12:00", thread=thread)
                if telegram else await bridge.ingest("Запиши меня на 12:00", thread=thread))
    claim = await bridge.claim(await bridge.turn(accepted))
    response = await bridge.execute(claim)
    assert response.status_code == 200, response.text[:300]
    origin = response.json()["turn"]
    assert origin["status"] == "COMPLETED" and origin["id"] == claim["turn"]["id"]
    operations = await bridge.operations(origin["id"])
    assert len(operations) == 1 and operations[0]["status"] == "RESERVED"
    assert (await bridge.provider_counts())[0].get("POST", 0) == 0
    return accepted, origin, operations[0]


async def _confirm_model(bridge, proposal_id):
    await bridge.script(model=[
        {"body": completion(None, [tool_call("business_decide_proposal", {
            "proposal_id": str(proposal_id), "intent": "CONFIRM"})])},
        {"body": completion("Запись подтверждена.")},
    ])


def _eligibility_observations(bridge):
    return [
        (json.loads(call["body"]), json.loads(call["response_body"]))
        for call in bridge.effect_proxy.snapshot()
        if call["path"] == "/internal/v1/external-effect-eligibility"
        and call.get("response_status") == 200
    ]


async def test_real_http_confirmation_uses_distinct_proposal_origin(bridge):
    accepted, origin, operation = await _propose_confirmation(bridge)

    # The business operation id is not the proposal id; retrieve the immutable
    # plan identity retained by the operation before exercising the later Turn.
    async with bridge.engines["ai"].connect() as connection:
        proposal_id = await connection.scalar(text(
            "SELECT plan_id FROM business_operations WHERE id=:operation_id"
        ), {"operation_id": operation["id"]})
    thread = accepted["conversation"]["provider_thread_id"]
    confirmation = await bridge.ingest("Да", thread=thread)
    current = await bridge.claim(await bridge.turn(confirmation, 2, 2))
    bridge.effect_proxy.clear_capture()
    await _confirm_model(bridge, proposal_id)
    response = await bridge.execute(current)

    assert response.status_code == 200, response.text[:300]
    current_turn = response.json()["turn"]
    assert current_turn["status"] == "COMPLETED", {
        key: current_turn.get(key)
        for key in ("status", "failure_code", "outcome", "draft_text", "operation_refs")
    }
    assert current_turn["id"] != origin["id"]
    plan_operations = await bridge.operations(origin["id"])
    assert len(plan_operations) == 1 and plan_operations[0]["status"] == "SUCCEEDED"
    assert await bridge.operations(current_turn["id"]) == []
    permits = await bridge.permit_rows()
    assert len(permits) == 1
    assert str(permits[0]["turn_id"]) == str(current_turn["id"])
    assert str(permits[0]["origin_turn_id"]) == str(origin["id"])
    counts, objects = await bridge.provider_counts()
    assert counts.get("POST", 0) == objects == 1
    managed = bridge.ai_request()
    assert managed is not None and managed["turn_id"] == str(current_turn["id"])


async def test_same_burst_correction_blocks_later_confirmation_over_real_http(bridge):
    accepted, origin, operation = await _propose_confirmation(bridge, telegram=True)
    async with bridge.engines["ai"].connect() as connection:
        proposal_id = await connection.scalar(text(
            "SELECT plan_id FROM business_operations WHERE id=:operation_id"
        ), {"operation_id": operation["id"]})

    thread = accepted["conversation"]["provider_thread_id"]
    await bridge.configure_settling({
        "version": "goal5-bridge-split-v1", "channel": "TELEGRAM",
        "integration_id": bridge.ids["integration"],
        "quiet_window_ms": 500, "max_wait_ms": 5000,
    })
    burst_start = await bridge.telegram_ingest("Да", thread=thread)
    conversation_id = burst_start["conversation"]["id"]
    async with bridge.engines["conversation"].connect() as connection:
        first_sequence = await connection.scalar(text(
            "SELECT last_message_sequence FROM conversations WHERE id=:id"
        ), {"id": conversation_id})
    seeded = await bridge.insert_interleaved_history(
        conversation_id, origin["id"], first_sequence,
    )
    correction = await bridge.telegram_ingest("Нет, перенеси на 14:00", thread=thread)
    correction_sequence = correction["conversation"]["last_message_sequence"]
    async with bridge.engines["conversation"].connect() as connection:
        members = (await connection.execute(text(
            "SELECT sequence,actor,settling_burst_id FROM messages "
            "WHERE conversation_id=:id AND sequence BETWEEN :first AND :last "
            "ORDER BY sequence"
        ), {"id": conversation_id, "first": first_sequence,
            "last": correction_sequence})).mappings().all()
    assert [row["actor"] for row in members] == ["CUSTOMER", "AGENT", "CUSTOMER"]
    assert str(members[0]["settling_burst_id"]) == str(members[2]["settling_burst_id"])
    assert members[1]["settling_burst_id"] is None
    assert int(seeded["agent_sequence"]) == int(first_sequence) + 1

    # Close the owned burst at its real PostgreSQL-ready boundary, then use the
    # actual Telegram ingress with settling disabled for the isolated reply.
    await bridge.wait_burst_ready(seeded["burst_id"])
    async with bridge.engines["conversation"].begin() as connection:
        await connection.execute(text(
            "UPDATE settling_bursts SET closed_at=ready_at WHERE id=:id "
            "AND ready_at<=clock_timestamp()"
        ), {"id": seeded["burst_id"]})
    await bridge.configure_settling(None)
    later = await bridge.telegram_ingest("Да", thread=thread)
    later_sequence = later["conversation"]["last_message_sequence"]
    async with bridge.engines["conversation"].connect() as connection:
        latest = (await connection.execute(text(
            "SELECT settling_burst_id,settling_policy_version FROM messages "
            "WHERE conversation_id=:id AND sequence=:sequence"
        ), {"id": conversation_id, "sequence": later_sequence})).mappings().one()
    assert latest["settling_burst_id"] is None
    assert latest["settling_policy_version"] is None

    await _confirm_model(bridge, proposal_id)
    claim = await bridge.claim(await bridge.turn(later, later_sequence, later_sequence))
    response = await bridge.execute(claim)
    assert response.status_code == 200, response.text[:300]
    result_turn = response.json()["turn"]
    assert result_turn["id"] != origin["id"]
    eligibility = _eligibility_observations(bridge)
    assert any(request["turn_id"] == result_turn["id"]
               and request["origin_turn_id"] == origin["id"]
               and result["reason"] == "INTERVENING_SPLIT"
               and result["eligible"] is False
               for request, result in eligibility)
    assert any(request["turn_id"] == result_turn["id"]
               and request["origin_turn_id"] == result_turn["id"]
               and result["reason"] == "ELIGIBLE"
               and result["eligible"] is True
               for request, result in eligibility)
    assert not any(call["path"] == "/internal/v1/external-effect-permits"
                   for call in bridge.effect_proxy.snapshot())
    async with bridge.engines["ai"].connect() as connection:
        current = (await connection.execute(text(
            "SELECT attempt_count,origin_turn_id,effect_permit_id FROM business_operations "
            "WHERE id=:id"
        ), {"id": operation["id"]})).mappings().one()
    assert current["attempt_count"] == 0
    assert current["effect_permit_id"] is None
    assert str(current["origin_turn_id"]) == str(origin["id"])
    assert await bridge.operations(result_turn["id"]) == []
    counts, objects = await bridge.provider_counts()
    assert counts.get("POST", 0) == objects == 0
    assert await bridge.permit_rows() == []

    # A complete distinct intent after the split must semantically classify the
    # prior proposal as changed, safely retire only that unacquired identity,
    # then create a new proposal with this Turn as its immutable origin.
    await bridge.script(model=[
        {"body": completion(None, [tool_call("business_decide_proposal", {
            "proposal_id": str(proposal_id), "intent": "CHANGE",
        })])},
        {"body": completion(None, [tool_call("business_propose", {"actions": [{
            "capability": "booking.confirm_create", "arguments": {"slot": "14:00"},
        }]})])},
        {"body": completion("Подтвердите новую запись на 14:00.")},
    ])
    fresh = await bridge.telegram_ingest("Запиши меня на 14:00", thread=thread)
    fresh_sequence = fresh["conversation"]["last_message_sequence"]
    fresh_claim = await bridge.claim(await bridge.turn(fresh, fresh_sequence, fresh_sequence))
    bridge.effect_proxy.clear_capture()
    fresh_response = await bridge.execute(fresh_claim)
    assert fresh_response.status_code == 200, fresh_response.text[:300]
    fresh_turn = fresh_response.json()["turn"]
    assert fresh_turn["status"] == "COMPLETED", {
        key: fresh_turn.get(key)
        for key in ("status", "failure_code", "outcome", "draft_text", "operation_refs")
    }
    assert fresh_turn["id"] != origin["id"]
    fresh_eligibility = _eligibility_observations(bridge)
    assert any(request["turn_id"] == fresh_turn["id"]
               and request["origin_turn_id"] == origin["id"]
               and result["reason"] == "INTERVENING_SPLIT"
               and result["eligible"] is False
               for request, result in fresh_eligibility)
    assert any(request["turn_id"] == fresh_turn["id"]
               and request["origin_turn_id"] == fresh_turn["id"]
               and result["reason"] == "ELIGIBLE"
               and result["eligible"] is True
               for request, result in fresh_eligibility)
    async with bridge.engines["ai"].connect() as connection:
        fresh_operation = (await connection.execute(text(
            "SELECT o.id,o.status,o.attempt_count,o.origin_turn_id,o.effect_permit_id,o.plan_id "
            "FROM business_operations o "
            "JOIN business_plans p ON p.id=o.plan_id "
            "WHERE p.origin_turn_id=:turn_id"
        ), {"turn_id": fresh_turn["id"]})).mappings().all()
    assert len(fresh_operation) == 1
    assert fresh_operation[0]["status"] == "RESERVED"
    assert fresh_operation[0]["attempt_count"] == 0
    assert fresh_operation[0]["effect_permit_id"] is None
    assert str(fresh_operation[0]["origin_turn_id"]) == str(fresh_turn["id"])
    async with bridge.engines["ai"].connect() as connection:
        retired = (await connection.execute(text(
            "SELECT p.proposal_state,o.status,o.failure_code,"
            "o.receipt_evidence->>'kind' AS receipt_kind,o.attempt_count,"
            "o.dispatch_started_at,o.effect_permit_id "
            "FROM business_operations o JOIN business_plans p ON p.id=o.plan_id "
            "WHERE o.id=:id"
        ), {"id": operation["id"]})).mappings().one()
    assert retired["proposal_state"] == "INVALIDATED"
    assert retired["status"] == "FAILED"
    assert retired["failure_code"] == "STALE_INTERVENING_SPLIT"
    assert retired["receipt_kind"] == "NO_DISPATCH"
    assert retired["attempt_count"] == 0
    assert retired["dispatch_started_at"] is None
    assert retired["effect_permit_id"] is None
    assert await bridge.permit_rows() == []
    counts, objects = await bridge.provider_counts()
    assert counts.get("POST", 0) == objects == 0

    # Only a separate later confirmation of the replacement proposal may write.
    fresh_proposal_id = fresh_operation[0]["plan_id"]
    await _confirm_model(bridge, fresh_proposal_id)
    fresh_confirmation = await bridge.telegram_ingest("Да", thread=thread)
    confirmation_sequence = fresh_confirmation["conversation"]["last_message_sequence"]
    confirmation_claim = await bridge.claim(await bridge.turn(
        fresh_confirmation, confirmation_sequence, confirmation_sequence,
    ))
    confirmation_response = await bridge.execute(confirmation_claim)
    assert confirmation_response.status_code == 200, confirmation_response.text[:300]
    confirmation_turn = confirmation_response.json()["turn"]
    assert confirmation_turn["status"] == "COMPLETED", {
        key: confirmation_turn.get(key)
        for key in ("status", "failure_code", "outcome", "draft_text", "operation_refs")
    }
    assert confirmation_turn["id"] != fresh_turn["id"]
    old_operations = await bridge.operations(origin["id"])
    assert len(old_operations) == 1
    assert old_operations[0]["effect_permit_id"] is None
    new_operations = await bridge.operations(fresh_turn["id"])
    assert len(new_operations) == 1 and new_operations[0]["status"] == "SUCCEEDED"
    assert new_operations[0]["effect_permit_id"] is not None
    permits = await bridge.permit_rows()
    assert len(permits) == 1
    assert str(permits[0]["turn_id"]) == str(confirmation_turn["id"])
    assert str(permits[0]["origin_turn_id"]) == str(fresh_turn["id"])
    counts, objects = await bridge.provider_counts()
    assert counts.get("POST", 0) == objects == 1


async def test_authority_lock_wins_before_permit_and_prevents_provider_write(bridge):
    await bridge.script(slots=("12:00",))
    accepted = await bridge.ingest()
    conversation_id = accepted["conversation"]["id"]
    claim = await bridge.claim(await bridge.turn(accepted))
    entered, release = bridge.effect_proxy.arm("before")
    running = asyncio.create_task(bridge.execute(claim))
    try:
        assert await asyncio.to_thread(entered.wait, 15), "Permit request did not reach barrier"
        await bridge.authority(conversation_id, "HUMAN")
    finally:
        release.set()
    response = await asyncio.wait_for(running, 20)
    assert response.status_code == 200, response.text[:300]
    operations = await bridge.operations(claim["turn"]["id"])
    assert len(operations) == 1
    assert operations[0]["status"] != "DISPATCHING"
    assert operations[0]["effect_permit_id"] is None
    assert await bridge.permit_rows() == []
    counts, objects = await bridge.provider_counts()
    assert counts.get("POST", 0) == objects == 0


async def test_permit_commit_wins_authority_lock_and_never_grants_next_ordinal(bridge):
    await bridge.script(slots=("12:00", "13:00"))
    accepted = await bridge.ingest()
    conversation_id = accepted["conversation"]["id"]
    claim = await bridge.claim(await bridge.turn(accepted))
    entered, release = bridge.effect_proxy.arm("after")
    running = asyncio.create_task(bridge.execute(claim))
    try:
        assert await asyncio.to_thread(entered.wait, 15), "Committed permit did not reach barrier"
        permits = await bridge.permit_rows()
        assert len(permits) == 1
        await bridge.authority(conversation_id, "HUMAN")
    finally:
        release.set()
    response = await asyncio.wait_for(running, 25)
    assert response.status_code == 200, response.text[:300]
    operations = await bridge.operations(claim["turn"]["id"])
    assert len(operations) == 2
    first = next(value for value in operations if value["effect_permit_id"] is not None)
    assert first["status"] == "SUCCEEDED"
    assert sum(value["effect_permit_id"] is not None for value in operations) == 1
    assert len(await bridge.permit_rows()) == 1
    counts, objects = await bridge.provider_counts()
    assert counts.get("POST", 0) == objects == 1


@pytest.mark.parametrize(
    ("order", "iteration"),
    [(order, iteration) for order in ("ingress_first", "permit_first")
     for iteration in range(1, 4)],
    ids=lambda value: str(value),
)
async def test_fresh_ingress_and_permit_are_serialized_by_actual_conversation_lock(
    bridge, order, iteration,
):
    thread = "goal5-ingress-race-" + str(uuid4())
    await bridge.script(slots=("12:00",))
    accepted = await bridge.ingest(thread=thread)
    claim = await bridge.claim(await bridge.turn(accepted))
    stage = "before" if order == "ingress_first" else "after"
    entered, release = bridge.effect_proxy.arm(stage)
    running = asyncio.create_task(bridge.execute(claim))
    try:
        assert await asyncio.to_thread(entered.wait, 15), "Permit request did not reach race barrier"
        await bridge.ingest("Новое сообщение", thread=thread)
    finally:
        release.set()
    response = await asyncio.wait_for(running, 25)
    assert response.status_code == 200, response.text[:300]
    counts, objects = await bridge.provider_counts()
    if order == "ingress_first":
        assert await bridge.permit_rows() == []
        assert counts.get("POST", 0) == objects == 0
    else:
        assert len(await bridge.permit_rows()) == 1
        assert counts.get("POST", 0) == objects == 1


async def test_kill_after_permit_commit_before_dispatch_replays_same_operation(bridge):
    thread = "goal5-kill-before-dispatch-" + str(uuid4())
    await bridge.script(slots=("12:00",))
    accepted = await bridge.ingest(thread=thread)
    claim = await bridge.claim(await bridge.turn(accepted))
    entered, release = bridge.effect_proxy.arm("after")
    running = asyncio.create_task(bridge.execute(claim))
    try:
        assert await asyncio.to_thread(entered.wait, 15), "Committed permit did not reach barrier"
        permits_before = await bridge.permit_rows()
        assert len(permits_before) == 1
        await bridge.stop("ai", abrupt=True)
    finally:
        release.set()
    failed = await asyncio.wait_for(running, 12)
    assert failed.status_code == 200, failed.text[:300]
    await bridge.start("ai")
    queued = await bridge.ingest("Следующий вопрос", thread=thread)
    recovery_claim = await bridge.claim(await bridge.turn(queued, 2, 2))
    recovered = await bridge.execute(recovery_claim)
    assert recovered.status_code == 200, recovered.text[:300]
    operations = await bridge.operations(claim["turn"]["id"])
    assert len(operations) == 1
    assert operations[0]["status"] == "FAILED"
    assert operations[0]["effect_permit_id"] is None
    assert permits_before[0]["id"] == (await bridge.permit_rows())[0]["id"]
    assert len(await bridge.permit_rows()) == 1
    counts, objects = await bridge.provider_counts()
    assert counts.get("POST", 0) == objects == 0


async def test_kill_after_dispatching_commit_uses_provider_lookup_without_replacement(bridge):
    label = "goal5-after-dispatch-commit"
    thread = "goal5-kill-after-dispatch-" + str(uuid4())
    await bridge.script(slots=("12:00",), execute={"default": {"barrier": label}})
    accepted = await bridge.ingest(thread=thread)
    claim = await bridge.claim(await bridge.turn(accepted))
    running = asyncio.create_task(bridge.execute(claim))
    try:
        await bridge.wait_provider_barrier(label)
        operations = await bridge.operations(claim["turn"]["id"])
        assert len(operations) == 1 and operations[0]["status"] == "DISPATCHING"
        await bridge.stop("ai", abrupt=True)
    finally:
        await bridge.release_provider_barrier(label)
    finished = await asyncio.wait_for(running, 12)
    assert finished.status_code == 200, finished.text[:300]
    await bridge.wait_dispatch_expiry(claim["turn"]["id"])
    await bridge.start("ai")
    queued = await bridge.ingest("Следующий вопрос", thread=thread)
    recovery_claim = await bridge.claim(await bridge.turn(queued, 2, 2))
    recovered = await bridge.execute(recovery_claim)
    assert recovered.status_code == 200, recovered.text[:300]
    operations = await bridge.operations(claim["turn"]["id"])
    assert len(operations) == 1 and operations[0]["status"] == "SUCCEEDED"
    counts, objects = await bridge.provider_counts()
    assert counts.get("POST", 0) == objects == 1
    assert counts.get("LOOKUP", 0) >= 1


async def test_late_stamped_queued_customer_input_cannot_authorize_write(bridge):
    thread = "goal5-late-stamped-input-" + str(uuid4())
    await bridge.script(slots=("12:00",))
    first = await bridge.ingest("M1", thread=thread)
    await bridge.ingest("M2", thread=thread)
    await bridge.ingest("M3", thread=thread)
    claim = await bridge.claim(await bridge.turn(first, 2, 2))

    response = await bridge.execute(claim)
    assert response.status_code == 200, response.text[:300]
    counts, objects = await bridge.provider_counts()
    assert counts.get("POST", 0) == objects == 0
    calls = bridge.effect_proxy.snapshot()
    assert any(call["path"] == "/internal/v1/external-effect-eligibility" for call in calls)
    assert not any(call["path"] == "/internal/v1/external-effect-permits" for call in calls)


async def test_recovery_lock_wins_against_delayed_original_before_dispatch(bridge):
    thread = "goal5-delayed-worker-recovery-wins-" + str(uuid4())
    await bridge.script(slots=("12:00",))
    accepted = await bridge.ingest(thread=thread)
    claim = await bridge.claim(await bridge.turn(accepted))
    entered, release = bridge.effect_proxy.arm("after")
    detached = bridge.provider_proxy.detach_next("/internal/v1/managed-turns/execute")
    delayed = asyncio.create_task(bridge.execute(claim))
    try:
        if not await asyncio.to_thread(entered.wait, 15):
            raise AssertionError(
                "Committed permit did not reach barrier; "
                f"detached_started={detached['started'].is_set()} "
                f"detached_done={detached['done'].is_set()} "
                f"detached_status={detached['status']} body={detached.get('body', '')!r} "
                f"AI_paths={[row['path'] for row in bridge.provider_proxy.snapshot()]} "
                f"effect_paths={[row['path'] for row in bridge.effect_proxy.snapshot()]}"
            )
        detached["caller_release"].set()
        assert await asyncio.wait_for(delayed, 12)
        queued = await bridge.ingest("Следующий вопрос", thread=thread)
        recovery_claim = await bridge.claim(await bridge.turn(queued, 2, 2))
        recovered = await bridge.execute(recovery_claim)
        assert recovered.status_code == 200, recovered.text[:300]
        operations = await bridge.operations(claim["turn"]["id"])
        assert len(operations) == 1 and operations[0]["status"] == "FAILED"
        assert operations[0]["effect_permit_id"] is None
    finally:
        detached["caller_release"].set()
        release.set()
    assert await asyncio.to_thread(detached["done"].wait, 20)
    assert detached["status"] == 200
    operations = await bridge.operations(claim["turn"]["id"])
    assert len(operations) == 1 and operations[0]["status"] == "FAILED"
    assert len(await bridge.permit_rows()) == 1
    counts, objects = await bridge.provider_counts()
    assert counts.get("POST", 0) == objects == 0


async def test_original_dispatch_lock_wins_against_recovery_observer(bridge):
    label = "goal5-dispatch-wins-recovery"
    thread = "goal5-dispatch-worker-recovery-wins-" + str(uuid4())
    await bridge.script(slots=("12:00",), execute={"default": {"barrier": label}})
    accepted = await bridge.ingest(thread=thread)
    claim = await bridge.claim(await bridge.turn(accepted))
    detached = bridge.provider_proxy.detach_next("/internal/v1/managed-turns/execute")
    delayed = asyncio.create_task(bridge.execute(claim))
    try:
        await bridge.wait_provider_barrier(label)
        operations = await bridge.operations(claim["turn"]["id"])
        assert len(operations) == 1 and operations[0]["status"] == "DISPATCHING"
        detached["caller_release"].set()
        assert await asyncio.wait_for(delayed, 12)
        queued = await bridge.ingest("Следующий вопрос", thread=thread)
        recovery_claim = await bridge.claim(await bridge.turn(queued, 2, 2))
        recovered = await bridge.execute(recovery_claim)
        assert recovered.status_code == 200, recovered.text[:300]
        operations = await bridge.operations(claim["turn"]["id"])
        assert len(operations) == 1 and operations[0]["status"] == "DISPATCHING"
        assert len(await bridge.permit_rows()) == 1
        counts, objects = await bridge.provider_counts()
        assert counts.get("POST", 0) == objects == 1
        # A live DISPATCHING lease belongs to the delayed original worker.
        # Recovery observes it without racing a lookup or issuing a replacement.
        assert counts.get("LOOKUP", 0) == 0
    finally:
        detached["caller_release"].set()
        await bridge.release_provider_barrier(label)
    assert await asyncio.to_thread(detached["done"].wait, 20)
    assert detached["status"] == 200
    operations = await bridge.operations(claim["turn"]["id"])
    assert len(operations) == 1 and operations[0]["status"] == "SUCCEEDED"
    counts, objects = await bridge.provider_counts()
    assert counts.get("POST", 0) == objects == 1
