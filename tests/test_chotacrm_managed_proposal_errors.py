"""Managed Website CRM proposals translate pre-reservation provider failures."""

from datetime import datetime, timedelta, timezone
import json
from uuid import UUID, uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select

import main
import services.llm_service as llm_module
from business_capabilities.context import failure
from business_capabilities.registration import create_business_registry
from core.config import Settings, get_settings
from models.business import BusinessOperation, BusinessPlan
from models.business_connection import BusinessConnection
from schemas.managed_turn import ManagedTurnRequest, OperationReference
from services.business_runtime import BusinessRuntime
from tests.business_effect_helpers import FakeConversationEffectPort, open_business_runtime
from tests.fakes import ScriptedLLMClient, build_llm_response, build_tool_call
from tests.test_chat_flow import create_agent
from tests.test_managed_turns import configure, execute, payload
from tests.test_business_runtime import later
from business_capabilities.registry import DEFAULTS


def crm_test_settings():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return Settings(_env_file=None, business_capabilities_enabled=True,
                    business_crm_key_id="synthetic", business_crm_private_key=pem,
                    business_crm_url="http://synthetic.local")


def existing_customer_lookup(*, name="Влад", ref="customer_existing", archived=False):
    return {"status": "OK", "data": {"match": "SINGLE", "candidates": [{
        "ref": ref, "display_name": name, "archived": archived,
        "identities": [{"kind": "PHONE", "value": "+375291112233"}],
    }]}}


def preparation_arguments(tool_name, booking_arguments):
    if tool_name == "booking_create":
        return booking_arguments
    return {"actions": [{"capability": "booking.create", "arguments": booking_arguments}]}


@pytest.mark.integration
@pytest.mark.parametrize("tool_name", ["booking_create", "business_propose"])
@pytest.mark.parametrize("customer_name,candidate_name", [
    ("Влад", "Влад"), ("Влад", "вЛаД"),
    ("Артем", "Артём"), ("Артём", "Артем"),
    ("Артем", "АРТЁМ"), ("Артём", "Арте\u0308м"),
])
async def test_existing_customer_requires_explicit_reference_then_confirms_once(
    api_client, db_session, monkeypatch, tool_name, customer_name, candidate_name,
):
    value, arguments, registry, calls, effect_port, _ = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {},
        lookup_response=existing_customer_lookup(name=candidate_name),
        allow_execute=True,
    )
    arguments["customer"]["display_name"] = customer_name
    request = ManagedTurnRequest.model_validate(value)
    grant = next(grant for grant in get_settings().managed_principals[0].grants
                 if grant.scope() == (value["owner_id"], UUID(value["integration_id"]),
                                      value["channel"], UUID(value["agent_id"])))
    runtime = await open_business_runtime(request, grant, db_session, registry, effect_port=effect_port)
    await runtime.prompt_context()

    guidance = await runtime.call_tool(tool_name, preparation_arguments(tool_name, arguments))
    assert guidance.tool_payload == {
        "status": "CUSTOMER_REUSE_REQUIRED", "customer_ref": "customer_existing",
        "instruction": "Call the booking tool again with this customer_ref and omit customer.",
    }
    assert calls["lookup"] == 1 and calls["facts"] == calls["execute"] == 0
    assert calls["lookup_arguments"] == [{
        "limit": 10, "identity": {"kind": "PHONE", "value": "+375291112233"},
    }]
    assert effect_port.permit_requests == []
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0

    reference_arguments = {key: val for key, val in arguments.items() if key != "customer"}
    reference_arguments["customer_ref"] = "customer_existing"
    proposed = await runtime.call_tool(tool_name,
        preparation_arguments(tool_name, reference_arguments))
    assert proposed.safe_draft and "Окрашивание" in proposed.safe_draft
    assert calls["lookup"] == 1 and calls["facts"] >= 1 and calls["execute"] == 0
    assert calls["fact_arguments"] and all(
        value.get("customer_ref") == "customer_existing" and "customer" not in value
        for value in calls["fact_arguments"]
    )
    async with db_session() as db:
        plans = (await db.scalars(select(BusinessPlan))).all()
        operations = (await db.scalars(select(BusinessOperation))).all()
        assert len(plans) == len(operations) == 1
        assert operations[0].request_payload["arguments"]["customer_ref"] == "customer_existing"
        assert "customer" not in operations[0].request_payload["arguments"]

    later_runtime = await open_business_runtime(later(request, "Да, подтверждаю."), grant,
                                                db_session, registry, effect_port=effect_port)
    await later_runtime.prompt_context()
    confirmed = await later_runtime.call_tool("business_decide_proposal", {
        "proposal_id": proposed.tool_payload["proposal_id"], "intent": "CONFIRM",
    })
    assert confirmed.halt_status is None and calls["execute"] == 1
    assert effect_port.permit_requests
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 1
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 1


@pytest.mark.integration
@pytest.mark.parametrize("tool_name", ["booking_create", "business_propose"])
@pytest.mark.parametrize("customer_name,lookup", [
    ("Влад", existing_customer_lookup(name="Different Customer")),
    ("Артем", existing_customer_lookup(name="Антон")),
    ("Артем", existing_customer_lookup(name="Артём Иванов")),
    ("Артем", existing_customer_lookup(name="Aртем")),
    ("Артем", existing_customer_lookup(name="Артём", archived=True)),
    ("Jose", existing_customer_lookup(name="José")),
    ("Влад", {"status": "AMBIGUOUS", "data": {"match": "AMBIGUOUS", "candidates": []}}),
])
async def test_conflicting_or_ambiguous_customer_returns_private_safe_clarification(
    api_client, db_session, monkeypatch, tool_name, customer_name, lookup,
):
    value, arguments, registry, calls, effect_port, _model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, lookup_response=lookup,
    )
    arguments["customer"]["display_name"] = customer_name
    request = ManagedTurnRequest.model_validate(value)
    grant = next(grant for grant in get_settings().managed_principals[0].grants
                 if grant.scope() == (value["owner_id"], UUID(value["integration_id"]),
                                      value["channel"], UUID(value["agent_id"])))
    runtime = await open_business_runtime(request, grant, db_session, registry, effect_port=effect_port)
    await runtime.prompt_context()

    result = await runtime.call_tool(tool_name, preparation_arguments(tool_name, arguments))
    assert result.safe_draft and result.halt_status is None
    assert "customer_existing" not in repr(result)
    assert "Different Customer" not in repr(result)
    assert calls["lookup"] == 1 and calls["facts"] == calls["execute"] == 0
    assert effect_port.permit_requests == []
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0


@pytest.mark.integration
@pytest.mark.parametrize("tool_name", ["booking_create", "business_propose"])
@pytest.mark.parametrize("lookup_response,enabled_capabilities", [
    ({"status": "ERROR", "error": {"code": "PROVIDER_UNAVAILABLE",
                                      "message": "private provider detail", "retry": "SAFE"}}, list(DEFAULTS)),
    ({"status": "OK", "data": {"match": "SINGLE", "candidates": []}}, list(DEFAULTS)),
    (None, [capability for capability in DEFAULTS if capability != "customer.find"]),
])
async def test_unavailable_invalid_or_disabled_lookup_fails_closed(
    api_client, db_session, monkeypatch, tool_name, lookup_response, enabled_capabilities,
):
    value, arguments, registry, calls, effect_port, _model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, lookup_response=lookup_response,
        enabled_capabilities=enabled_capabilities,
    )
    request = ManagedTurnRequest.model_validate(value)
    grant = next(grant for grant in get_settings().managed_principals[0].grants
                 if grant.scope() == (value["owner_id"], UUID(value["integration_id"]),
                                      value["channel"], UUID(value["agent_id"])))
    runtime = await open_business_runtime(request, grant, db_session, registry, effect_port=effect_port)
    await runtime.prompt_context()

    result = await runtime.call_tool(tool_name, preparation_arguments(tool_name, arguments))
    assert result.safe_draft and result.halt_status is None
    assert "private provider detail" not in repr(result)
    expected_lookups = 0 if "customer.find" not in enabled_capabilities else 1
    assert calls["lookup"] == expected_lookups and calls["facts"] == calls["execute"] == 0
    assert effect_port.permit_requests == []
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0


@pytest.mark.integration
@pytest.mark.parametrize("lookup", [
    {"status": "ERROR", "error": {"code": "PROVIDER_UNAVAILABLE",
                                      "message": "private provider detail", "retry": "NEVER"}},
    {"status": "OK", "data": {"match": "SINGLE", "candidates": []}},
    existing_customer_lookup(name="Different Customer"),
])
async def test_preflight_never_returns_ordinary_feedback_over_retained_success_refs(
    api_client, db_session, monkeypatch, lookup,
):
    value, arguments, registry, calls, effect_port, _model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, lookup_response=lookup,
    )
    request = ManagedTurnRequest.model_validate(value)
    grant = next(grant for grant in get_settings().managed_principals[0].grants
                 if grant.scope() == (value["owner_id"], UUID(value["integration_id"]),
                                      value["channel"], UUID(value["agent_id"])))
    runtime = await open_business_runtime(request, grant, db_session, registry, effect_port=effect_port)
    await runtime.prompt_context()
    reference = OperationReference(origin="AI_AGENT_SERVICE", operation_id="retained-success",
                                   outcome="SUCCEEDED")
    guard_calls = 0
    snapshot_calls = 0

    async def open_guard(**_kwargs):
        nonlocal guard_calls
        guard_calls += 1
        return None

    def refs_after_lookup():
        nonlocal snapshot_calls
        snapshot_calls += 1
        return [] if snapshot_calls == 1 else [reference]

    monkeypatch.setattr(runtime, "execution_guard", open_guard)
    monkeypatch.setattr(runtime.collector, "snapshot", refs_after_lookup)
    result = await runtime.call_tool("business_propose", {
        "actions": [{"capability": "booking.create", "arguments": arguments}],
    })

    assert guard_calls >= 2 and calls["lookup"] == 1
    assert result.halt_status == "UNKNOWN" and result.safe_draft is None
    assert calls["facts"] == calls["execute"] == 0 and effect_port.permit_requests == []


@pytest.mark.integration
async def test_fact_refusal_with_newly_retained_success_ref_remains_unknown(
    api_client, db_session, monkeypatch,
):
    value, arguments, registry, calls, effect_port, _model = await install_managed_crm(
        api_client, db_session, monkeypatch, 503, {},
    )
    request = ManagedTurnRequest.model_validate(value)
    grant = next(grant for grant in get_settings().managed_principals[0].grants
                 if grant.scope() == (value["owner_id"], UUID(value["integration_id"]),
                                      value["channel"], UUID(value["agent_id"])))
    runtime = await open_business_runtime(request, grant, db_session, registry, effect_port=effect_port)
    await runtime.prompt_context()
    reference = OperationReference(origin="AI_AGENT_SERVICE", operation_id="retained-success",
                                   outcome="SUCCEEDED")
    snapshot_calls = 0

    async def open_guard(**_kwargs):
        return None

    def refs_at_fact_refusal():
        nonlocal snapshot_calls
        snapshot_calls += 1
        return [] if snapshot_calls <= 3 else [reference]

    monkeypatch.setattr(runtime, "execution_guard", open_guard)
    monkeypatch.setattr(runtime.collector, "snapshot", refs_at_fact_refusal)
    result = await runtime.call_tool("business_propose", {
        "actions": [{"capability": "booking.create", "arguments": arguments}],
    })

    assert calls["lookup"] == 1 and calls["facts"] == 1 and calls["execute"] == 0
    assert result.halt_status == "UNKNOWN" and result.safe_draft is None
    assert effect_port.permit_requests == []


async def install_managed_crm(api_client, db_session, monkeypatch, response_status, response_body,
                              *, recover_after_first=False, lookup_response=None,
                              enabled_capabilities=None, allow_execute=False,
                              channel="WEBSITE_WIDGET"):
    agent = await create_agent(api_client, db_session_factory=db_session)
    value = payload(agent["id"])
    value["channel"] = channel
    owner_id = UUID(agent["user_id"])
    configure(monkeypatch, value, owner_id)

    connection_id, workspace_id, grant_id = uuid4(), uuid4(), uuid4()
    enabled_capabilities = list(enabled_capabilities or DEFAULTS)
    async with db_session() as db:
        db.add(BusinessConnection(
            id=connection_id, agent_id=UUID(agent["id"]), owner_id=owner_id,
            workspace_id=workspace_id, workspace_name="Synthetic salon",
            timezone="Europe/Minsk", grant_id=grant_id, grant_revision=1,
            generation=1, active=True, enabled_capabilities=enabled_capabilities,
        ))
        await db.commit()

    settings = crm_test_settings()
    registry = create_business_registry(settings, db_session)
    calls = {"lookup": 0, "facts": 0, "execute": 0,
             "lookup_arguments": [], "fact_arguments": []}
    context = {
        "workspace_id": str(workspace_id), "grant_id": str(grant_id),
        "revision": 1, "capabilities": enabled_capabilities, "timezone": "Europe/Minsk",
    }

    async def handle(request):
        if request.url.path.endswith("/context"):
            return httpx.Response(200, json=context)
        if request.url.path.endswith("/execute/customer.find"):
            calls["lookup"] += 1
            calls["lookup_arguments"].append(json.loads(request.content)["arguments"])
            body = lookup_response or {"status": "OK", "data": {"match": "NONE", "candidates": []}}
            return httpx.Response(200, json=body)
        if request.url.path.endswith("/execute/booking.check_availability"):
            arguments = json.loads(request.content)["arguments"]
            start = datetime.fromisoformat(arguments["start_at"].replace("Z", "+00:00"))
            return httpx.Response(200, json={"status": "OK", "data": {
                "available": True, "start_at": start.isoformat(),
                "end_at": (start + timedelta(hours=1)).isoformat(),
                "checked_at": datetime.now(timezone.utc).isoformat(), "reservation": False,
            }})
        if "/facts/" in request.url.path:
            calls["facts"] += 1
            fact_arguments = json.loads(request.content)["arguments"]
            calls["fact_arguments"].append(fact_arguments)
            if recover_after_first and calls["facts"] > 1:
                arguments = fact_arguments
                start = datetime.fromisoformat(arguments["start_at"].replace("Z", "+00:00"))
                return httpx.Response(200, json={
                    "status": "OK",
                    "data": {
                        "arguments": arguments,
                        "payload_hash": "a" * 64,
                        "facts_hash": "b" * 64,
                        "summary": {
                            "customer": "Влад", "service": "Окрашивание", "resource": "Анна",
                            "start_at": start.isoformat(),
                            "end_at": (start + timedelta(hours=1)).isoformat(),
                            "timezone": "Europe/Minsk", "price": "50.00", "currency": "BYN",
                        },
                    },
                })
            if response_status == 200 and not response_body:
                start = datetime.fromisoformat(fact_arguments["start_at"].replace("Z", "+00:00"))
                return httpx.Response(200, json={
                    "status": "OK",
                    "data": {
                        "arguments": fact_arguments,
                        "payload_hash": "a" * 64,
                        "facts_hash": "b" * 64,
                        "summary": {
                            "customer": "Влад", "service": "Окрашивание", "resource": "Анна",
                            "start_at": start.isoformat(),
                            "end_at": (start + timedelta(hours=1)).isoformat(),
                            "timezone": "Europe/Minsk", "price": "50.00", "currency": "BYN",
                        },
                    },
                })
            return httpx.Response(response_status, json=response_body)
        if "/execute/" in request.url.path:
            calls["execute"] += 1
            if allow_execute and request.url.path.endswith("/execute/booking.create"):
                body = json.loads(request.content)
                action = body["arguments"]
                start = datetime.fromisoformat(action["start_at"].replace("Z", "+00:00"))
                return httpx.Response(200, json={
                    "status": "OK", "operation_ref": "synthetic_receipt",
                    "operation_id": body["operation"]["id"], "effect": "APPLIED",
                    "data": {
                        "ref": "booking_test", "customer_ref": action.get("customer_ref", "customer_new"),
                        "offering_ref": action["offering_ref"], "resource_ref": action["resource_ref"],
                        "offering_name": "Окрашивание", "resource_name": "Анна",
                        "timezone": "Europe/Minsk", "start_at": start.isoformat(),
                        "end_at": (start + timedelta(hours=1)).isoformat(),
                        "status": "CONFIRMED", "revision": "1",
                    },
                })
            raise AssertionError("facts refusal must happen before provider execute")
        raise AssertionError(request.url.path)

    registry.get("chotacrm").adapter.client.transport = httpx.MockTransport(handle)
    monkeypatch.setattr(main.app.state, "business_registry", registry)
    effect_port = FakeConversationEffectPort()
    original_open = BusinessRuntime.open.__func__

    async def open_with_test_authority(cls, *args, **kwargs):
        kwargs["effect_port"] = effect_port
        return await original_open(cls, *args, **kwargs)

    monkeypatch.setattr(BusinessRuntime, "open", classmethod(open_with_test_authority))
    model = ScriptedLLMClient()
    monkeypatch.setattr(llm_module, "AsyncOpenAI", lambda **kwargs: model)

    start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(
        hour=15, minute=0, second=0, microsecond=0,
    ).isoformat()
    arguments = {
        "resource_ref": "resource_test", "offering_ref": "offering_test",
        "start_at": start,
        "customer": {"display_name": "Влад", "identity": {"kind": "PHONE", "value": "+375291112233"}},
    }
    if channel == "VOICE":
        # The scripted booking uses facts actually supplied by the caller.
        value["messages"][0]["text"] = "Запишите меня. Меня зовут Влад, телефон +375291112233."
    return value, arguments, registry, calls, effect_port, model


@pytest.mark.integration
@pytest.mark.parametrize(("status", "body", "error_code"), [
    (200, {"status": "ERROR", "error": {"code": "BOOKING_CONFLICT", "message": "synthetic", "retry": "NEVER"}}, "BOOKING_CONFLICT"),
    (200, {"status": "ERROR", "error": {"code": "STATE_CONFLICT", "message": "synthetic", "retry": "NEVER"}}, "STATE_CONFLICT"),
    (200, {"status": "ERROR", "error": {"code": "NOT_FOUND", "message": "synthetic", "retry": "NEVER"}}, "NOT_FOUND"),
    (403, {}, "PERMISSION_DENIED"),
    (503, {}, "PROVIDER_UNAVAILABLE"),
])
async def test_managed_proposal_provider_refusal_is_recoverable_before_reservation(
    api_client, db_session, monkeypatch, status, body, error_code,
):
    value, arguments, _, calls, effect_port, model = await install_managed_crm(
        api_client, db_session, monkeypatch, status, body,
        enabled_capabilities=[*DEFAULTS, 'deal.create'],
    )
    action = {"capability": "booking.create", "arguments": arguments}
    model.chat.completions.queue(
        build_llm_response(tool_calls=[build_tool_call(
            "proposal", "business_propose", json.dumps({"actions": [action]}),
        )]),
        build_llm_response(content="Не удалось подобрать это время. Уточните другой вариант."),
    )

    response = await execute(api_client, value)
    result = response.json()
    assert response.status_code == 200
    assert result["status"] == "DRAFT_READY"
    # A provider refusal cannot leave the final wording to untrusted model
    # prose. Keep the typed feedback and the no-effect/recovery checks below.
    assert result["draft_text"] == (
        "Не удалось подготовить предложение для подтверждения. "
        "Действие не выполнялось. Давайте уточним детали."
    )
    tool_result = next(message for message in model.chat.completions.calls[1]["messages"]
                       if message.get("role") == "tool")
    assert json.loads(tool_result["content"]) == failure(error_code)
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0
    assert calls["lookup"] == 1 and calls["facts"] == 1 and calls["execute"] == 0
    assert calls["lookup_arguments"] == [{
        "limit": 10, "identity": {"kind": "PHONE", "value": "+375291112233"},
    }]
    assert effect_port.permit_requests == []


@pytest.mark.integration
async def test_provider_refusal_can_be_corrected_into_prepared_proposal_without_dispatch(
    api_client, db_session, monkeypatch,
):
    value, arguments, _, calls, effect_port, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200,
        {"status": "ERROR", "error": {"code": "BOOKING_CONFLICT", "message": "synthetic", "retry": "NEVER"}},
        recover_after_first=True, enabled_capabilities=[*DEFAULTS, 'deal.create'],
    )
    corrected = dict(arguments)
    corrected["start_at"] = (datetime.fromisoformat(arguments["start_at"]) + timedelta(hours=1)).isoformat()
    model.chat.completions.queue(
        build_llm_response(tool_calls=[build_tool_call("rejected", "business_propose", json.dumps({
            "actions": [{"capability": "booking.create", "arguments": arguments}],
        }))]),
        build_llm_response(tool_calls=[build_tool_call("corrected", "business_propose", json.dumps({
            "actions": [{"capability": "booking.create", "arguments": corrected}],
        }))]),
    )

    response = await execute(api_client, value)
    result = response.json()
    assert response.status_code == 200 and result["status"] == "DRAFT_READY"
    assert result["draft_text"] and "19:00" in result["draft_text"]
    failed_feedback = next(message for message in model.chat.completions.calls[1]["messages"]
                           if message.get("role") == "tool")
    assert json.loads(failed_feedback["content"]) == failure("BOOKING_CONFLICT")
    async with db_session() as db:
        plans = (await db.scalars(select(BusinessPlan))).all()
        operations = (await db.scalars(select(BusinessOperation))).all()
        assert len(plans) == 1 and plans[0].proposal_state == "PREPARED"
        assert len(operations) == 1 and operations[0].status == "RESERVED"
    assert calls["facts"] >= 2 and calls["execute"] == 0
    assert effect_port.permit_requests == []


@pytest.mark.integration
async def test_managed_proposal_provider_refusal_with_retained_evidence_is_unknown(
    api_client, db_session, monkeypatch,
):
    value, arguments, registry, calls, effect_port, _ = await install_managed_crm(
        api_client, db_session, monkeypatch, 200,
        {"status": "ERROR", "error": {"code": "BOOKING_CONFLICT", "message": "synthetic", "retry": "NEVER"}},
    )
    request = ManagedTurnRequest.model_validate(value)
    grant = next(grant for grant in get_settings().managed_principals[0].grants
                 if grant.scope() == (value["owner_id"], UUID(value["integration_id"]),
                                      value["channel"], UUID(value["agent_id"])))
    runtime = await open_business_runtime(request, grant, db_session, registry, effect_port=effect_port)
    await runtime.prompt_context()
    runtime.collector.observe_committed([
        OperationReference(origin="AI_AGENT_SERVICE", operation_id="retained-operation", outcome="SUCCEEDED"),
    ])

    result = await runtime.call_tool("business_propose", {
        "actions": [{"capability": "booking.create", "arguments": arguments}],
    })
    assert result.halt_status == "UNKNOWN" and result.error_code == "TOOL_EXECUTION_UNKNOWN"
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0
    assert calls == {"lookup": 0, "facts": 0, "execute": 0,
                     "lookup_arguments": [], "fact_arguments": []}
    assert effect_port.permit_requests == []


@pytest.mark.integration
async def test_managed_proposal_rejects_extra_top_level_fields_before_provider_call(
    api_client, db_session, monkeypatch,
):
    value, arguments, registry, calls, effect_port, _ = await install_managed_crm(
        api_client, db_session, monkeypatch, 200,
        {"status": "ERROR", "error": {"code": "BOOKING_CONFLICT", "message": "synthetic", "retry": "NEVER"}},
    )
    request = ManagedTurnRequest.model_validate(value)
    grant = next(grant for grant in get_settings().managed_principals[0].grants
                 if grant.scope() == (value["owner_id"], UUID(value["integration_id"]),
                                      value["channel"], UUID(value["agent_id"])))
    runtime = await open_business_runtime(request, grant, db_session, registry, effect_port=effect_port)
    await runtime.prompt_context()

    result = await runtime.call_tool("business_propose", {
        "actions": [{"capability": "booking.create", "arguments": arguments}],
        "provider_idempotency_key": "must-not-be-accepted",
    })

    assert result.halt_status == "FAILED" and result.error_code == "TOOL_SETUP_FAILED"
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0
    assert calls == {"lookup": 0, "facts": 0, "execute": 0,
                     "lookup_arguments": [], "fact_arguments": []}
    assert effect_port.permit_requests == []
