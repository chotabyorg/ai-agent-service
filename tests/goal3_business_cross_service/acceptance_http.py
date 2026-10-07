"""Real paired Goal3 HTTP/PG/process acceptance; explicit-file collection only."""

import asyncio
import copy
import json
from uuid import uuid4

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
    assert after["counts"]["MODEL"] == before["counts"]["MODEL"] + 1
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
    assert outcomes[0]["plan_id"] == outcomes[1]["plan_id"]
    if existing:
        assert outcomes[0]["plan_id"] == prior["plan_id"]
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
    assert next(result for result in outcomes if not result["permitted"])["reason"] in {"IN_FLIGHT", "UNKNOWN"}
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
    assert all(outcome["plan_id"] == plan["plan_id"] for outcome in outcomes)
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
    assert after["failure_code"] == "STALE_AUTHORITY_OR_CONTEXT" and after["receipt_id"] == "no-dispatch:" + str(original["id"])
    assert (await cross.counters())["counts"].get("POST", 0) == 0
    await cross.separated(claim["turn"]["conversation_id"], 2)
