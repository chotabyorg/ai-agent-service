"""Cross-service Telegram Handoff behavior over real local HTTP and PostgreSQL."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import text

from tests.goal3_business_cross_service.fake_provider import completion, tool_call


pytestmark = pytest.mark.asyncio(loop_scope="session")

BOT_ID = 731005
OWNER_COOKIE_A = "access_token=owner-a.payload.signature"
OWNER_COOKIE_B = "access_token=owner-b.payload.signature"
FOREIGN_COOKIE = "access_token=foreign.payload.signature"
PRIVATE_TIMEOUT = 30


async def _scalar(engine, statement: str, values: dict | None = None):
    async with engine.connect() as connection:
        return await connection.scalar(text(statement), values or {})


async def _rows(engine, statement: str, values: dict | None = None):
    async with engine.connect() as connection:
        result = await connection.execute(text(statement), values or {})
        return [dict(row) for row in result.mappings().all()]


async def _wait_for(read, predicate, *, timeout: float = PRIVATE_TIMEOUT):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = await read()
        if predicate(value):
            return value
        await asyncio.sleep(0.04)
    raise AssertionError("Owned process-backed acceptance condition timed out")


def _telegram_update(update_id: int, chat_id: int, message_id: int, value: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": 1,
            "chat": {"id": chat_id, "type": "private", "first_name": "Customer"},
            "from": {
                "id": chat_id,
                "is_bot": False,
                "first_name": "Customer",
                "language_code": "ru",
            },
            "text": value,
        },
    }


def _admission_request(handoff, *, update_id: int, chat_id: int,
                       message_id: int, value: str) -> dict:
    return {
        "owner_id": handoff.grant["owner_id"],
        "integration_id": handoff.ids["integration"],
        "agent_id": handoff.ids["agent"],
        "provider_thread_id": str(chat_id),
        "provider_event_id": f"b{BOT_ID}c{chat_id}m{message_id}",
        "update_id": update_id,
        "chat_id": chat_id,
        "sender_id": chat_id,
        "message_id": message_id,
        "bot_id": BOT_ID,
        "text": value,
        "desired_route": "MANAGED",
    }


async def _admission(handoff, update_id: int):
    return await _wait_for(
        lambda: _rows(
            handoff.engines["conversation"],
            "SELECT id,conversation_id,canonical_message_id,route "
            "FROM telegram_admissions WHERE update_id=:update_id",
            {"update_id": update_id},
        ),
        lambda value: bool(value),
    )


async def _turn(handoff, message_id: str):
    return await _wait_for(
        lambda: _rows(
            handoff.engines["conversation"],
            "SELECT t.id,t.status,t.outcome,t.draft_text,t.input_first_sequence,"
            "t.input_last_sequence,t.authority_revision,t.managed_contract_version "
            "FROM telegram_immediate_turns i JOIN agent_turns t ON t.id=i.turn_id "
            "WHERE i.message_id=:message_id",
            {"message_id": message_id},
        ),
        lambda value: bool(value) and value[0]["status"] in {"COMPLETED", "FAILED"},
    )


def _browser_headers(cookie: str, *, write: bool = False) -> dict[str, str]:
    result = {"Cookie": cookie}
    if write:
        result["Origin"] = "https://agents.chota.by"
    return result


async def _case(handoff, conversation_id: str):
    return await _wait_for(
        lambda: _rows(
            handoff.engines["conversation"],
            "SELECT id,status,case_revision,authority_revision,assignment_revision,created_at "
            "FROM handoff_cases WHERE conversation_id=:conversation_id",
            {"conversation_id": conversation_id},
        ),
        lambda value: bool(value),
    )


async def _send_human(handoff, case: dict, cookie: str, command_id: str, value: str):
    case_id = case.get("id", case.get("case_id"))
    assert isinstance(case_id, str)
    return await handoff.client.post(
        handoff.url("cs") + f"/public/v1/inbox/cases/{case_id}/messages",
        json={
            "command_id": command_id,
            "expected_authority_revision": case["authority_revision"],
            "expected_case_revision": case["case_revision"],
            "expected_assignment_revision": case["assignment_revision"],
            "text": value,
        },
        headers=_browser_headers(cookie, write=True),
    )


async def _wait_delivery(handoff, message_id: str):
    return await _wait_for(
        lambda: _rows(
            handoff.engines["conversation"],
            "SELECT status,writer_state,provider_message_id,error_code "
            "FROM delivery_attempts WHERE source_message_id=:message_id",
            {"message_id": message_id},
        ),
        lambda value: bool(value) and value[0]["status"] in {
            "PROVIDER_ACCEPTED", "UNKNOWN", "FAILED", "SUPERSEDED",
        } and value[0]["writer_state"] in {"ENDED", "RETIRED"},
    )


async def _wait_agent_delivery(handoff, turn_id: str):
    return await _wait_for(
        lambda: _rows(
            handoff.engines["conversation"],
            "SELECT d.id,d.source_turn_id,d.source_message_id,d.outbound_ordinal,d.case_id,"
            "d.grant_generation,d.status,d.writer_state,d.authority_revision,"
            "d.provider_message_id,d.error_code,c.control_mode,c.authority_revision "
            "AS current_authority_revision FROM delivery_attempts d "
            "JOIN conversations c ON c.id=d.conversation_id "
            "WHERE d.source_turn_id=:turn_id",
            {"turn_id": turn_id},
        ),
        lambda value: bool(value) and value[0]["status"] in {
            "PROVIDER_ACCEPTED", "UNKNOWN", "FAILED", "SUPERSEDED",
        } and value[0]["writer_state"] in {"ENDED", "RETIRED"},
    )


async def _managed_v3_evidence(handoff, request_body: dict) -> dict:
    from schemas.managed_turn_v3 import ManagedTurnV3Request
    from services.managed_execution_receipts import (
        managed_v3_dispatch_id,
        managed_v3_request_fingerprint,
    )

    request = ManagedTurnV3Request.model_validate(request_body)
    fingerprint = managed_v3_request_fingerprint(request)
    evidence_body = {
        "contract_version": 3,
        "owner_id": request.owner_id,
        "integration_id": str(request.integration_id),
        "channel": request.channel,
        "agent_id": str(request.agent_id),
        "conversation_id": str(request.conversation_id),
        "turn_id": str(request.turn_id),
        "input_revision": request.input_revision,
        "authority_revision": request.authority_revision,
        "input_first_sequence": request.input_first_sequence,
        "input_last_sequence": request.input_last_sequence,
        "context_last_sequence": request.context_last_sequence,
        "request_fingerprint": fingerprint,
        "original_dispatch_id": str(managed_v3_dispatch_id(request, fingerprint)),
    }
    response = await handoff.client.post(
        handoff.url("ai") + "/internal/v3/managed-turns/control-evidence",
        json=evidence_body,
        headers={"X-Service-Token": handoff.tokens["managed"]},
    )
    assert response.status_code == 200
    return response.json()


async def test_telegram_handoff_real_http_and_database_lifecycle(handoff):
    """Exercise enrollment, AI control, Inbox, ordered Telegram writes, and Return."""
    assert handoff.enrollment["active"] is True
    assert handoff.enrollment["integration_id"] == handoff.ids["integration"]
    assert "getUpdates" in handoff.telegram_peer.snapshot()["method_calls"]

    chat_id = 780000001
    first_update_id = 1001
    await handoff.script(model=[
        {"body": completion(None, [tool_call("business_propose", {"actions": [{
            "capability": "booking.confirm_create", "arguments": {"slot": "12:00"},
        }]})])},
        {"body": completion("Подтвердите запись на 12:00.")},
    ])
    handoff.telegram_peer.enqueue(_telegram_update(
        first_update_id, chat_id, 1, "Запиши меня на 12:00",
    ))
    first_admission = await _admission(handoff, first_update_id)
    first_turn = await _turn(handoff, str(first_admission[0]["canonical_message_id"]))
    assert first_turn[0]["managed_contract_version"] == 3
    assert first_turn[0]["draft_text"]
    assert first_turn[0]["input_first_sequence"] == first_turn[0]["input_last_sequence"] == 1
    assert first_turn[0]["status"] == "COMPLETED"
    origin_turn_id = str(first_turn[0]["id"])
    initial_delivery = await _wait_agent_delivery(handoff, origin_turn_id)
    assert len(initial_delivery) == 1
    initial_intent = initial_delivery[0]
    assert str(initial_intent["source_turn_id"]) == origin_turn_id
    assert initial_intent["source_message_id"] is None
    assert initial_intent["outbound_ordinal"] == 1
    assert initial_intent["case_id"] is None
    assert initial_intent["grant_generation"] == 1
    assert initial_intent["status"] == "PROVIDER_ACCEPTED"
    assert initial_intent["writer_state"] == "ENDED"
    assert initial_intent["authority_revision"] == initial_intent["current_authority_revision"] == 0
    assert initial_intent["control_mode"] == "AGENT"
    assert initial_intent["provider_message_id"]
    assert initial_intent["error_code"] is None
    first_agent_attempts = [
        item for item in handoff.telegram_peer.snapshot()["send_attempts"]
        if item["text"] == first_turn[0]["draft_text"]
    ]
    first_agent_accepted = [
        item for item in handoff.telegram_peer.snapshot()["accepted_messages"]
        if item["text"] == first_turn[0]["draft_text"]
    ]
    assert len(first_agent_attempts) == len(first_agent_accepted) == 1
    assert initial_intent["provider_message_id"] == str(first_agent_accepted[0]["message_id"])

    operations = await handoff.operations(origin_turn_id)
    assert len(operations) == 1 and operations[0]["status"] == "RESERVED"
    proposal_id = await _scalar(
        handoff.engines["ai"],
        "SELECT plan_id FROM business_operations WHERE id=:operation_id",
        {"operation_id": operations[0]["id"]},
    )
    assert proposal_id is not None
    assert (await handoff.provider_counts())[0].get("POST", 0) == 0

    second_update_id = first_update_id + 1
    model_calls_before_handoff = (await handoff.provider_counts())[0].get("MODEL", 0)
    stale_agent_text = first_turn[0]["draft_text"]
    await handoff.script(model=[
        {"body": completion(None, [tool_call("platform_request_human_handoff", {
            "reason": "CUSTOMER_REQUEST", "note": "Please connect a person.",
        })])},
    ])
    lost_call = handoff.provider_proxy.detach_next(
        "/internal/v3/managed-turns/execute",
    )
    handoff.telegram_peer.enqueue(_telegram_update(
        second_update_id, chat_id, 2, "Пожалуйста, подключите человека",
    ))
    second_admission = await _admission(handoff, second_update_id)
    await asyncio.wait_for(asyncio.to_thread(lost_call["started"].wait, PRIVATE_TIMEOUT),
                           timeout=PRIVATE_TIMEOUT + 1)
    await asyncio.wait_for(asyncio.to_thread(lost_call["done"].wait, PRIVATE_TIMEOUT),
                           timeout=PRIVATE_TIMEOUT + 1)
    assert lost_call["status"] == 200
    lost_call["caller_release"].set()
    conversation_id = str(second_admission[0]["conversation_id"])
    case_rows = await _case(handoff, conversation_id)
    assert case_rows[0]["status"] == "OPEN"
    case_id = str(case_rows[0]["id"])
    handoff_created_at = case_rows[0]["created_at"]
    if handoff_created_at.tzinfo is None:
        handoff_created_at = handoff_created_at.replace(tzinfo=UTC)

    turn_rows = await _turn(handoff, str(second_admission[0]["canonical_message_id"]))
    handoff_turn_id = str(turn_rows[0]["id"])
    assert turn_rows[0]["status"] == "COMPLETED"
    handoff_input_sequence = await _scalar(
        handoff.engines["conversation"],
        "SELECT sequence FROM messages WHERE id=:message_id",
        {"message_id": second_admission[0]["canonical_message_id"]},
    )
    assert turn_rows[0]["input_first_sequence"] == turn_rows[0]["input_last_sequence"]
    assert turn_rows[0]["input_first_sequence"] == handoff_input_sequence
    assert await _scalar(
        handoff.engines["conversation"],
        "SELECT control_mode FROM conversations WHERE id=:id", {"id": conversation_id},
    ) == "HUMAN"

    ai_calls = [item for item in handoff.provider_proxy.snapshot()
                if item["path"] == "/internal/v3/managed-turns/execute"]
    assert len(ai_calls) == 2
    handoff_request = json.loads(ai_calls[-1]["body"])
    evidence = await _managed_v3_evidence(handoff, handoff_request)
    assert evidence["evidence_kind"] == "CONTROL_REQUEST"
    assert evidence["control_request"]["request_id"]
    model_calls_after_handoff = (await handoff.provider_counts())[0].get("MODEL", 0)
    assert model_calls_after_handoff == model_calls_before_handoff + 1
    replay = await handoff.client.post(
        handoff.url("ai") + "/internal/v3/managed-turns/execute",
        json=handoff_request,
        headers={"X-Service-Token": handoff.tokens["managed"]},
    )
    assert replay.status_code == 200
    assert replay.json()["result_kind"] == "PLATFORM_CONTROL_REQUEST"
    assert (await handoff.provider_counts())[0].get("MODEL", 0) == model_calls_after_handoff
    assert await _scalar(
        handoff.engines["conversation"],
        "SELECT count(*) FROM handoff_cases WHERE conversation_id=:id",
        {"id": conversation_id},
    ) == 1

    list_url = handoff.url("cs") + "/public/v1/inbox/cases"
    owner_a = _browser_headers(OWNER_COOKIE_A)
    owner_b = _browser_headers(OWNER_COOKIE_B)
    listed = await handoff.client.get(list_url, headers=owner_a)
    assert listed.status_code == 200
    assert [item["case_id"] for item in listed.json()["items"]] == [case_id]
    detail = await handoff.client.get(
        handoff.url("cs") + f"/public/v1/inbox/cases/{case_id}", headers=owner_b,
    )
    assert detail.status_code == 200
    foreign = await handoff.client.get(
        handoff.url("cs") + f"/public/v1/inbox/cases/{case_id}",
        headers=_browser_headers(FOREIGN_COOKIE),
    )
    assert foreign.status_code == 404

    initial_case = detail.json()["case"]
    claim_path = f"/public/v1/inbox/cases/{case_id}/claim"
    commands = [
        {"command_id": str(uuid4()), "expected_authority_revision": initial_case["authority_revision"],
         "expected_case_revision": initial_case["case_revision"]},
        {"command_id": str(uuid4()), "expected_authority_revision": initial_case["authority_revision"],
         "expected_case_revision": initial_case["case_revision"]},
    ]
    claims = await asyncio.gather(*(
        handoff.client.post(handoff.url("cs") + claim_path, json=command,
                            headers=_browser_headers(cookie, write=True))
        for command, cookie in zip(commands, (OWNER_COOKIE_A, OWNER_COOKIE_B), strict=True)
    ))
    assert sorted(response.status_code for response in claims) == [200, 409]
    winner_index = next(index for index, response in enumerate(claims)
                        if response.status_code == 200)
    winner_cookie = (OWNER_COOKIE_A, OWNER_COOKIE_B)[winner_index]
    winner_command = commands[winner_index]
    claimed = claims[winner_index].json()
    assert claimed["receipt"]["replayed"] is False
    assert claimed["case"]["assigned_account_id"] == handoff.ids["agent_owner"]
    claim_replay = await handoff.client.post(
        handoff.url("cs") + claim_path,
        json=winner_command,
        headers=_browser_headers(winner_cookie, write=True),
    )
    assert claim_replay.status_code == 200
    assert claim_replay.json()["receipt"]["replayed"] is True

    active_case = claimed["case"]
    first_human_command = {
        "command_id": str(uuid4()),
        "expected_authority_revision": active_case["authority_revision"],
        "expected_case_revision": active_case["case_revision"],
        "expected_assignment_revision": active_case["assignment_revision"],
        "text": "Сейчас подключу коллегу.",
    }
    handoff.telegram_peer.arm_send("hold")
    first_human = await handoff.client.post(
        handoff.url("cs") + f"/public/v1/inbox/cases/{case_id}/messages",
        json=first_human_command,
        headers=_browser_headers(winner_cookie, write=True),
    )
    assert first_human.status_code == 200
    first_human_id = first_human.json()["receipt"]["message_id"]
    await asyncio.wait_for(asyncio.to_thread(handoff.telegram_peer.send_entered.wait,
                                             PRIVATE_TIMEOUT), timeout=PRIVATE_TIMEOUT + 1)

    second_human_case = first_human.json()["case"]
    second_human = await _send_human(
        handoff, second_human_case, winner_cookie, str(uuid4()),
        "Я остаюсь на связи.",
    )
    assert second_human.status_code == 200
    second_human_id = second_human.json()["receipt"]["message_id"]
    work = await handoff.client.post(
        handoff.url("cs") + "/internal/v2/telegram/delivery/work",
        json={"contract_version": 2, "owner_id": handoff.grant["owner_id"],
              "integration_id": handoff.ids["integration"], "agent_id": handoff.ids["agent"],
              "bot_id": BOT_ID},
        headers={"X-Service-Token": handoff.tokens["telegram_gateway"]},
    )
    assert work.status_code == 200
    assert not any(item["source_id"] == second_human_id
                   and item["work_kind"] == "SEND_CANDIDATE"
                   for item in work.json()["items"])

    handoff.telegram_peer.arm_send("unknown")
    handoff.telegram_peer.release_send()
    first_delivery = await _wait_delivery(handoff, first_human_id)
    second_delivery = await _wait_delivery(handoff, second_human_id)
    assert first_human_id != second_human_id
    assert first_delivery[0]["status"] == "PROVIDER_ACCEPTED", (
        f"first={first_delivery!r} second={second_delivery!r} "
        f"telegram_peer={handoff.telegram_peer.snapshot()!r}"
    )
    assert second_delivery[0]["status"] == "UNKNOWN"
    assert second_delivery[0]["writer_state"] == "ENDED"
    second_attempts = [item for item in handoff.telegram_peer.snapshot()["send_attempts"]
                       if item["text"] == "Я остаюсь на связи."]
    assert len(second_attempts) == 1

    replay_human = await handoff.client.post(
        handoff.url("cs") + f"/public/v1/inbox/cases/{case_id}/messages",
        json=first_human_command,
        headers=_browser_headers(winner_cookie, write=True),
    )
    assert replay_human.status_code == 200
    assert replay_human.json()["receipt"]["replayed"] is True
    assert replay_human.json()["receipt"]["message_id"] == first_human_id

    third_human_case = second_human.json()["case"]
    third_human = await _send_human(
        handoff, third_human_case, winner_cookie, str(uuid4()),
        "Спасибо, подожду.",
    )
    assert third_human.status_code == 200
    third_human_id = third_human.json()["receipt"]["message_id"]
    third_delivery = await _wait_delivery(handoff, third_human_id)
    assert third_delivery[0]["status"] == "PROVIDER_ACCEPTED"
    await asyncio.sleep(0.2)
    assert len([item for item in handoff.telegram_peer.snapshot()["send_attempts"]
                if item["text"] == "Я остаюсь на связи."]) == 1
    stale_agent_attempts_after_handoff = [
        item for item in handoff.telegram_peer.snapshot()["send_attempts"]
        if item["text"] == stale_agent_text
        and datetime.fromisoformat(item["request_started_at"]) > handoff_created_at
    ]
    assert stale_agent_attempts_after_handoff == []

    # A real IMMEDIATE customer admission in HUMAN is fenced with no Turn; a
    # following Telegram update must still advance the gateway's polling order.
    human_update_id = second_update_id + 1
    human_message_id = 3
    handoff.telegram_peer.enqueue(_telegram_update(
        human_update_id, chat_id, human_message_id, "Уточню вопрос позже",
    ))
    human_admission = await _admission(handoff, human_update_id)
    assert human_admission[0]["canonical_message_id"] is not None
    human_result = await handoff.client.post(
        handoff.url("cs") + "/internal/v1/telegram/results",
        json={"owner_id": handoff.grant["owner_id"],
              "integration_id": handoff.ids["integration"], "agent_id": handoff.ids["agent"],
              "provider_thread_id": str(chat_id),
              "provider_event_id": f"b{BOT_ID}c{chat_id}m{human_message_id}"},
        headers={"X-Service-Token": handoff.tokens["telegram_gateway"]},
    )
    assert human_result.status_code == 200
    assert human_result.json()["progress_disposition"] == "AUTHORITY_FENCED"
    assert human_result.json()["turn_id"] is None

    next_human_update_id = human_update_id + 1
    next_human_message_id = human_message_id + 1
    handoff.telegram_peer.enqueue(_telegram_update(
        next_human_update_id, chat_id, next_human_message_id, "Ещё одно сообщение",
    ))
    next_human_admission = await _admission(handoff, next_human_update_id)
    assert next_human_admission[0]["canonical_message_id"] is not None
    assert await _scalar(
        handoff.engines["conversation"],
        "SELECT count(*) FROM telegram_immediate_turns WHERE message_id IN "
        "(SELECT canonical_message_id FROM telegram_admissions WHERE update_id IN "
        "(:first,:second))",
        {"first": human_update_id, "second": next_human_update_id},
    ) == 0

    old_payload = _admission_request(
        handoff, update_id=human_update_id, chat_id=chat_id,
        message_id=human_message_id, value="Уточню вопрос позже",
    )
    returned_case = third_human.json()["case"]
    return_command = {
        "command_id": str(uuid4()),
        "expected_authority_revision": returned_case["authority_revision"],
        "expected_case_revision": returned_case["case_revision"],
    }
    returned = await handoff.client.post(
        handoff.url("cs") + f"/public/v1/inbox/cases/{case_id}/return-to-agent",
        json=return_command,
        headers=_browser_headers(winner_cookie, write=True),
    )
    assert returned.status_code == 200
    assert returned.json()["case"]["status"] == "RETURNED"
    assert await _scalar(
        handoff.engines["conversation"],
        "SELECT control_mode FROM conversations WHERE id=:id", {"id": conversation_id},
    ) == "AGENT"
    assert await _scalar(
        handoff.engines["conversation"],
        "SELECT handoff_resume_floor_sequence FROM conversations WHERE id=:id",
        {"id": conversation_id},
    ) == await _scalar(
        handoff.engines["conversation"],
        "SELECT last_message_sequence FROM conversations WHERE id=:id",
        {"id": conversation_id},
    )
    turn_count_after_return = await _scalar(
        handoff.engines["conversation"],
        "SELECT count(*) FROM agent_turns WHERE conversation_id=:id",
        {"id": conversation_id},
    )
    replay_old_admission = await handoff.client.post(
        handoff.url("cs") + "/internal/v1/telegram/admissions",
        json=old_payload,
        headers={"X-Service-Token": handoff.tokens["telegram_gateway"]},
    )
    assert replay_old_admission.status_code == 200
    assert replay_old_admission.json()["replayed"] is True
    assert await _scalar(
        handoff.engines["conversation"],
        "SELECT count(*) FROM agent_turns WHERE conversation_id=:id",
        {"id": conversation_id},
    ) == turn_count_after_return

    # The old proposal is considered only as data on the fresh post-Return
    # input. Its immutable origin cannot acquire a new permit in the new epoch.
    post_return_update_id = next_human_update_id + 1
    post_return_message_id = next_human_message_id + 1
    await handoff.script(model=[
        {"body": completion(None, [tool_call("business_decide_proposal", {
            "proposal_id": str(proposal_id), "intent": "CONFIRM",
        })])},
    ])
    handoff.telegram_peer.enqueue(_telegram_update(
        post_return_update_id, chat_id, post_return_message_id,
        "Да, подтверждаю старую запись",
    ))
    post_return_admission = await _admission(handoff, post_return_update_id)
    post_return_turn = await _turn(
        handoff, str(post_return_admission[0]["canonical_message_id"]),
    )
    assert post_return_turn[0]["status"] == "FAILED"
    stale_error = await _scalar(
        handoff.engines["ai"],
        "SELECT result_payload->>'error_code' FROM managed_execution_receipts "
        "WHERE turn_id=:turn_id",
        {"turn_id": post_return_turn[0]["id"]},
    )
    assert stale_error == "TOOL_SETUP_FAILED", f"closed-code actual={stale_error or 'NONE'}"
    assert int(post_return_turn[0]["input_first_sequence"]) > int(
        await _scalar(
            handoff.engines["conversation"],
            "SELECT handoff_resume_floor_sequence FROM conversations WHERE id=:id",
            {"id": conversation_id},
        )
    )
    old_operation = await _rows(
        handoff.engines["ai"],
        "SELECT o.status,o.attempt_count,o.effect_permit_id,p.proposal_state "
        "FROM business_operations o JOIN business_plans p ON p.id=o.plan_id "
        "WHERE p.id=:proposal_id",
        {"proposal_id": proposal_id},
    )
    assert old_operation and old_operation[0]["effect_permit_id"] is None
    assert old_operation[0]["attempt_count"] == 0
    assert await handoff.permit_rows() == []
    assert (await handoff.provider_counts())[0].get("POST", 0) == 0

    # A normal new action remains available after Return; it follows the
    # established business permit path without an extra confirmation turn.
    final_update_id = post_return_update_id + 1
    final_message_id = post_return_message_id + 1
    await handoff.script(model=[
        {"body": completion(None, [tool_call("business_execute_plan", {
            "actions": [{"capability": "booking.create", "arguments": {"slot": "14:00"}}],
        })])},
        {"body": completion("Запись выполнена.")},
    ])
    handoff.telegram_peer.enqueue(_telegram_update(
        final_update_id, chat_id, final_message_id, "Запиши меня на 14:00",
    ))
    final_admission = await _admission(handoff, final_update_id)
    final_turn = await _turn(handoff, str(final_admission[0]["canonical_message_id"]))
    assert final_turn[0]["status"] == "COMPLETED"
    assert final_turn[0]["outcome"] == "DRAFT_READY"
    final_customer_sequence = await _scalar(
        handoff.engines["conversation"],
        "SELECT sequence FROM messages WHERE id=:message_id",
        {"message_id": final_admission[0]["canonical_message_id"]},
    )
    assert final_turn[0]["input_first_sequence"] == final_turn[0]["input_last_sequence"]
    assert final_turn[0]["input_first_sequence"] == final_customer_sequence
    final_operations = await handoff.operations(str(final_turn[0]["id"]))
    assert len(final_operations) == 1 and final_operations[0]["status"] == "SUCCEEDED"
    assert final_operations[0]["effect_permit_id"] is not None
    assert await _wait_for(
        lambda: handoff.provider_counts(),
        lambda value: value[0].get("POST", 0) == 1 and value[1] == 1,
    )
    assert len(await handoff.permit_rows()) == 1
    final_delivery = await _wait_for(
        lambda: _rows(
            handoff.engines["conversation"],
            "SELECT status,writer_state FROM delivery_attempts WHERE source_turn_id=:turn_id",
            {"turn_id": final_turn[0]["id"]},
        ),
        lambda rows: bool(rows) and rows[0]["status"] == "PROVIDER_ACCEPTED"
        and rows[0]["writer_state"] == "ENDED",
    )
    assert final_delivery[0]["status"] == "PROVIDER_ACCEPTED"
    assert any(
        item["text"] == final_turn[0]["draft_text"] and item["mode"] == "accept"
        for item in handoff.telegram_peer.snapshot()["send_attempts"]
    )
    assert handoff_turn_id != origin_turn_id
