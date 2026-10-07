"""Real paired Goal3 HTTP/PG/process acceptance; explicit-file collection only."""

import asyncio
import copy
import json
from uuid import uuid4
from datetime import datetime

import pytest
from sqlalchemy import text

from tests.goal3_business_cross_service.conftest import observed_waiters
from tests.goal3_business_cross_service.fake_provider import completion, tool_call
from tests.goal3_business_cross_service.state import private_json

pytestmark = pytest.mark.asyncio(loop_scope="session")


def actions(*slots, confirmed=False):
    return [{"capability": "booking.confirm_create" if confirmed else "booking.create",
             "arguments": {"slot": slot}} for slot in slots or ("12:00",)]


def script(*slots):
    return [{"body": completion(None, [tool_call("business_execute_plan", {"actions": actions(*slots)})])},
            {"body": completion("Запись подтверждена")}]


def evidence_input(request):
    return {key: request[key] for key in ("contract_version", "owner_id", "integration_id", "channel",
                                         "agent_id", "conversation_id", "turn_id")}


def later_request(request, text_value="Запиши меня на 13:00", *, distinct=False, cross=None):
    value = copy.deepcopy(request)
    message = dict(value["messages"][-1], id=str(uuid4()), sequence=len(value["messages"]) + 1, text=text_value)
    value["messages"].append(message)
    value.update(turn_id=str(uuid4()), input_revision=value["input_revision"] + 1,
        input_first_sequence=message["sequence"], input_last_sequence=message["sequence"],
        context_last_sequence=message["sequence"], selected_input_message_ids=[message["id"]])
    if distinct:
        value.update(owner_id=cross.grants["b"]["owner_id"], integration_id=cross.ids["integration_b"],
                     agent_id=cross.ids["agent_b"], conversation_id=str(uuid4()))
    return value


def unknown_turn(response, *, refs=True):
    assert response.status_code == 200
    value = response.json()["turn"]
    assert value["status"] == "FAILED" and value["failure_code"] == "EXECUTION_UNKNOWN"
    assert value["draft_text"] is None
    if refs:
        assert value["operation_refs"]
    return value


def ai_unknown(response):
    assert response.status_code == 200
    value = response.json()
    assert value["status"] == "UNKNOWN" and value["draft_text"] is None
    assert value["error_code"] == "TOOL_EXECUTION_UNKNOWN"
    return value


async def one_operation(cross, turn_id):
    rows = await cross.operations(turn_id)
    assert len(rows) == 1
    return rows[0]


async def start_request(cross, *slots):
    await cross.script(model=script(*slots))
    accepted, claim = await cross.selected()
    running = asyncio.create_task(cross.execute(claim))
    request = await cross.projection(claim["turn"]["id"])
    return accepted, claim, request, running


async def test_actual_same_turn_receipt_draft_unpromoted_later_turn(cross):
    accepted, claim, request, running = await start_request(cross)
    response = await running
    assert response.status_code == 200
    turn = response.json()["turn"]
    assert turn["id"] == claim["turn"]["id"] and turn["status"] == "COMPLETED"
    op = await one_operation(cross, turn["id"])
    assert op["status"] == "SUCCEEDED" and op["receipt_id"] and op["object_id"]
    assert turn["operation_refs"] == [{"origin": "AI_AGENT_SERVICE", "operation_id": str(op["id"]),
                                       "outcome": "SUCCEEDED"}]
    assert (await cross.counters())["counts"]["POST"] == (await cross.counters())["objects"] == 1
    await cross.separated(turn["conversation_id"])
    await cross.script(model=script())
    _, claim2 = await cross.selected(thread=accepted["conversation"]["provider_thread_id"])
    second = await cross.execute(claim2)
    assert second.status_code == 200 and second.json()["turn"]["id"] != turn["id"]
    assert (await cross.operations(turn["id"]))[0]["receipt_id"] == op["receipt_id"]
    assert (await cross.cs("GET", "/turns/" + turn["id"])).json()["operation_refs"] == turn["operation_refs"]
    assert (await cross.counters())["objects"] == 2
    await cross.separated(turn["conversation_id"], 2)


@pytest.mark.parametrize("cross", ["LOOKUP", "NATIVE", "BLIND"], indirect=True)
async def test_lost_response_restart_provider_receipt_modes(cross):
    await cross.script(settings={"execute": {"default": {"drop": True}}})
    _, claim, request, running = await start_request(cross)
    turn = unknown_turn(await running)
    op = await one_operation(cross, turn["id"])
    assert op["status"] == "UNKNOWN"
    assert (await cross.counters())["counts"]["POST"] == (await cross.counters())["objects"] == 1
    await cross.restart("ai", abrupt=True)
    await cross.restart("provider", abrupt=True)
    await cross.script(model=script(), replace_model=True, settings={"execute": {"default": {}}})
    assert ai_unknown(await cross.ai("/managed-turns/execute", request))["operation_refs"][0]["operation_id"] == str(op["id"])
    assert (await cross.counters())["counts"]["POST"] == 1
    mode = json.loads((cross.root / "ai.json").read_text())["profile"]["mode"]
    await cross.worker(request, "native" if mode == "NATIVE" else "reconcile", operation_id=str(op["id"]))
    after = await cross.counters()
    persisted = await one_operation(cross, turn["id"])
    assert persisted["status"] == ("UNKNOWN" if mode == "BLIND" else "SUCCEEDED")
    if mode == "BLIND":
        assert after["counts"].get("LOOKUP", 0) == 0
        await cross.restart("ai", abrupt=True)
    await cross.worker(request, "reconcile", operation_id=str(op["id"]))
    assert after["objects"] == 1 and after["counts"]["POST"] == (2 if mode == "NATIVE" else 1)
    if mode == "NATIVE":
        assert {v["body"]["provider_idempotency_key"] for v in after["posts"]} == {op["provider_idempotency_key"]}
        assert len({v["body"]["issued_at"] for v in after["posts"]}) == 1
    await cross.separated(turn["conversation_id"])


@pytest.mark.parametrize("phase", ["A", "B", "B2", "C", "D", "E"])
async def test_process_crash_boundaries(cross, phase):
    if phase == "C":
        await cross.script(settings={"execute": {"default": {"barrier": "provider-C"}}})
    else:
        cross.fault(phase)
    _, claim, request, running = await start_request(cross)
    await (cross.provider_barrier("provider-C") if phase == "C" else cross.phase(phase))
    rows = await cross.operations(claim["turn"]["id"])
    assert len(rows) == (0 if phase == "A" else 1)
    if rows:
        assert rows[0]["status"] == ("RESERVED" if phase == "B" else "SUCCEEDED" if phase == "E" else "DISPATCHING")
    assert (await cross.counters())["counts"].get("POST", 0) == (0 if phase in {"A", "B", "B2"} else 1)
    await cross.stop("ai", abrupt=True)
    await asyncio.gather(running, return_exceptions=True)
    cross.fault()
    await cross.script(settings={"execute": {"default": {}}}, release=["provider-C"], model=script(), replace_model=True)
    await cross.start("ai")
    if phase in {"A", "B"}:
        assert (await cross.worker(request, "execute", actions=actions()))["operation_refs"][0]["outcome"] == "SUCCEEDED"
    elif phase in {"B2", "C", "D"}:
        await cross.expire_attempt(rows[0]["id"])
        await cross.worker(request, "recover")
        assert (await one_operation(cross, claim["turn"]["id"]))["status"] == "UNKNOWN"
        await cross.worker(request, "reconcile", operation_id=str(rows[0]["id"]))
        assert (await one_operation(cross, claim["turn"]["id"]))["status"] == ("UNKNOWN" if phase == "B2" else "SUCCEEDED")
    else:
        await cross.worker(request, "execute", actions=actions())
        assert (await one_operation(cross, claim["turn"]["id"]))["status"] == "SUCCEEDED"
    after = await cross.counters()
    assert after["counts"].get("POST", 0) == after["objects"] == (0 if phase == "B2" else 1)
    await cross.separated(claim["turn"]["conversation_id"])


async def test_F_actual_pg_wait_kill_cs_reclaim_evidence_attach(cross):
    cross.fault("E")
    _, claim, request, running = await start_request(cross)
    await cross.phase("E")
    async with cross.sql.begin() as blocker:
        pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
        await blocker.execute(text("SELECT id FROM conversations WHERE id=:id FOR UPDATE"),
                              {"id": claim["turn"]["conversation_id"]})
        cross.fault(); (cross.root / "release-E").touch()
        cross.lock_evidence.append(await observed_waiters(cross.sql, pid, "F CS result finalizer"))
        assert cross.trace("result")[-1]["operation_refs"][0]["outcome"] == "SUCCEEDED"
        await cross.stop("cs", abrupt=True)
    await asyncio.gather(running, return_exceptions=True)
    await cross.start("cs")
    retained = (await cross.cs("GET", "/turns/" + claim["turn"]["id"])).json()
    assert retained["status"] == "CLAIMED" and retained["managed_dispatch_epoch"] == 1
    await cross.expire_turn(retained["id"])
    recovered = await cross.claim(retained)
    assert recovered["turn"]["id"] == retained["id"] and recovered["turn"]["claim_epoch"] == 2
    before, ai_calls = await cross.counters(), len(cross.trace("execute"))
    evidence = await cross.ai("/managed-turns/evidence", evidence_input(request))
    assert evidence.status_code == 200
    refs = evidence.json()["operation_refs"]
    terminal = dict(cross.proof(recovered), failure_code="EXECUTION_UNKNOWN", operation_refs=refs)
    stale = await cross.cs("POST", "/turns/" + retained["id"] + "/fail", dict(cross.proof(claim),
        failure_code="EXECUTION_UNKNOWN", operation_refs=refs))
    assert stale.status_code == 409
    turn = unknown_turn(await cross.cs("POST", "/turns/" + retained["id"] + "/fail", terminal))
    assert turn["operation_refs"] == refs and turn["managed_dispatch_epoch"] == 1
    replay = await cross.cs("POST", "/turns/" + retained["id"] + "/fail", terminal)
    assert replay.status_code == 200 and replay.json()["replayed"]
    assert await cross.counters() == before and len(cross.trace("execute")) == ai_calls == 1
    await cross.separated(retained["conversation_id"])


async def test_effect_survives_later_model_failure(cross):
    await cross.script(model=[script()[0], *[{"status": 500, "body": {"error": {
        "message": "SYNTHETIC_PROVIDER_SECRET", "type": "fixture"}}} for _ in range(3)]])
    accepted, claim = await cross.selected()
    turn = unknown_turn(await cross.execute(claim))
    op = await one_operation(cross, turn["id"])
    assert op["status"] == "SUCCEEDED" and turn["operation_refs"][0]["outcome"] == "SUCCEEDED"
    request = await cross.projection(turn["id"])
    await cross.restart("ai", abrupt=True)
    await cross.worker(request, "execute", actions=actions())
    assert (await cross.counters())["counts"]["POST"] == 1
    await cross.separated(accepted["conversation"]["id"])


async def bare_request(cross, *, thread=None, text_value="Запиши меня на 12:00"):
    await cross.script(model=[{"body": completion("Тестовый ответ без эффекта")}])
    _, claim = await cross.selected(text_value, thread=thread)
    response = await cross.execute(claim)
    assert response.status_code == 200 and response.json()["turn"]["operation_refs"] == []
    return await cross.projection(claim["turn"]["id"])


async def reserved_pair(cross, *, consent=False):
    first = await bare_request(cross)
    async with cross.sql.connect() as db:
        thread = await db.scalar(text("SELECT provider_thread_id FROM conversations WHERE id=:id"),
                                 {"id": first["conversation_id"]})
    second = await bare_request(cross, thread=thread, text_value="Запиши меня на 13:00")
    assert second["conversation_id"] == first["conversation_id"] and second["turn_id"] != first["turn_id"]
    plans = [await cross.worker(r, "reserve", actions=actions(slot, confirmed=consent))
             for r, slot in ((first, "12:00"), (second, "13:00"))]
    if consent:
        confirming = await bare_request(cross, thread=thread, text_value="confirm " + plans[0]["plan_id"])
        await cross.worker(confirming, "confirm_only", plan_id=plans[0]["plan_id"])
        confirming2 = await bare_request(cross, thread=thread, text_value="confirm " + plans[1]["plan_id"])
        await cross.worker(confirming2, "confirm_only", plan_id=plans[1]["plan_id"])
    rows = [await one_operation(cross, r["turn_id"]) for r in (first, second)]
    assert all(row["status"] == "RESERVED" and row["attempt_count"] == 0 for row in rows)
    return first, second, rows


async def frozen_snapshot(cross, operation):
    async with cross.ai_sql.connect() as db:
        plan = (await db.execute(text("SELECT * FROM business_plans WHERE id=:id"),
                                 {"id": operation["plan_id"]})).mappings().one()
        links = (await db.execute(text("SELECT * FROM business_turn_plan_links WHERE plan_id=:id "
                                       "ORDER BY turn_id"), {"id": operation["plan_id"]})).mappings().all()
    row = next(row for row in await cross.operations(str(operation["origin_turn_id"])) if row["id"] == operation["id"])
    return json.dumps({"operation": dict(row), "plan": dict(plan), "links": [dict(v) for v in links]},
                      sort_keys=True, default=str)


async def blocked_with_pg_wait(cross, request, scope_id, label):
    async with cross.ai_sql.begin() as blocker:
        pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
        await blocker.execute(text("SELECT id FROM business_scopes WHERE id=:id FOR UPDATE"), {"id": scope_id})
        running = asyncio.create_task(cross.worker(request, "claim"))
        cross.lock_evidence.append(await observed_waiters(cross.ai_sql, pid, label))
    value = await running
    assert not value["permitted"] and value["reason"] == "BLOCKED_UNRESOLVED_EFFECT"
    assert value["operation_refs"][0]["outcome"] is None
    return value


@pytest.mark.parametrize("cross", [{"mode": "LOOKUP", "timeout": 3}], indirect=True)
async def test_AR1_pre_reserved_live_expired_unknown_preserve_consent(cross):
    first, second, rows = await reserved_pair(cross, consent=True)
    frozen = await frozen_snapshot(cross, rows[1])
    await cross.script(settings={"execute": {"default": {"barrier": "AR1-live"}}})
    running = asyncio.create_task(cross.worker(first, "claim", send=True))
    await cross.provider_barrier("AR1-live")
    assert (await one_operation(cross, first["turn_id"]))["status"] == "DISPATCHING"
    await blocked_with_pg_wait(cross, second, rows[1]["scope_id"], "AR1 live committed permission")
    assert await frozen_snapshot(cross, rows[1]) == frozen
    # Genuine provider request remains gated while no DB lock spans its lifetime.
    async with cross.ai_sql.begin() as observer:
        await observer.execute(text("SELECT id FROM agent_templates WHERE id=:id FOR UPDATE NOWAIT"), {"id": first["agent_id"]})
        await observer.execute(text("SELECT id FROM business_bindings WHERE id=(SELECT binding_id FROM "
            "business_plans WHERE id=:id) FOR UPDATE NOWAIT"), {"id": rows[0]["plan_id"]})
        for table, key in (("business_scopes", "scope_id"), ("business_plans", "plan_id"), ("business_operations", "id")):
            await observer.execute(text("SELECT id FROM " + table + " WHERE id=:id FOR UPDATE NOWAIT"), {"id": rows[0][key]})
    independent = later_request(first, distinct=True, cross=cross)
    await cross.script(settings={"execute": {"12:00": {"barrier": "AR1-live"}, "other": {}}})
    assert (await cross.worker(independent, "execute", actions=actions("other")))["operation_refs"][0]["outcome"] == "SUCCEEDED"
    await cross.stop("ai", abrupt=True)
    await asyncio.gather(running, return_exceptions=True)
    await cross.start("ai")
    # Actual DB-clock lease expiry, preserving stored DISPATCHING before any open.
    deadline = asyncio.get_running_loop().time() + 12
    while True:
        async with cross.ai_sql.connect() as db:
            expired = await db.scalar(text("SELECT clock_timestamp()>=dispatch_lease_expires_at "
                                          "FROM business_operations WHERE id=:id"), {"id": rows[0]["id"]})
        if expired:
            break
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(.03)
    assert (await one_operation(cross, first["turn_id"]))["status"] == "DISPATCHING"
    await blocked_with_pg_wait(cross, second, rows[1]["scope_id"], "AR1 expired unnormalized")
    cross.fault("LOCK-recover_attempts")
    normalization = asyncio.create_task(cross.worker(first, "recover"))
    await cross.phase("LOCK-recover_attempts")
    pid = [v for v in cross.trace("scope_lock") if v["method"] == "recover_attempts"][-1]["pid"]
    contender = asyncio.create_task(cross.worker(second, "claim"))
    cross.lock_evidence.append(await observed_waiters(cross.ai_sql, pid, "AR1 normalization COMMIT overlap"))
    cross.fault(); (cross.root / "release-LOCK-recover_attempts").touch()
    await normalization
    assert not (await contender)["permitted"]
    assert (await one_operation(cross, first["turn_id"]))["status"] == "UNKNOWN"
    assert await frozen_snapshot(cross, rows[1]) == frozen
    await cross.script(model=[{"body": completion(None, [tool_call("business_execute_plan", {
        "actions": actions("13:00", confirmed=True)})])}, {"body": completion("Готово, запись создана")}], replace_model=True)
    before = await cross.counters()
    value = ai_unknown(await cross.ai("/managed-turns/execute", second))
    assert value["operation_refs"] == [{"origin": "AI_AGENT_SERVICE", "operation_id": str(rows[1]["id"]), "outcome": None}]
    after = await cross.counters()
    # The retained entry guard now suppresses model continuation for known U.
    assert after["counts"]["MODEL"] == before["counts"]["MODEL"]
    assert after["counts"]["POST"] == before["counts"]["POST"] == 2
    assert not any(p["operation_id"] == str(rows[1]["id"]) for p in after["posts"])
    assert await frozen_snapshot(cross, rows[1]) == frozen
    await cross.script(release=["AR1-live"])


@pytest.mark.parametrize("negative", [False, True])
async def test_AR1_terminal_commit_releases_original_reservation(cross, negative):
    first, second, rows = await reserved_pair(cross, consent=True)
    original = await frozen_snapshot(cross, rows[1])
    await cross.script(settings={"execute": {"default": {"drop": True, "negative": negative}}})
    await cross.worker(first, "claim", send=True)
    assert (await one_operation(cross, first["turn_id"]))["status"] == "UNKNOWN"
    await cross.script(settings={"execute": {"default": {}}})
    cross.fault("LOCK-finalize_reconcile")
    resolving = asyncio.create_task(cross.worker(first, "reconcile", operation_id=str(rows[0]["id"])))
    await cross.phase("LOCK-finalize_reconcile")
    pid = [v for v in cross.trace("scope_lock") if v["method"] == "finalize_reconcile"][-1]["pid"]
    # Attempted slot remains occupied until the original terminal COMMIT.
    contender = asyncio.create_task(cross.worker(second, "claim", send=True))
    cross.lock_evidence.append(await observed_waiters(cross.ai_sql, pid, "AR1 terminal release COMMIT"))
    assert (await one_operation(cross, first["turn_id"]))["status"] == "UNKNOWN"
    assert await frozen_snapshot(cross, rows[1]) == original
    cross.fault(); (cross.root / "release-LOCK-finalize_reconcile").touch()
    await resolving
    assert (await contender)["permitted"]
    assert (await one_operation(cross, first["turn_id"]))["status"] == ("FAILED" if negative else "SUCCEEDED")
    assert (await one_operation(cross, second["turn_id"]))["id"] == rows[1]["id"]
    counters = await cross.counters()
    assert counters["counts"]["POST"] == 2 and counters["counts"]["LOOKUP"] == 1
    assert counters["objects"] == (1 if negative else 2)
    third = later_request(second)
    assert (await cross.worker(third, "execute", actions=actions("later")))["operation_refs"][0]["outcome"] == "SUCCEEDED"


async def test_AR1_partial_unique_backstop_and_missing_lookup(cross):
    from sqlalchemy.exc import IntegrityError
    first, second, rows = await reserved_pair(cross)
    await cross.script(settings={"execute": {"default": {"drop": True}}, "lookup": "missing"})
    await cross.worker(first, "claim", send=True)
    frozen = await frozen_snapshot(cross, rows[1])
    with pytest.raises(IntegrityError) as rejected:
        async with cross.ai_sql.begin() as db:
            await db.execute(text("UPDATE business_operations SET status='DISPATCHING',state_version=1,"
                "dispatch_epoch=1,attempt_count=1,dispatch_nonce=:nonce,dispatch_started_at=clock_timestamp(),"
                "dispatch_lease_expires_at=clock_timestamp()+interval '8 seconds' WHERE id=:id"),
                {"id": rows[1]["id"], "nonce": uuid4()})
    assert "uq_business_operations_unresolved_scope" in str(rejected.value.orig)
    await cross.worker(first, "reconcile", operation_id=str(rows[0]["id"]))
    await blocked_with_pg_wait(cross, second, rows[1]["scope_id"], "AR1 missing lookup retains slot")
    assert await frozen_snapshot(cross, rows[1]) == frozen
    assert (await cross.counters())["counts"]["POST"] == (await cross.counters())["objects"] == 1


@pytest.mark.parametrize("winner", [0, 1])
async def test_AR1_reverse_first_permission_wins_bounded_loser(cross, winner):
    first, second, rows = await reserved_pair(cross)
    requests = (first, second)
    winner_req, loser_req = requests[winner], requests[1 - winner]
    cross.fault("LOCK-claim_dispatch")
    winning = asyncio.create_task(cross.worker(winner_req, "claim", send=True))
    await cross.phase("LOCK-claim_dispatch")
    pid = [v for v in cross.trace("scope_lock") if v["method"] == "claim_dispatch"][-1]["pid"]
    losing = asyncio.create_task(cross.worker(loser_req, "claim"))
    cross.lock_evidence.append(await observed_waiters(cross.ai_sql, pid, "AR1 reverse winner " + str(winner)))
    # Hold provider after D so the contender observes attempted state, not a
    # already terminal effect. The new fake script is committed before release.
    await cross.script(settings={"execute": {"default": {"drop": True}}})
    cross.fault(); (cross.root / "release-LOCK-claim_dispatch").touch()
    assert not (await losing)["permitted"]
    assert (await winning)["permitted"]
    assert (await one_operation(cross, loser_req["turn_id"]))["status"] == "RESERVED"
    assert (await cross.counters())["counts"]["POST"] == 1


async def test_AR1_R_D_transition_and_fresh_plan_denied(cross):
    first = await bare_request(cross)
    await cross.worker(first, "reserve", actions=actions())
    second = later_request(first)
    cross.fault("LOCK-claim_dispatch")
    permission = asyncio.create_task(cross.worker(first, "claim", send=True))
    await cross.phase("LOCK-claim_dispatch")
    pid = [v for v in cross.trace("scope_lock") if v["method"] == "claim_dispatch"][-1]["pid"]
    reserve = asyncio.create_task(cross.worker(second, "reserve", actions=actions("13:00")))
    cross.lock_evidence.append(await observed_waiters(cross.ai_sql, pid, "AR1 D wins before fresh R"))
    await cross.script(settings={"execute": {"default": {"drop": True}}})
    cross.fault(); (cross.root / "release-LOCK-claim_dispatch").touch()
    assert (await reserve)["safety_code"] == "BLOCKED_UNRESOLVED_EFFECT"
    await permission
    assert await cross.operations(second["turn_id"]) == []
    assert (await cross.counters())["counts"]["POST"] == 1


async def test_AR1_ordered_mixed_effects_and_residual_admission(cross):
    first = await bare_request(cross)
    await cross.worker(first, "reserve", actions=actions("12:00", "13:00", "14:00"))
    assert (await cross.worker(first, "claim", ordinal=1))["reason"] == "BLOCKED_PREDECESSOR"
    assert (await cross.worker(first, "claim", ordinal=0, send=True))["permitted"]
    await cross.script(settings={"execute": {"13:00": {"drop": True}}})
    assert (await cross.worker(first, "claim", ordinal=1, send=True))["permitted"]
    assert (await cross.worker(first, "claim", ordinal=2))["reason"] == "BLOCKED_PREDECESSOR"
    rows = await cross.operations(first["turn_id"])
    assert [r["status"] for r in rows] == ["SUCCEEDED", "UNKNOWN", "RESERVED"]
    evidence = await cross.ai("/managed-turns/evidence", evidence_input(first))
    assert {r["outcome"] for r in evidence.json()["operation_refs"]} == {"SUCCEEDED", "UNKNOWN", None}
    await cross.worker(first, "reconcile", operation_id=str(rows[1]["id"]))
    await cross.script(settings={"execute": {"default": {}}})
    assert (await cross.worker(first, "claim", ordinal=2, send=True))["permitted"]
    assert [r["status"] for r in await cross.operations(first["turn_id"])] == ["SUCCEEDED"] * 3
    assert (await cross.counters())["counts"]["POST"] == (await cross.counters())["objects"] == 3


@pytest.mark.parametrize("cross", ["NATIVE"], indirect=True)
async def test_AR1_native_original_only_and_competing_reserved(cross):
    first, second, rows = await reserved_pair(cross)
    await cross.script(settings={"execute": {"default": {"drop": True}}})
    await cross.worker(first, "claim", send=True)
    frozen = await frozen_snapshot(cross, rows[1])
    # Another RESERVED row cannot use the native original-UNKNOWN exception.
    denied = await cross.worker(second, "claim", native=True)
    assert not denied["permitted"]
    cross.fault("B2")
    replay = asyncio.create_task(cross.worker(first, "native", operation_id=str(rows[0]["id"])))
    await cross.phase("B2")
    assert (await cross.worker(first, "claim", native=True))["reason"] == "IN_FLIGHT"
    await blocked_with_pg_wait(cross, second, rows[1]["scope_id"], "AR1 native same original slot")
    assert await frozen_snapshot(cross, rows[1]) == frozen
    await cross.script(settings={"execute": {"default": {}}})
    cross.fault(); (cross.root / "release-B2").touch()
    await replay
    counters = await cross.counters()
    assert counters["counts"]["POST"] == 2 and counters["objects"] == 1
    assert len({p["body"]["provider_idempotency_key"] for p in counters["posts"]}) == 1
    assert (await cross.worker(second, "claim", send=True))["permitted"]


@pytest.mark.parametrize("cross", [{"mode": "NATIVE", "native_policy": "BOUNDED", "horizon": 1}], indirect=True)
async def test_native_horizon_insufficient_and_expired_denies_replay(cross):
    await cross.script(settings={"execute": {"default": {"drop": True}}})
    _, claim, request, running = await start_request(cross)
    unknown_turn(await running)
    op = await one_operation(cross, request["turn_id"])
    await cross.worker(request, "native", operation_id=str(op["id"]))
    assert (await cross.counters())["counts"]["POST"] == 1
    await asyncio.sleep(1.05)
    await cross.worker(request, "native", operation_id=str(op["id"]))
    assert (await one_operation(cross, request["turn_id"]))["status"] == "UNKNOWN"
    assert (await cross.counters())["counts"]["POST"] == (await cross.counters())["objects"] == 1


@pytest.mark.parametrize("cross", ["BLIND"], indirect=True)
async def test_AR1_blind_restarts_keep_all_candidates_blocked(cross):
    first, second, rows = await reserved_pair(cross)
    await cross.script(settings={"execute": {"default": {"drop": True}}})
    await cross.worker(first, "claim", send=True)
    original = await frozen_snapshot(cross, rows[1])
    for _ in range(2):
        await cross.restart("ai", abrupt=True)
        assert not (await cross.worker(second, "claim"))["permitted"]
        await cross.worker(first, "reconcile", operation_id=str(rows[0]["id"]))
        assert (await one_operation(cross, first["turn_id"]))["status"] == "UNKNOWN"
    assert await frozen_snapshot(cross, rows[1]) == original
    assert (await cross.counters())["counts"]["POST"] == (await cross.counters())["objects"] == 1


async def test_AR1_R_wins_commit_then_both_plans_freshly_check_scope(cross):
    first = await bare_request(cross)
    await cross.worker(first, "reserve", actions=actions())
    second = later_request(first)
    cross.fault("LOCK-reserve_plan")
    reserve = asyncio.create_task(cross.worker(second, "reserve", actions=actions("13:00")))
    await cross.phase("LOCK-reserve_plan")
    pid = [v for v in cross.trace("scope_lock") if v["method"] == "reserve_plan"][-1]["pid"]
    dispatch = asyncio.create_task(cross.worker(first, "claim", send=True))
    cross.lock_evidence.append(await observed_waiters(cross.ai_sql, pid, "AR1 fresh R wins before D"))
    await cross.script(settings={"execute": {"default": {"drop": True}}})
    cross.fault(); (cross.root / "release-LOCK-reserve_plan").touch()
    assert (await reserve)["plan_id"]
    assert (await dispatch)["permitted"]
    reserved = await one_operation(cross, second["turn_id"])
    frozen = await frozen_snapshot(cross, reserved)
    await blocked_with_pg_wait(cross, second, reserved["scope_id"], "AR1 committed R still checks D")
    assert await frozen_snapshot(cross, reserved) == frozen
    assert (await cross.counters())["counts"]["POST"] == 1


async def test_AR1_expiry_normalization_blocks_fresh_reservation_after_commit(cross):
    first = await bare_request(cross)
    await cross.worker(first, "reserve", actions=actions())
    await cross.worker(first, "claim")  # Positive D COMMIT, deliberately no provider call.
    row = await one_operation(cross, first["turn_id"])
    await cross.expire_attempt(row["id"])
    second = later_request(first)
    cross.fault("LOCK-recover_attempts")
    recovery = asyncio.create_task(cross.worker(first, "recover"))
    await cross.phase("LOCK-recover_attempts")
    pid = [v for v in cross.trace("scope_lock") if v["method"] == "recover_attempts"][-1]["pid"]
    fresh = asyncio.create_task(cross.worker(second, "reserve", actions=actions("13:00")))
    cross.lock_evidence.append(await observed_waiters(cross.ai_sql, pid, "AR1 fresh R waits normalization"))
    assert (await one_operation(cross, first["turn_id"]))["status"] == "DISPATCHING"
    cross.fault(); (cross.root / "release-LOCK-recover_attempts").touch()
    await recovery
    assert (await fresh)["safety_code"] == "BLOCKED_UNRESOLVED_EFFECT"
    assert await cross.operations(second["turn_id"]) == []
    assert (await one_operation(cross, first["turn_id"]))["status"] == "UNKNOWN"
    assert (await cross.counters())["counts"].get("POST", 0) == 0


@pytest.mark.parametrize("cross", [{"mode": "LOOKUP", "timeout": 3}], indirect=True)
async def test_AR1_live_predecessor_and_another_turn_between_actions(cross):
    first = await bare_request(cross)
    await cross.worker(first, "reserve", actions=actions("12:00", "13:00"))
    second = later_request(first)
    await cross.worker(second, "reserve", actions=actions("other"))
    await cross.script(settings={"execute": {"12:00": {"barrier": "ordered-live"}}})
    active = asyncio.create_task(cross.worker(first, "claim", ordinal=0, send=True))
    await cross.provider_barrier("ordered-live")
    async with cross.ai_sql.begin() as blocker:
        pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
        rows = await cross.operations(first["turn_id"])
        await blocker.execute(text("SELECT id FROM business_scopes WHERE id=:id FOR UPDATE"), {"id": rows[0]["scope_id"]})
        successor = asyncio.create_task(cross.worker(first, "claim", ordinal=1))
        cross.lock_evidence.append(await observed_waiters(cross.ai_sql, pid, "AR1 live predecessor"))
    assert (await successor)["reason"] == "BLOCKED_PREDECESSOR"
    await cross.script(release=["ordered-live"])
    assert (await active)["permitted"]
    frozen = await frozen_snapshot(cross, rows[1])
    await cross.script(settings={"execute": {"default": {"drop": True}}})
    cross.fault("LOCK-claim_dispatch")
    intervening = asyncio.create_task(cross.worker(second, "claim", send=True))
    await cross.phase("LOCK-claim_dispatch")
    pid = [v for v in cross.trace("scope_lock") if v["method"] == "claim_dispatch"][-1]["pid"]
    residual = asyncio.create_task(cross.worker(first, "claim", ordinal=1, send=True))
    cross.lock_evidence.append(await observed_waiters(cross.ai_sql, pid, "AR1 residual rechecks intervening Turn"))
    cross.fault(); (cross.root / "release-LOCK-claim_dispatch").touch()
    assert (await intervening)["permitted"]
    assert (await residual)["reason"] == "BLOCKED_UNRESOLVED_EFFECT"
    # Compare this multioperation reservation without using one_operation().
    after = (await cross.operations(first["turn_id"]))[1]
    assert after["id"] == rows[1]["id"] and after["status"] == "RESERVED" and after["attempt_count"] == 0
    assert after["request_fingerprint"] == rows[1]["request_fingerprint"]
    assert await frozen_snapshot(cross, rows[1]) == frozen
    await cross.worker(second, "reconcile", operation_id=str((await one_operation(cross, second["turn_id"]))["id"]))
    await cross.script(settings={"execute": {"default": {}}})
    assert (await cross.worker(first, "claim", ordinal=1, send=True))["permitted"]
    assert (await cross.counters())["counts"]["POST"] == (await cross.counters())["objects"] == 3


async def test_frozen_identity_replay_conflicts_bounds_and_readonly_auth(cross):
    first = await bare_request(cross)
    absent_plan = await cross.ai("/managed-turns/evidence", evidence_input(first))
    assert absent_plan.status_code == 200 and absent_plan.json()["operation_refs"] == []
    slots = tuple("slot-" + str(n) for n in range(32))
    plan = await cross.worker(first, "reserve", actions=actions(*slots))
    rows = await cross.operations(first["turn_id"])
    assert len(rows) == 32 and len({r["id"] for r in rows}) == 32
    assert (await cross.worker(first, "reserve", actions=actions(*slots)))["plan_id"] == plan["plan_id"]
    for changed in (slots[::-1], slots[:-1], ("material-change", *slots[1:])):
        assert (await cross.worker(first, "reserve", actions=actions(*changed)))["safety_code"] == "PLAN_CONFLICT"
    assert (await cross.worker(later_request(first), "reserve", actions=actions(*slots, "33")))["safety_code"] == "PLAN_BOUNDS"
    before = await cross.counters()
    result = await cross.ai("/managed-turns/evidence", evidence_input(first))
    assert result.status_code == 200 and set(result.headers["cache-control"].split(", ")) == {"no-store"}
    assert len(result.json()["operation_refs"]) == 32
    assert all(r["outcome"] is None for r in result.json()["operation_refs"])
    for key, replacement in (("owner_id", "opaque-goal3-b"), ("integration_id", cross.ids["integration_b"]),
                             ("agent_id", cross.ids["agent_b"]), ("channel", "INSTAGRAM")):
        assert (await cross.ai("/managed-turns/evidence", dict(evidence_input(first), **{key: replacement}))).status_code == 404
    assert (await cross.ai("/managed-turns/evidence", evidence_input(first), token="chat")).status_code == 401
    assert (await cross.ai("/managed-turns/evidence", dict(evidence_input(first), extra="forbidden"))).status_code == 422
    for key in ("conversation_id", "turn_id"):
        absent = await cross.ai("/managed-turns/evidence", dict(evidence_input(first), **{key: str(uuid4())}))
        assert absent.status_code == 404
    assert await cross.counters() == before
    assert len(await cross.operations(first["turn_id"])) == 32


async def test_proposal_exact_later_customer_confirmation_and_origin(cross):
    await cross.script(model=[{"body": completion(None, [tool_call("business_propose", {
        "actions": actions(confirmed=True)})])}, {"body": completion("Небезопасное утверждение о записи")}])
    accepted, claim = await cross.selected()
    response = await cross.execute(claim)
    assert response.status_code == 200
    first_turn = response.json()["turn"]
    row = await one_operation(cross, first_turn["id"])
    assert row["status"] == "RESERVED" and row["attempt_count"] == 0
    assert "confirm " + str(row["plan_id"]) in first_turn["draft_text"]
    assert (await cross.counters())["counts"].get("POST", 0) == 0
    request = await cross.projection(first_turn["id"])
    for wording in ("yes", "подтверждаю", "Confirm " + str(row["plan_id"])):
        denied = await cross.worker(later_request(request, wording), "confirm", plan_id=str(row["plan_id"]))
        assert denied["halt_status"] == "FAILED" and denied["error_code"] == "TOOL_SETUP_FAILED"
    await cross.script(model=[{"body": completion(None, [tool_call("business_confirm", {
        "proposal_id": str(row["plan_id"])})])}, {"body": completion("Запись подтверждена")}], replace_model=True)
    _, later_claim = await cross.selected("confirm " + str(row["plan_id"]),
                                         thread=accepted["conversation"]["provider_thread_id"])
    later = await cross.execute(later_claim)
    assert later.status_code == 200 and later.json()["turn"]["status"] == "COMPLETED"
    assert later.json()["turn"]["operation_refs"][0]["operation_id"] == str(row["id"])
    assert await cross.operations(later_claim["turn"]["id"]) == []
    retained = await one_operation(cross, first_turn["id"])
    assert retained["status"] == "SUCCEEDED" and retained["origin_turn_id"] == row["origin_turn_id"]
    await cross.separated(first_turn["conversation_id"], 2)


async def test_already_terminal_empty_refs_remain_immutable_with_ai_evidence(cross):
    cross.fault("E")
    _, claim, request, running = await start_request(cross)
    await cross.phase("E")
    await cross.stop("cs", abrupt=True)
    await asyncio.gather(running, return_exceptions=True)
    cross.fault(); (cross.root / "release-E").touch()
    await cross.start("cs")
    retained = (await cross.cs("GET", "/turns/" + claim["turn"]["id"])).json()
    await cross.expire_turn(retained["id"])
    recovered = await cross.claim(retained)
    before = await cross.counters()
    terminal = unknown_turn(await cross.execute(recovered), refs=False)
    assert terminal["operation_refs"] == []
    evidence = (await cross.ai("/managed-turns/evidence", evidence_input(request))).json()["operation_refs"]
    assert len(evidence) == 1 and evidence[0]["outcome"] == "SUCCEEDED"
    enrichment = await cross.cs("POST", "/turns/" + retained["id"] + "/fail", dict(cross.proof(recovered),
        failure_code="EXECUTION_UNKNOWN", operation_refs=evidence))
    assert enrichment.status_code == 409
    assert (await cross.cs("GET", "/turns/" + retained["id"])).json()["operation_refs"] == []
    assert await cross.counters() == before
    await cross.separated(retained["conversation_id"])


async def test_real_provider_deadline_closes_socket_and_retains_unknown(cross):
    await cross.script(settings={"execute": {"default": {"barrier": "owned-deadline"}}})
    accepted, claim, request, running = await start_request(cross)
    await cross.provider_barrier("owned-deadline")
    turn = unknown_turn(await running)
    await cross.provider_peer_eof("owned-deadline")
    row = await one_operation(cross, turn["id"])
    assert row["status"] == "UNKNOWN" and row["attempt_count"] == 1
    assert (await cross.counters())["counts"]["POST"] == (await cross.counters())["objects"] == 1
    await cross.script(release=["owned-deadline"])
    await cross.worker(request, "reconcile", operation_id=str(row["id"]))
    assert (await one_operation(cross, turn["id"]))["status"] == "SUCCEEDED"
    await cross.separated(accepted["conversation"]["id"])


async def test_block_preserves_consent_then_current_authority_denies_original(cross):
    first, second, rows = await reserved_pair(cross, consent=True)
    await cross.script(settings={"execute": {"default": {"drop": True}}})
    await cross.worker(first, "claim", send=True)
    frozen = await frozen_snapshot(cross, rows[1])
    await cross.worker(second, "configure", enabled_capabilities=["booking.create"], authorization_generation=2)
    await blocked_with_pg_wait(cross, second, rows[1]["scope_id"], "AR1 blocked before stale authority change")
    assert await frozen_snapshot(cross, rows[1]) == frozen
    # Retained lookup remains read-only and can close the original slot after
    # mutation permission changes; no new permission is inferred from a receipt.
    await cross.worker(first, "reconcile", operation_id=str(rows[0]["id"]))
    denied = await cross.worker(second, "claim", send=True)
    assert not denied["permitted"] and denied["reason"] == "AUTHORITY_DENIED"
    assert denied["operation_refs"] == [{"origin": "AI_AGENT_SERVICE", "operation_id": str(rows[1]["id"]), "outcome": "FAILED"}]
    async with cross.ai_sql.connect() as db:
        plan = (await db.execute(text("SELECT proposal_state,confirmed_turn_id,confirmation_input_ids,confirmation_policy "
            "FROM business_plans WHERE id=:id"), {"id": rows[1]["plan_id"]})).mappings().one()
    original = json.loads(frozen)["plan"]
    assert plan["proposal_state"] == "INVALIDATED"
    assert str(plan["confirmed_turn_id"]) == original["confirmed_turn_id"]
    assert plan["confirmation_input_ids"] == original["confirmation_input_ids"]
    assert plan["confirmation_policy"] == original["confirmation_policy"]
    after = await one_operation(cross, second["turn_id"])
    assert after["id"] == rows[1]["id"] and after["status"] == "FAILED" and after["attempt_count"] == 0
    assert after["receipt_id"] == "no-dispatch:" + str(rows[1]["id"])
    assert (await cross.counters())["counts"]["POST"] == (await cross.counters())["objects"] == 1


async def test_actual_cs_mixed_three_refs_halt_and_no_success_draft(cross):
    await cross.script(settings={"execute": {"13:00": {"drop": True}}})
    accepted, claim, request, running = await start_request(cross, "12:00", "13:00", "14:00")
    turn = unknown_turn(await running)
    rows = await cross.operations(turn["id"])
    assert [row["status"] for row in rows] == ["SUCCEEDED", "UNKNOWN", "RESERVED"]
    refs = {ref["operation_id"]: ref["outcome"] for ref in turn["operation_refs"]}
    assert refs == {str(row["id"]): outcome for row, outcome in zip(rows, ("SUCCEEDED", "UNKNOWN", None), strict=True)}
    assert (await cross.counters())["counts"]["POST"] == (await cross.counters())["objects"] == 2
    assert (await cross.counters())["counts"]["MODEL"] == 1
    assert (await cross.ai("/managed-turns/evidence", evidence_input(request))).json()["operation_refs"] == turn["operation_refs"]
    await cross.separated(accepted["conversation"]["id"])


@pytest.mark.parametrize("failure", ["cleanup", "invalid_draft"])
async def test_receipt_survives_post_effect_draft_and_cleanup_failure(cross, failure):
    if failure == "cleanup":
        private_json(cross.root / "faults.json", {"cleanup_failure": True})
    await cross.script(model=[script()[0], {"body": completion("x" * 17000 if failure == "invalid_draft" else "Запись создана")}])
    accepted, claim = await cross.selected()
    turn = unknown_turn(await cross.execute(claim))
    row = await one_operation(cross, turn["id"])
    assert row["status"] == "SUCCEEDED" and row["receipt_id"] and row["object_id"]
    assert turn["operation_refs"] == [{"origin": "AI_AGENT_SERVICE", "operation_id": str(row["id"]), "outcome": "SUCCEEDED"}]
    assert cross.trace("result")[-1]["error_code"] == ("TOOL_CLEANUP_FAILED" if failure == "cleanup" else "INVALID_RESULT")
    request = await cross.projection(turn["id"])
    cross.fault()
    await cross.restart("ai", abrupt=True)
    assert (await cross.ai("/managed-turns/evidence", evidence_input(request))).json()["operation_refs"] == turn["operation_refs"]
    assert (await cross.counters())["counts"]["POST"] == (await cross.counters())["objects"] == 1
    await cross.separated(accepted["conversation"]["id"])


@pytest.mark.parametrize("existing", [False, True])
async def test_section46_same_operation_reservation_and_exact_replay_wait(cross, existing):
    request = await bare_request(cross)
    if existing:
        prior = await cross.worker(request, "reserve_direct", actions=actions())
    async with cross.ai_sql.begin() as blocker:
        pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
        await blocker.execute(text("SELECT id FROM agent_templates WHERE id=:id FOR UPDATE"), {"id": request["agent_id"]})
        workers = [asyncio.create_task(cross.worker(request, "reserve_direct", actions=actions())) for _ in range(2)]
        cross.lock_evidence.append(await observed_waiters(cross.ai_sql, pid,
            "46 exact replay" if existing else "46 first same-operation reservation", count=2))
    outcomes = [await worker for worker in workers]
    committed = [outcome["plan_id"] for outcome in outcomes if outcome.get("plan_id")]
    assert committed and len(set(committed)) == 1
    assert all(outcome.get("plan_id") or (outcome.get("safety_code") == "ADMISSION_CONTENDED"
               and outcome["halt_status"] == "UNKNOWN") for outcome in outcomes)
    # P may wait, but renewed decisive contention is deliberately bounded.
    # Only a later explicit invocation obtains fresh facts for exact replay.
    replay = await cross.worker(request, "reserve_direct", actions=actions())
    assert replay["plan_id"] == committed[0]
    if existing:
        assert committed[0] == prior["plan_id"]
    row = await one_operation(cross, request["turn_id"])
    assert row["status"] == "RESERVED" and row["attempt_count"] == 0
    assert (await cross.counters())["counts"].get("POST", 0) == 0


async def test_section46_same_operation_competing_permission_one_actual_post(cross):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions())
    await cross.script(settings={"execute": {"default": {"drop": True}}})
    async with cross.ai_sql.begin() as blocker:
        pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
        await blocker.execute(text("SELECT id FROM agent_templates WHERE id=:id FOR UPDATE"), {"id": request["agent_id"]})
        workers = [asyncio.create_task(cross.worker(request, "claim", send=True)) for _ in range(2)]
        cross.lock_evidence.append(await observed_waiters(cross.ai_sql, pid, "46 same-operation D permission", count=2))
    outcomes = [await worker for worker in workers]
    assert sum(result["permitted"] for result in outcomes) == 1
    assert next(result for result in outcomes if not result["permitted"])["reason"] in {"IN_FLIGHT", "UNKNOWN", "ADMISSION_CONTENDED"}
    assert not (await cross.worker(request, "claim", send=True))["permitted"]
    assert (await cross.counters())["counts"]["POST"] == (await cross.counters())["objects"] == 1
    assert (await one_operation(cross, request["turn_id"]))["attempt_count"] == 1


@pytest.mark.parametrize("cross", [{"mode": "LOOKUP", "timeout": 3}], indirect=True)
async def test_section46_original_receipt_finalization_and_exact_receipt_replay(cross):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions())
    row = await one_operation(cross, request["turn_id"])
    await cross.script(settings={"execute": {"default": {"barrier": "receipt-race"}}})
    original = asyncio.create_task(cross.worker(request, "claim", send=True))
    await cross.provider_barrier("receipt-race")
    async with cross.ai_sql.begin() as blocker:
        pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
        await blocker.execute(text("SELECT id FROM business_scopes WHERE id=:id FOR UPDATE"), {"id": row["scope_id"]})
        await cross.script(release=["receipt-race"])
        cross.lock_evidence.append(await observed_waiters(cross.ai_sql, pid, "46 original receipt F waiting"))
        replay = asyncio.create_task(cross.worker(request, "finalize_again", operation_id=str(row["id"])))
        cross.lock_evidence.append(await observed_waiters(cross.ai_sql, pid, "46 receipt F exact replay", count=2))
        assert (await one_operation(cross, request["turn_id"]))["status"] == "DISPATCHING"
    assert (await original)["permitted"]
    await replay
    receipt = await one_operation(cross, request["turn_id"])
    assert receipt["status"] == "SUCCEEDED" and receipt["attempt_count"] == 1
    frozen = await frozen_snapshot(cross, receipt)
    await cross.worker(request, "finalize_again", operation_id=str(row["id"]))
    assert await frozen_snapshot(cross, receipt) == frozen
    assert (await cross.counters())["counts"]["POST"] == (await cross.counters())["objects"] == 1
    assert (await cross.counters())["counts"]["LOOKUP"] == 2


async def test_section46_reconciliation_claims_overlap_one_read_no_second_mutation(cross):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions())
    await cross.script(settings={"execute": {"default": {"drop": True}}})
    await cross.worker(request, "claim", send=True)
    row = await one_operation(cross, request["turn_id"])
    cross.fault("LOCK-claim_reconcile")
    first = asyncio.create_task(cross.worker(request, "reconcile_direct", operation_id=str(row["id"])))
    await cross.phase("LOCK-claim_reconcile")
    pid = [v for v in cross.trace("scope_lock") if v["method"] == "claim_reconcile"][-1]["pid"]
    second = asyncio.create_task(cross.worker(request, "reconcile_direct", operation_id=str(row["id"])))
    cross.lock_evidence.append(await observed_waiters(cross.ai_sql, pid, "46 reconciliation claims"))
    cross.fault("RCP"); (cross.root / "release-LOCK-claim_reconcile").touch()
    await cross.phase("RCP")
    assert not (await second)["permitted"]
    cross.fault(); (cross.root / "release-RCP").touch()
    assert (await first)["permitted"]
    await cross.worker(request, "reconcile", operation_id=str(row["id"]))
    counters = await cross.counters()
    assert counters["counts"]["LOOKUP"] == counters["counts"]["POST"] == counters["objects"] == 1
    assert (await one_operation(cross, request["turn_id"]))["status"] == "SUCCEEDED"


async def test_section46_exact_confirmation_overlap_and_replay(cross):
    first = await bare_request(cross)
    plan = await cross.worker(first, "reserve", actions=actions(confirmed=True), propose=True)
    async with cross.sql.connect() as db:
        thread = await db.scalar(text("SELECT provider_thread_id FROM conversations WHERE id=:id"), {"id": first["conversation_id"]})
    confirming = await bare_request(cross, thread=thread, text_value="confirm " + plan["plan_id"])
    async with cross.ai_sql.begin() as blocker:
        pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
        await blocker.execute(text("SELECT id FROM agent_templates WHERE id=:id FOR UPDATE"), {"id": first["agent_id"]})
        workers = [asyncio.create_task(cross.worker(confirming, "confirm_direct", plan_id=plan["plan_id"])) for _ in range(2)]
        cross.lock_evidence.append(await observed_waiters(cross.ai_sql, pid, "46 exact selected confirmation", count=2))
    outcomes = [await worker for worker in workers]
    assert any(outcome.get("plan_id") == plan["plan_id"] for outcome in outcomes)
    assert all(outcome.get("plan_id") == plan["plan_id"] or (outcome.get("safety_code") == "ADMISSION_CONTENDED"
               and outcome["halt_status"] == "UNKNOWN") for outcome in outcomes)
    async with cross.ai_sql.connect() as db:
        consent = (await db.execute(text("SELECT confirmed_at,confirmed_turn_id,confirmation_input_ids,confirmation_policy FROM business_plans WHERE id=:id"),
                                   {"id": plan["plan_id"]})).one()
    assert (await cross.worker(confirming, "confirm_direct", plan_id=plan["plan_id"]))["plan_id"] == plan["plan_id"]
    async with cross.ai_sql.connect() as db:
        assert (await db.execute(text("SELECT confirmed_at,confirmed_turn_id,confirmation_input_ids,confirmation_policy FROM business_plans WHERE id=:id"),
                                {"id": plan["plan_id"]})).one() == consent
    for _ in range(2):
        assert (await cross.worker(confirming, "confirm", plan_id=plan["plan_id"]))["operation_refs"][0]["outcome"] == "SUCCEEDED"
    assert (await cross.counters())["counts"]["POST"] == (await cross.counters())["objects"] == 1
    assert str((await one_operation(cross, first["turn_id"]))["origin_turn_id"]) == first["turn_id"]


async def test_section46_generation_commit_wins_fresh_permission_check(cross):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions())
    async with cross.ai_sql.begin() as changer:
        pid = await changer.scalar(text("SELECT pg_backend_pid()"))
        await changer.execute(text("SELECT id FROM agent_templates WHERE id=:id FOR UPDATE"), {"id": request["agent_id"]})
        await changer.execute(text("UPDATE business_bindings SET authorization_generation=2 WHERE agent_id=:id"), {"id": request["agent_id"]})
        permission = asyncio.create_task(cross.worker(request, "claim", send=True))
        cross.lock_evidence.append(await observed_waiters(cross.ai_sql, pid, "46 authoritative generation COMMIT"))
    denied = await permission
    assert not denied["permitted"] and denied["reason"] == "AUTHORITY_DENIED"
    row = await one_operation(cross, request["turn_id"])
    assert row["status"] == "FAILED" and row["attempt_count"] == 0
    assert row["receipt_id"] == "no-dispatch:" + str(row["id"])
    assert (await cross.counters())["counts"].get("POST", 0) == 0


@pytest.mark.parametrize("cross", [{"mode": "LOOKUP", "proposal_ttl": 1}], indirect=True)
async def test_actual_stale_proposal_cannot_dispatch_later_confirmation(cross):
    await cross.script(model=[{"body": completion(None, [tool_call("business_propose", {"actions": actions(confirmed=True)})])}])
    accepted, claim = await cross.selected()
    proposed = await cross.execute(claim)
    assert proposed.status_code == 200 and proposed.json()["turn"]["status"] == "COMPLETED"
    original = await one_operation(cross, claim["turn"]["id"])
    await asyncio.sleep(1.05)  # Real profile TTL; no mutation of frozen deadlines.
    await cross.script(model=[{"body": completion(None, [tool_call("business_confirm", {"proposal_id": str(original["plan_id"])})])}])
    _, later_claim = await cross.selected("confirm " + str(original["plan_id"]), thread=accepted["conversation"]["provider_thread_id"])
    denied = await cross.execute(later_claim)
    assert denied.status_code == 200
    turn = denied.json()["turn"]
    assert turn["status"] == "FAILED" and turn["draft_text"] is None
    assert turn["operation_refs"] == [{"origin": "AI_AGENT_SERVICE", "operation_id": str(original["id"]), "outcome": "FAILED"}]
    after = await one_operation(cross, claim["turn"]["id"])
    assert after["id"] == original["id"] and after["status"] == "FAILED" and after["attempt_count"] == 0
    assert after["failure_code"] == "STALE_CONFIRMATION" and after["receipt_id"] == "no-dispatch:" + str(original["id"])
    assert after["receipt_evidence"] == {"kind": "NO_DISPATCH", "reason_code": "STALE_CONFIRMATION"}
    assert (await cross.counters())["counts"].get("POST", 0) == 0
    await cross.separated(claim["turn"]["conversation_id"], 2)


# R3 source-fix author additions. These invoke actual public bridges, the shared
# managed HTTP core and the independent provider; no ledger/result is fabricated.

@pytest.mark.parametrize("state", ["unknown", "failed", "proposal", "reserved", "confirmed",
                                   "mixed", "mixed32", "success", "success32"])
async def test_sourcefix_no_tool_retained_plan_guard(cross, state):
    request = await bare_request(cross)
    slots = tuple("guard-" + str(n) for n in range(32 if state.endswith("32") else 3 if state == "mixed" else 1))
    confirmed = state in {"proposal", "confirmed"}
    plan = await cross.worker(request, "reserve", actions=actions(*slots, confirmed=confirmed), propose=confirmed)
    origin = request
    if state == "confirmed":
        request = later_request(request, "confirm " + plan["plan_id"])
        await cross.worker(request, "confirm_only", plan_id=plan["plan_id"])
    if state in {"unknown", "mixed", "mixed32"}:
        if state.startswith("mixed"):
            await cross.worker(request, "claim", ordinal=0, send=True)
        await cross.script(settings={"execute": {"default": {"drop": True}}})
        await cross.worker(request, "claim", ordinal=1 if state.startswith("mixed") else 0, send=True)
    elif state == "failed":
        await cross.script(settings={"execute": {"default": {"negative": True}}})
        await cross.worker(request, "claim", send=True)
    elif state.startswith("success"):
        await cross.worker(request, "execute", actions=actions(*slots))
    rows = await cross.operations(origin["turn_id"])
    originals = [await frozen_snapshot(cross, row) for row in rows]
    expected = (await cross.ai("/managed-turns/evidence", evidence_input(request))).json()["operation_refs"]
    await cross.script(model=[{"body": completion("Готово, запись создана")}], replace_model=True)
    before = await cross.counters()
    response = await cross.ai("/managed-turns/execute", request)
    assert response.status_code == 200
    value = response.json()
    assert value["operation_refs"] == expected and len(expected) == len(slots)
    if state.startswith("success"):
        assert value["status"] == "DRAFT_READY" and value["draft_text"] == "Готово, запись создана"
    elif state == "proposal":
        assert value["status"] == "DRAFT_READY" and "confirm " + plan["plan_id"] in value["draft_text"]
        assert value["draft_text"] != "Готово, запись создана"
    else:
        assert value["status"] in {"FAILED", "UNKNOWN"} and value["draft_text"] is None
    after = await cross.counters()
    assert after["counts"].get("POST", 0) == before["counts"].get("POST", 0)
    assert after["objects"] == before["objects"]
    assert [await frozen_snapshot(cross, row) for row in rows] == originals


@pytest.mark.parametrize("current_success", [False, True])
@pytest.mark.parametrize("blocker_status", ["DISPATCHING", "UNKNOWN"])
async def test_sourcefix_prior_turn_U_guards_current_refs_only(cross, current_success, blocker_status):
    prior, current, rows = await reserved_pair(cross)
    if current_success:
        await cross.worker(current, "claim", send=True)
    else:
        # Use an additional current Turn with no selected plan. Opening it while
        # U is still empty records its own exact trusted projection.
        current = later_request(current, "Какой адрес?")
        await cross.script(model=[{"body": completion("Адрес доступен")}], replace_model=True)
        assert (await cross.ai("/managed-turns/execute", current)).json()["status"] == "DRAFT_READY"
    await cross.script(settings={"execute": {"default": {"drop": True}}})
    await cross.worker(prior, "claim", send=blocker_status == "UNKNOWN")
    expected = (await cross.ai("/managed-turns/evidence", evidence_input(current))).json()["operation_refs"]
    prior_evidence = (await cross.ai("/managed-turns/evidence", evidence_input(prior))).json()
    assert (await one_operation(cross, prior["turn_id"]))["status"] == blocker_status
    before = await cross.counters()
    await cross.script(model=[{"body": completion("Готово, запись создана")}], replace_model=True)
    value = ai_unknown(await cross.ai("/managed-turns/execute", current))
    assert value["operation_refs"] == expected
    assert len(expected) == (1 if current_success else 0)
    assert all(ref["operation_id"] != str(rows[0]["id"]) for ref in expected)
    assert (await cross.ai("/managed-turns/evidence", evidence_input(prior))).json() == prior_evidence
    assert (await cross.counters())["counts"].get("POST", 0) == before["counts"].get("POST", 0)


@pytest.mark.parametrize("method", ["reserve", "dispatch", "confirm"])
async def test_sourcefix_raw_material_facts_cannot_bypass_public_bridge(cross, method):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions(confirmed=method == "confirm"), propose=method == "confirm")
    before = await cross.counters()
    result = await cross.worker(request, "raw_facts", method=method, actions=actions())
    assert result.get("safety_code") and result.get("rejected") is not False
    after = await cross.counters()
    assert after["counts"].get("POST", 0) == before["counts"].get("POST", 0)
    assert (await one_operation(cross, request["turn_id"]))["attempt_count"] == 0


@pytest.mark.parametrize("purpose", ["reserve", "first", "residual", "confirm", "native"])
@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("cross", ["NATIVE"], indirect=True)
async def test_sourcefix_post_P_wait_facts_matrix(cross, purpose, changed):
    request = await bare_request(cross)
    slots = ("wait-0", "wait-1") if purpose == "residual" else ("wait-0",)
    if purpose != "reserve":
        plan = await cross.worker(request, "reserve", actions=actions(*slots, confirmed=purpose == "confirm"), propose=purpose == "confirm")
        if purpose == "residual":
            await cross.worker(request, "claim", ordinal=0, send=True)
        elif purpose == "native":
            await cross.script(settings={"execute": {"default": {"drop": True}}})
            await cross.worker(request, "claim", send=True)
            await cross.script(settings={"execute": {"default": {}}})
        elif purpose == "confirm":
            request = later_request(request, "confirm " + plan["plan_id"])
    before = await cross.counters()
    async with cross.ai_sql.begin() as holder:
        pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        await holder.execute(text("SELECT id FROM agent_templates WHERE id=:id FOR UPDATE"), {"id": request["agent_id"]})
        if purpose == "reserve":
            task = asyncio.create_task(cross.worker(request, "reserve_direct", actions=actions(*slots)))
        elif purpose == "confirm":
            task = asyncio.create_task(cross.worker(request, "confirm_direct", plan_id=plan["plan_id"]))
        else:
            task = asyncio.create_task(cross.worker(request, "claim", ordinal=1 if purpose == "residual" else 0,
                                                    native=purpose == "native", send=True))
        cross.lock_evidence.append(await observed_waiters(cross.ai_sql, pid, "sourcefix P " + purpose + " changed=" + str(changed)))
        if changed:
            await cross.script(facts={"values": {"resource": "revision-2"}, "revisions": {"catalog": 2}})
    result = await task
    after = await cross.counters()
    recent = cross.trace("fresh")
    assert recent[-1]["revisions"] == {"catalog": 2 if changed else 1}
    if changed:
        assert after["counts"].get("POST", 0) == before["counts"].get("POST", 0)
        if purpose == "reserve":
            assert result.get("safety_code") and await cross.operations(request["turn_id"]) == []
        elif purpose == "confirm":
            async with cross.ai_sql.connect() as db:
                assert await db.scalar(text("SELECT confirmed_at FROM business_plans WHERE id=:id"), {"id": plan["plan_id"]}) is None
        else:
            assert not result["permitted"]
            if purpose == "native":
                assert result["operation_refs"][0]["outcome"] == "UNKNOWN"
    elif purpose in {"reserve", "confirm"}:
        assert result["plan_id"]
    else:
        assert result["permitted"] and after["counts"].get("POST", 0) == before["counts"].get("POST", 0) + 1
    # FRESH is outside every live authority lock, including all selected rows.
    async with cross.ai_sql.begin() as observer:
        await observer.execute(text("SELECT id FROM agent_templates WHERE id=:id FOR UPDATE NOWAIT"), {"id": request["agent_id"]})


async def test_sourcefix_renewed_scope_contention_no_token_no_hidden_retry(cross):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions())
    row = await one_operation(cross, request["turn_id"])
    frozen = await frozen_snapshot(cross, row)
    cross.fault("FRESH-claim_dispatch")
    attempt = asyncio.create_task(cross.worker(request, "claim", send=True))
    await cross.phase("FRESH-claim_dispatch")
    before_release = await cross.counters()
    # The real provider facts HTTP read has completed. All authority locks can
    # independently be obtained while this callback is paused outside the DB.
    async with cross.ai_sql.begin() as holder:
        for table, identity in (("agent_templates", request["agent_id"]),
                                ("business_scopes", row["scope_id"]),
                                ("business_plans", row["plan_id"]),
                                ("business_operations", row["id"])):
            await holder.execute(text("SELECT id FROM " + table + " WHERE id=:id FOR UPDATE NOWAIT"), {"id": identity})
        await holder.execute(text("SELECT id FROM business_bindings WHERE agent_id=:id FOR UPDATE NOWAIT"), {"id": request["agent_id"]})
        cross.fault(); (cross.root / "release-FRESH-claim_dispatch").touch()
        denied = await asyncio.wait_for(attempt, 3)
        assert not denied["permitted"]
    after = await cross.counters()
    assert after["counts"].get("POST", 0) == 0
    assert after["counts"]["FACTS"] == before_release["counts"]["FACTS"]
    assert await frozen_snapshot(cross, row) == frozen
    # Only a later explicit attempt gets another P and another FRESH.
    assert (await cross.worker(request, "claim", send=True))["permitted"]
    assert (await cross.counters())["counts"]["POST"] == (await cross.counters())["objects"] == 1


@pytest.mark.parametrize("purpose", ["reserve", "first", "residual", "confirm", "native"])
@pytest.mark.parametrize("cross", ["NATIVE"], indirect=True)
async def test_sourcefix_each_decisive_admission_contender_has_no_token(cross, purpose):
    request = await bare_request(cross)
    slots = ("contend-0", "contend-1") if purpose == "residual" else ("contend-0",)
    plan = None
    if purpose != "reserve":
        plan = await cross.worker(request, "reserve", actions=actions(*slots, confirmed=purpose == "confirm"), propose=purpose == "confirm")
        if purpose == "residual":
            await cross.worker(request, "claim", ordinal=0, send=True)
        elif purpose == "native":
            await cross.script(settings={"execute": {"default": {"drop": True}}})
            await cross.worker(request, "claim", send=True)
            await cross.script(settings={"execute": {"default": {}}})
        elif purpose == "confirm":
            request = later_request(request, "confirm " + plan["plan_id"])
    async with cross.ai_sql.connect() as db:
        scope_id = await db.scalar(text("SELECT id FROM business_scopes WHERE agent_id=:agent AND conversation_id=:conversation"),
                                   {"agent": request["agent_id"], "conversation": request["conversation_id"]})
        binding_id = await db.scalar(text("SELECT id FROM business_bindings WHERE agent_id=:agent AND active"), {"agent": request["agent_id"]})
    rows = await cross.operations()
    snapshots = [await frozen_snapshot(cross, row) for row in rows]
    before = await cross.counters()
    original_tokens = len(cross.trace("dispatch_token"))
    method = "reserve_plan" if purpose == "reserve" else "confirm_plan" if purpose == "confirm" else "claim_dispatch"
    # P returned after closing its session. Gate F1 specifically, avoiding R's
    # initial F0, and then contend after the real facts HTTP read completed.
    cross.fault("P-" + method)
    if purpose == "reserve":
        task = asyncio.create_task(cross.worker(request, "reserve_direct", actions=actions(*slots)))
    elif purpose == "confirm":
        task = asyncio.create_task(cross.worker(request, "confirm_direct", plan_id=plan["plan_id"]))
    else:
        task = asyncio.create_task(cross.worker(request, "claim", ordinal=1 if purpose == "residual" else 0,
                                                native=purpose == "native", send=True))
    await cross.phase("P-" + method)
    cross.fault("FRESH-" + method); (cross.root / ("release-P-" + method)).touch()
    await cross.phase("FRESH-" + method)
    fact_count = (await cross.counters())["counts"]["FACTS"]
    async with cross.ai_sql.begin() as holder:
        # These independent NOWAIT acquisitions prove the callback holds no
        # runtime authority locks or surrounding DB configuration transaction.
        for table, identity in (("agent_templates", request["agent_id"]), ("business_bindings", binding_id),
                                ("business_scopes", scope_id)):
            await holder.execute(text("SELECT id FROM " + table + " WHERE id=:id FOR UPDATE NOWAIT"), {"id": identity})
        if plan:
            await holder.execute(text("SELECT id FROM business_plans WHERE id=:id FOR UPDATE NOWAIT"), {"id": plan["plan_id"]})
            await holder.execute(text("SELECT id FROM business_operations WHERE plan_id=:id ORDER BY ordinal FOR UPDATE NOWAIT"), {"id": plan["plan_id"]})
        cross.fault(); (cross.root / ("release-FRESH-" + method)).touch()
        result = await asyncio.wait_for(task, 3)
        assert result.get("reason", result.get("safety_code")) == "ADMISSION_CONTENDED"
        assert not result.get("permitted", False) and not result.get("plan_id")
    after = await cross.counters()
    assert after["counts"].get("POST", 0) == before["counts"].get("POST", 0)
    assert after["counts"]["FACTS"] == fact_count and len(cross.trace("dispatch_token")) == original_tokens
    assert [await frozen_snapshot(cross, row) for row in rows] == snapshots
    if purpose == "reserve":
        assert await cross.operations(request["turn_id"]) == []
        assert (await cross.worker(request, "reserve_direct", actions=actions(*slots)))["plan_id"]
    elif purpose == "confirm":
        async with cross.ai_sql.connect() as db:
            assert await db.scalar(text("SELECT confirmed_at FROM business_plans WHERE id=:id"), {"id": plan["plan_id"]}) is None
        assert (await cross.worker(request, "confirm_direct", plan_id=plan["plan_id"]))["plan_id"] == plan["plan_id"]
    else:
        assert (await cross.worker(request, "claim", ordinal=1 if purpose == "residual" else 0,
                                  native=purpose == "native", send=True))["permitted"]


async def test_sourcefix_expected_facts_change_after_positive_D_is_attempt_only(cross):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions())
    cross.fault("B2")
    executing = asyncio.create_task(cross.worker(request, "claim", send=True))
    await cross.phase("B2")
    assert (await one_operation(cross, request["turn_id"]))["status"] == "DISPATCHING"
    await cross.script(facts={"values": {"resource": "changed-after-D"}, "revisions": {"catalog": 2}})
    cross.fault(); (cross.root / "release-B2").touch()
    assert (await executing)["permitted"]
    row = await one_operation(cross, request["turn_id"])
    assert row["status"] == "UNKNOWN" and row["attempt_count"] == 1
    counters = await cross.counters()
    assert counters["counts"]["POST"] == counters["counts"]["REJECT"] == 1 and counters["objects"] == 0
    await cross.worker(request, "reconcile", operation_id=str(row["id"]))
    assert (await one_operation(cross, request["turn_id"]))["status"] == "UNKNOWN"
    assert (await cross.counters())["counts"]["POST"] == 1


@pytest.mark.parametrize("prefix_done", [1, 2, 3])
async def test_sourcefix_stale_each_ordinal_and_all_terminal_consumed(cross, prefix_done):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions("stale-0", "stale-1", "stale-2"))
    original_ids = [row["id"] for row in await cross.operations(request["turn_id"])]
    cross.fault("STALE")
    stale = asyncio.create_task(cross.worker(request, "execute_stale"))
    await cross.phase("STALE")
    for ordinal in range(prefix_done):
        assert (await cross.worker(request, "claim", ordinal=ordinal, send=True))["permitted"]
    cross.fault(); (cross.root / "release-STALE").touch()
    value = await stale
    assert value["halt_status"] is None
    rows = await cross.operations(request["turn_id"])
    assert [row["id"] for row in rows] == original_ids
    assert [row["status"] for row in rows] == ["SUCCEEDED"] * 3
    counters = await cross.counters()
    assert counters["counts"]["POST"] == counters["objects"] == len(counters["object_ids"]) == 3
    assert {post["operation_id"] for post in counters["posts"]} == {str(identity) for identity in original_ids}


@pytest.mark.parametrize("invalidation", ["other_U", "facts", "authority", "failed_predecessor"])
async def test_sourcefix_stale_success_residual_rechecks_full_current_permission(cross, invalidation):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions("stale-0", "stale-1"))
    other = later_request(request)
    if invalidation == "other_U":
        await cross.worker(other, "reserve", actions=actions("other"))
    cross.fault("STALE")
    stale = asyncio.create_task(cross.worker(request, "execute_stale"))
    await cross.phase("STALE")
    if invalidation == "failed_predecessor":
        # A real permitted RESERVED→DISPATCHING→FAILED transition. An immutable
        # SUCCEEDED row is never rewritten to manufacture this interleaving.
        await cross.script(settings={"execute": {"default": {"negative": True}}})
    await cross.worker(request, "claim", ordinal=0, send=True)
    if invalidation == "other_U":
        await cross.script(settings={"execute": {"default": {"drop": True}}})
        await cross.worker(other, "claim", send=True)
    elif invalidation == "facts":
        await cross.script(facts={"values": {}, "revisions": {"catalog": 2}})
    elif invalidation == "authority":
        await cross.worker(request, "configure", enabled_capabilities=[], authorization_generation=2)
    cross.fault(); (cross.root / "release-STALE").touch()
    value = await stale
    assert value["halt_status"] in {"FAILED", "UNKNOWN"}
    rows = await cross.operations(request["turn_id"])
    assert rows[1]["attempt_count"] == 0 and rows[1]["status"] in {"RESERVED", "FAILED"}
    assert not any(post["operation_id"] == str(rows[1]["id"]) for post in (await cross.counters())["posts"])


async def test_sourcefix_final_coherent_read_wait_sees_new_prior_U(cross):
    prior, current, rows = await reserved_pair(cross)
    await cross.worker(current, "claim", send=True)
    await cross.script(model=[{"body": completion("Готово, запись создана")}], replace_model=True)
    cross.fault("FINAL-READ")
    final = asyncio.create_task(cross.ai("/managed-turns/execute", current))
    await cross.phase("FINAL-READ")
    cross.fault("LOCK-claim_dispatch")
    changing = asyncio.create_task(cross.worker(prior, "claim"))
    await cross.phase("LOCK-claim_dispatch")
    pid = [record for record in cross.trace("scope_lock") if record["method"] == "claim_dispatch" and record["decisive"]][-1]["pid"]
    (cross.root / "release-FINAL-READ").touch()
    cross.lock_evidence.append(await observed_waiters(cross.ai_sql, pid, "sourcefix final coherent guard read"))
    cross.fault(); (cross.root / "release-LOCK-claim_dispatch").touch()
    assert (await changing)["permitted"]
    value = ai_unknown(await final)
    assert value["operation_refs"] == [{"origin": "AI_AGENT_SERVICE", "operation_id": str(rows[1]["id"]), "outcome": "SUCCEEDED"}]
    assert (await one_operation(cross, prior["turn_id"]))["status"] == "DISPATCHING"
    assert (await cross.counters())["counts"]["POST"] == 1


@pytest.mark.parametrize("current_success", [False, True])
@pytest.mark.parametrize("cleanup_failure", [False, True])
async def test_sourcefix_final_unavailable_retains_terminal_receipts_without_draft(cross, current_success, cleanup_failure):
    request = await bare_request(cross)
    if current_success:
        await cross.worker(request, "execute", actions=actions("receipt-0", "receipt-1"))
    expected = (await cross.ai("/managed-turns/evidence", evidence_input(request))).json()["operation_refs"]
    await cross.script(model=[{"body": completion("Готово, запись создана")}], replace_model=True)
    private_json(cross.root / "faults.json", {"final_read_failure": True, "cleanup_failure": cleanup_failure})
    response = await cross.ai("/managed-turns/execute", request)
    assert response.status_code == 200 and response.json()["draft_text"] is None
    assert response.json()["status"] == "UNKNOWN" and response.json()["operation_refs"] == expected
    if cleanup_failure:
        assert response.json()["error_code"] == "TOOL_CLEANUP_FAILED"
    assert len(expected) == (2 if current_success else 0) and all(ref["outcome"] == "SUCCEEDED" for ref in expected)
    counters = await cross.counters()
    assert counters["counts"].get("POST", 0) == counters["objects"] == (2 if current_success else 0)


async def wait_past_original_cache_and_deadline(cross, operation):
    deadline = asyncio.get_running_loop().time() + 10
    while True:
        async with cross.ai_sql.connect() as db:
            expired = await db.scalar(text("SELECT clock_timestamp()>native_replay_until FROM business_operations WHERE id=:id"), {"id": operation["id"]})
            lease_live = await db.scalar(text("SELECT clock_timestamp()<dispatch_lease_expires_at FROM business_operations WHERE id=:id"), {"id": operation["id"]})
        async with cross.provider_sql.connect() as db:
            cache_expired = await db.scalar(text("SELECT clock_timestamp()>expires_at FROM fake_keys WHERE native_key=:key"), {"key": operation["provider_idempotency_key"]})
        if expired and (cache_expired or cache_expired is None):
            assert lease_live, "The original 8-second profile lease must not be extended for the deadline test"
            return
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(.025)


async def wait_provider_rejection(cross):
    deadline = asyncio.get_running_loop().time() + 5
    while (await cross.counters())["counts"].get("REJECT", 0) == 0:
        assert asyncio.get_running_loop().time() < deadline
        await asyncio.sleep(.02)


@pytest.mark.parametrize("cross", [{"mode": "NATIVE", "native_policy": "BOUNDED", "horizon": 3}], indirect=True)
async def test_sourcefix_bounded_post_D_pause_local_guard_preserves_original_unknown(cross):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions())
    await cross.script(settings={"execute": {"default": {"drop": True}}})
    await cross.worker(request, "claim", send=True)
    original = await one_operation(cross, request["turn_id"])
    original_objects = (await cross.counters())["object_ids"]
    await cross.script(settings={"execute": {"default": {}}})
    cross.fault("B2")
    replay = asyncio.create_task(cross.worker(request, "native", operation_id=str(original["id"])))
    await cross.phase("B2")
    await wait_past_original_cache_and_deadline(cross, original)
    cross.fault(); (cross.root / "release-B2").touch()
    await replay
    after = await one_operation(cross, request["turn_id"])
    assert after["status"] == "UNKNOWN" and after["attempt_count"] == 2
    for key in ("id", "request_payload", "request_fingerprint", "provider_idempotency_key", "provider_profile_revision", "issued_at", "native_replay_until"):
        assert after[key] == original[key]
    counters = await cross.counters()
    assert counters["counts"]["POST"] == counters["objects"] == 1 and counters["object_ids"] == original_objects
    assert len(cross.trace("dispatch_token")) == 2
    assert {record["accept_before"] for record in cross.trace("dispatch_token")} == {str(original["native_replay_until"])}
    await cross.worker(request, "reconcile", operation_id=str(original["id"]))
    assert (await one_operation(cross, request["turn_id"]))["status"] == "SUCCEEDED"


@pytest.mark.parametrize("cross", [{"mode": "NATIVE", "native_policy": "BOUNDED", "horizon": 3}], indirect=True)
async def test_sourcefix_bounded_transport_accepts_bytes_before_deadline_rejects_after(cross):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions())
    await cross.script(settings={"execute": {"default": {"drop": True}}})
    await cross.worker(request, "claim", send=True)
    original = await one_operation(cross, request["turn_id"])
    await cross.script(settings={"execute": {"default": {"before_accept": "late-transport"}}})
    replay = asyncio.create_task(cross.worker(request, "native", operation_id=str(original["id"])))
    await cross.provider_barrier("late-transport")
    assert (await cross.counters())["counts"]["POST"] == 2
    await wait_past_original_cache_and_deadline(cross, original)
    await cross.script(release=["late-transport"])
    await replay
    await wait_provider_rejection(cross)
    counters = await cross.counters()
    assert counters["counts"]["POST"] == 2 and counters["counts"]["REJECT"] == 1
    assert counters["objects"] == len(counters["object_ids"]) == 1
    assert (await one_operation(cross, request["turn_id"]))["status"] == "UNKNOWN"
    assert {datetime.fromisoformat(post["dispatch"]["accept_before"].replace("Z", "+00:00"))
            for post in counters["posts"]} == {original["native_replay_until"]}


@pytest.mark.parametrize("provider_lock", ["facts", "key"])
@pytest.mark.parametrize("cross", [{"mode": "NATIVE", "native_policy": "BOUNDED", "horizon": 3}], indirect=True)
async def test_sourcefix_provider_acceptance_clock_after_every_authority_wait(cross, provider_lock):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions())
    await cross.script(settings={"execute": {"default": {"drop": True}}})
    await cross.worker(request, "claim", send=True)
    original = await one_operation(cross, request["turn_id"])
    await cross.script(settings={"execute": {"default": {}}})
    cross.fault("B2")
    replay = asyncio.create_task(cross.worker(request, "native", operation_id=str(original["id"])))
    await cross.phase("B2")
    async with cross.provider_sql.begin() as holder:
        pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        if provider_lock == "facts":
            await holder.execute(text("SELECT id FROM fake_facts WHERE id=1 FOR UPDATE"))
        else:
            await holder.execute(text("SELECT native_key FROM fake_key_authorities WHERE native_key=:key FOR UPDATE"), {"key": original["provider_idempotency_key"]})
        cross.fault(); (cross.root / "release-B2").touch()
        cross.lock_evidence.append(await observed_waiters(cross.provider_sql, pid, "sourcefix provider acceptance " + provider_lock))
        await wait_past_original_cache_and_deadline(cross, original)
    await replay
    await wait_provider_rejection(cross)
    counters = await cross.counters()
    assert counters["counts"]["POST"] == 2 and counters["counts"]["REJECT"] == 1 and counters["objects"] == 1
    async with cross.provider_sql.connect() as db:
        accepted = (await db.execute(text("SELECT accepted_at,decision FROM fake_requests WHERE kind='POST' ORDER BY sequence DESC LIMIT 1"))).mappings().one()
        assert accepted["accepted_at"] >= original["native_replay_until"] and accepted["decision"] == "ACCEPTANCE_EXPIRED"
    assert (await one_operation(cross, request["turn_id"]))["status"] == "UNKNOWN"


@pytest.mark.parametrize("cross", [{"mode": "NATIVE", "native_policy": "BOUNDED", "horizon": 3}], indirect=True)
async def test_sourcefix_displaced_old_FIRST_and_unenveloped_paths_are_guarded(cross):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions())
    await cross.worker(request, "claim")  # Original positively acknowledged FIRST, no send.
    original = await one_operation(cross, request["turn_id"])
    token = cross.trace("dispatch_token")[-1]
    old_dispatch = {"command": token["command"], "attempt": {key: token[key] for key in
        ("operation_id", "state_version", "dispatch_epoch", "dispatch_nonce")}, "kind": "FIRST",
        "accept_before": token["accept_before"], "execution_contract": token["execution_contract"]}
    await cross.expire_attempt(original["id"])
    await cross.worker(request, "recover")
    await cross.worker(request, "native", operation_id=str(original["id"]))
    assert (await one_operation(cross, request["turn_id"]))["status"] == "SUCCEEDED"
    await wait_past_original_cache_and_deadline(cross, original)
    # Deliver the already issued displaced FIRST directly through the same
    # guarded provider endpoint after the original acceptance guarantee ended.
    response = await cross.client.post(cross.url("provider") + "/execute", json=old_dispatch)
    assert response.status_code == 200 and response.json()["kind"] == "ATTEMPT_REJECTED"
    assert response.json()["reason_code"] == "ACCEPTANCE_EXPIRED"
    legacy = await cross.client.post(cross.url("provider") + "/execute", json=token["command"])
    assert legacy.status_code == 409 and legacy.json()["reason_code"] == "UNENVELOPED_ATTEMPT"
    counters = await cross.counters()
    assert counters["counts"]["POST"] == 3 and counters["counts"]["REJECT"] == 2 and counters["objects"] == 1
    assert (await one_operation(cross, request["turn_id"]))["status"] == "SUCCEEDED"


@pytest.mark.parametrize("cross", [{"mode": "NATIVE", "native_policy": "BOUNDED", "horizon": 30,
                                     "bounded_acceptance": "FENCED"}], indirect=True)
async def test_sourcefix_fenced_bounded_profile_never_grants_native_replay(cross):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions())
    await cross.script(settings={"execute": {"default": {"drop": True}}})
    await cross.worker(request, "claim", send=True)
    original = await one_operation(cross, request["turn_id"])
    before = await frozen_snapshot(cross, original)
    await cross.worker(request, "native", operation_id=str(original["id"]))
    assert await frozen_snapshot(cross, original) == before
    assert (await cross.counters())["counts"]["POST"] == (await cross.counters())["objects"] == 1
    assert len(cross.trace("dispatch_token")) == 1


@pytest.mark.parametrize("failure", ["inactive", "key"])
async def test_sourcefix_initial_retained_U_guard_survives_live_setup_failure(cross, failure):
    prior = await bare_request(cross)
    current = later_request(prior, "Какой адрес?")
    await cross.script(model=[{"body": completion("Адрес доступен")}], replace_model=True)
    assert (await cross.ai("/managed-turns/execute", current)).json()["status"] == "DRAFT_READY"
    await cross.worker(prior, "reserve", actions=actions())
    await cross.script(settings={"execute": {"default": {"drop": True}}})
    await cross.worker(prior, "claim", send=True)
    async with cross.ai_sql.begin() as db:
        column = "is_active=false" if failure == "inactive" else "encrypted_openrouter_key=NULL"
        await db.execute(text("UPDATE agent_templates SET " + column + " WHERE id=:id"), {"id": prior["agent_id"]})
    before = await cross.counters()
    value = ai_unknown(await cross.ai("/managed-turns/execute", current))
    assert value["operation_refs"] == []
    assert (await cross.counters())["counts"]["POST"] == before["counts"]["POST"] == 1


async def test_sourcefix_provider_facts_reload_after_its_actual_wait(cross):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions())
    cross.fault("B2")
    executing = asyncio.create_task(cross.worker(request, "claim", send=True))
    await cross.phase("B2")
    async with cross.provider_sql.begin() as writer:
        pid = await writer.scalar(text("SELECT pg_backend_pid()"))
        await writer.execute(text("SELECT id FROM fake_facts WHERE id=1 FOR UPDATE"))
        cross.fault(); (cross.root / "release-B2").touch()
        cross.lock_evidence.append(await observed_waiters(cross.provider_sql, pid, "sourcefix mutable provider facts authority"))
        # The authoritative facts writer owns the same lock as acceptance.
        await writer.execute(text("UPDATE fake_facts SET values=CAST(:values AS jsonb),"
            "revisions=CAST(:revisions AS jsonb) WHERE id=1"), {"values": "{}", "revisions": json.dumps({"catalog": 2})})
    await executing
    await wait_provider_rejection(cross)
    counters = await cross.counters()
    assert counters["counts"]["POST"] == counters["counts"]["REJECT"] == 1 and counters["objects"] == 0
    assert (await one_operation(cross, request["turn_id"]))["status"] == "UNKNOWN"


@pytest.mark.parametrize("change", ["generation", "other_U"])
async def test_sourcefix_change_between_FRESH_and_decisive_admission(cross, change):
    first, other, rows = await reserved_pair(cross)
    original = await frozen_snapshot(cross, rows[0])
    cross.fault("FRESH-claim_dispatch")
    attempt = asyncio.create_task(cross.worker(first, "claim", send=True))
    await cross.phase("FRESH-claim_dispatch")
    # Clear only the fault selector. The first callback remains paused on its
    # original release file, while a separate real trusted worker changes state.
    cross.fault()
    if change == "generation":
        await cross.worker(first, "configure", enabled_capabilities=["booking.create", "booking.confirm_create"], authorization_generation=2)
    else:
        await cross.script(settings={"execute": {"default": {"drop": True}}})
        assert (await cross.worker(other, "claim", send=True))["permitted"]
    (cross.root / "release-FRESH-claim_dispatch").touch()
    result = await attempt
    assert result.get("permitted") is not True
    assert (await one_operation(cross, first["turn_id"]))["attempt_count"] == 0
    assert not any(post["operation_id"] == str(rows[0]["id"]) for post in (await cross.counters())["posts"])
    if change == "other_U":
        assert await frozen_snapshot(cross, rows[0]) == original


@pytest.mark.parametrize("cross", [{"mode": "LOOKUP", "proposal_ttl": 2}], indirect=True)
async def test_sourcefix_stale_success_residual_uses_original_expiry(cross):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions("expiry-0", "expiry-1"))
    original = (await cross.operations(request["turn_id"]))[1]
    cross.fault("STALE")
    stale = asyncio.create_task(cross.worker(request, "execute_stale"))
    await cross.phase("STALE")
    await cross.worker(request, "claim", ordinal=0, send=True)
    await asyncio.sleep(2.05)  # Real immutable proposal expiry, no deadline rewrite.
    cross.fault(); (cross.root / "release-STALE").touch()
    assert (await stale)["halt_status"] in {"FAILED", "UNKNOWN"}
    after = (await cross.operations(request["turn_id"]))[1]
    assert after["id"] == original["id"] and after["attempt_count"] == 0 and after["status"] == "FAILED"
    assert (await cross.counters())["counts"]["POST"] == (await cross.counters())["objects"] == 1


@pytest.mark.parametrize("cross", [{"mode": "NATIVE", "native_policy": "BOUNDED", "horizon": 3,
                                     "bounded_acceptance": "FENCED", "retained_legacy": True}], indirect=True)
async def test_sourcefix_exact_legacy_catalog_retained_old_FIRST_cannot_gain_replay(cross):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions())
    await cross.worker(request, "claim")
    original = await one_operation(cross, request["turn_id"])
    saved = cross.trace("dispatch_token")[-1]
    async with cross.ai_sql.connect() as db:
        legacy_catalog = await db.scalar(text("SELECT catalog_revision FROM business_plans WHERE id=:id"), {"id": original["plan_id"]})
    await cross.expire_attempt(original["id"])
    await cross.worker(request, "recover")
    before = await frozen_snapshot(cross, original)
    # Reopen the exact old catalog/profile, preserving the original durable rows.
    await cross.restart("ai", abrupt=True)
    await cross.worker(request, "native", operation_id=str(original["id"]))
    assert await frozen_snapshot(cross, original) == before
    assert len(cross.trace("dispatch_token")) == 1 and (await cross.counters())["counts"].get("POST", 0) == 0
    await asyncio.sleep(3.1)
    # This profile openly retains an unprotected old FIRST acceptance path.
    # Its existence is why the new worker must remain FENCED, even with a new
    # typed envelope implementation. No guarded claim is made for this profile.
    late = await cross.client.post(cross.url("provider") + "/execute", json=saved["command"])
    assert late.status_code == 200 and late.json()["kind"] == "SUCCEEDED"
    assert (await one_operation(cross, request["turn_id"]))["status"] == "UNKNOWN"
    await cross.worker(request, "reconcile", operation_id=str(original["id"]))
    after = await one_operation(cross, request["turn_id"])
    assert after["status"] == "SUCCEEDED"
    for key in ("id", "request_payload", "request_fingerprint", "provider_idempotency_key", "provider_profile_revision", "issued_at", "native_replay_until"):
        assert after[key] == original[key]
    async with cross.ai_sql.connect() as db:
        assert await db.scalar(text("SELECT catalog_revision FROM business_plans WHERE id=:id"), {"id": original["plan_id"]}) == legacy_catalog
    assert (await cross.counters())["counts"]["POST"] == (await cross.counters())["objects"] == 1


@pytest.mark.parametrize("cross", [{"mode": "NATIVE", "native_policy": "BOUNDED", "horizon": 3,
                                     "bounded_acceptance": "FENCED", "retained_legacy": True}], indirect=True)
async def test_sourcefix_evicted_key_negative_attempt_cannot_disprove_original_B1(cross):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions())
    await cross.script(settings={"execute": {"default": {"drop": True}}})
    await cross.worker(request, "claim", send=True)
    original = await one_operation(cross, request["turn_id"])
    saved = cross.trace("dispatch_token")[-1]
    object_ids = (await cross.counters())["object_ids"]
    await asyncio.sleep(3.1)
    await cross.script(settings={"execute": {"default": {"negative": True}}})
    late = await cross.client.post(cross.url("provider") + "/execute", json=saved["command"])
    assert late.status_code == 200 and late.json()["kind"] == "SUCCEEDED"
    assert late.json()["object_id"] in object_ids
    assert (await one_operation(cross, request["turn_id"]))["status"] == "UNKNOWN"
    async with cross.provider_sql.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM fake_keys WHERE native_key=:key"), {"key": original["provider_idempotency_key"]}) == 0
    counters = await cross.counters()
    assert counters["counts"]["POST"] == 2 and counters["counts"]["MUTATION"] == 1
    assert counters["objects"] == len(counters["object_ids"]) == 1 and counters["object_ids"] == object_ids
    await cross.worker(request, "reconcile", operation_id=str(original["id"]))
    assert (await one_operation(cross, request["turn_id"]))["status"] == "SUCCEEDED"


@pytest.mark.parametrize("cross", [{"mode": "NATIVE", "native_policy": "BOUNDED", "horizon": 3,
                                     "bounded_acceptance": "FENCED", "retained_legacy": True}], indirect=True)
async def test_sourcefix_original_noeffect_permanently_fences_expired_legacy_FIRST(cross):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions())
    await cross.script(settings={"execute": {"default": {"negative": True}}})
    await cross.worker(request, "claim", send=True)
    original = await one_operation(cross, request["turn_id"])
    saved = cross.trace("dispatch_token")[-1]
    frozen = await frozen_snapshot(cross, original)
    assert original["status"] == "FAILED" and original["receipt_id"] and original["object_id"] is None
    await asyncio.sleep(3.1)
    await cross.script(settings={"execute": {"default": {}}})
    late = await cross.client.post(cross.url("provider") + "/execute", json=saved["command"])
    assert late.status_code == 200 and late.json()["kind"] == "FAILED_NO_EFFECT"
    assert late.json()["receipt_id"] == original["receipt_id"]
    assert late.json()["binding"] == {key: saved["command"][key] for key in late.json()["binding"]}
    counters = await cross.counters()
    assert counters["counts"]["POST"] == counters["counts"]["ACCEPT"] == 2
    assert counters["counts"].get("MUTATION", 0) == counters["objects"] == 0 and counters["object_ids"] == []
    async with cross.provider_sql.connect() as db:
        assert await db.scalar(text("SELECT count(*) FROM fake_keys WHERE native_key=:key"), {"key": original["provider_idempotency_key"]}) == 0
        retained = await db.scalar(text("SELECT receipt FROM fake_receipts WHERE operation_id=:op"), {"op": str(original["id"])})
        assert retained == late.json()
    assert await frozen_snapshot(cross, original) == frozen
    lookup = await cross.client.get(cross.url("provider") + "/operations/" + str(original["id"]))
    assert lookup.json() == retained
    conflicting = copy.deepcopy(saved["command"])
    conflicting["request_fingerprint"] = "f" * 64 if conflicting["request_fingerprint"] != "f" * 64 else "e" * 64
    denied = await cross.client.post(cross.url("provider") + "/execute", json=conflicting)
    assert denied.status_code == 409 and denied.json()["reason_code"] == "ORIGINAL_BINDING_CONFLICT"
    assert (await cross.counters())["objects"] == 0


@pytest.mark.parametrize("cross", [{"mode": "NATIVE", "native_policy": "BOUNDED", "horizon": 3,
                                     "bounded_acceptance": "FENCED", "retained_legacy": True}], indirect=True)
async def test_sourcefix_legacy_success_cache_honestly_expires_without_durable_dedupe(cross):
    request = await bare_request(cross)
    await cross.worker(request, "reserve", actions=actions())
    await cross.script(settings={"execute": {"default": {"drop": True}}})
    await cross.worker(request, "claim", send=True)
    original = await one_operation(cross, request["turn_id"])
    saved = cross.trace("dispatch_token")[-1]
    first_ids = (await cross.counters())["object_ids"]
    await asyncio.sleep(3.1)
    await cross.script(settings={"execute": {"default": {}}})
    late = await cross.client.post(cross.url("provider") + "/execute", json=saved["command"])
    assert late.status_code == 200 and late.json()["kind"] == "SUCCEEDED" and late.json()["object_id"] not in first_ids
    counters = await cross.counters()
    # This explicit unsafe legacy path is exactly why new replay is FENCED.
    # The fake has not silently become a permanent-success dedupe provider.
    assert counters["counts"]["POST"] == counters["counts"]["MUTATION"] == counters["objects"] == 2
    assert len(counters["object_ids"]) == 2 and set(first_ids) < set(counters["object_ids"])
    assert len(cross.trace("dispatch_token")) == 1
    assert (await one_operation(cross, request["turn_id"]))["status"] == "UNKNOWN"
    lookup = await cross.client.get(cross.url("provider") + "/operations/" + str(original["id"]))
    assert lookup.json()["object_id"] in first_ids
