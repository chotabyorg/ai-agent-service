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
from business_capabilities.registry import DEFAULTS


def crm_test_settings():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return Settings(_env_file=None, business_capabilities_enabled=True,
                    business_crm_key_id="synthetic", business_crm_private_key=pem,
                    business_crm_url="http://synthetic.local")


async def install_managed_crm(api_client, db_session, monkeypatch, response_status, response_body,
                              *, recover_after_first=False):
    agent = await create_agent(api_client, db_session_factory=db_session)
    value = payload(agent["id"])
    owner_id = UUID(agent["user_id"])
    configure(monkeypatch, value, owner_id)

    connection_id, workspace_id, grant_id = uuid4(), uuid4(), uuid4()
    async with db_session() as db:
        db.add(BusinessConnection(
            id=connection_id, agent_id=UUID(agent["id"]), owner_id=owner_id,
            workspace_id=workspace_id, workspace_name="Synthetic salon",
            timezone="Europe/Minsk", grant_id=grant_id, grant_revision=1,
            generation=1, active=True, enabled_capabilities=list(DEFAULTS),
        ))
        await db.commit()

    settings = crm_test_settings()
    registry = create_business_registry(settings, db_session)
    calls = {"facts": 0, "execute": 0}
    context = {
        "workspace_id": str(workspace_id), "grant_id": str(grant_id),
        "revision": 1, "capabilities": list(DEFAULTS), "timezone": "Europe/Minsk",
    }

    async def handle(request):
        if request.url.path.endswith("/context"):
            return httpx.Response(200, json=context)
        if "/facts/" in request.url.path:
            calls["facts"] += 1
            if recover_after_first and calls["facts"] > 1:
                arguments = json.loads(request.content)["arguments"]
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
            return httpx.Response(response_status, json=response_body)
        if "/execute/" in request.url.path:
            calls["execute"] += 1
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
    assert result["draft_text"] == "Не удалось подобрать это время. Уточните другой вариант."
    tool_result = next(message for message in model.chat.completions.calls[1]["messages"]
                       if message.get("role") == "tool")
    assert json.loads(tool_result["content"]) == failure(error_code)
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0
    assert calls == {"facts": 1, "execute": 0}
    assert effect_port.permit_requests == []


@pytest.mark.integration
async def test_provider_refusal_can_be_corrected_into_prepared_proposal_without_dispatch(
    api_client, db_session, monkeypatch,
):
    value, arguments, _, calls, effect_port, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200,
        {"status": "ERROR", "error": {"code": "BOOKING_CONFLICT", "message": "synthetic", "retry": "NEVER"}},
        recover_after_first=True,
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
    assert calls == {"facts": 1, "execute": 0}
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
    assert calls == {"facts": 0, "execute": 0}
    assert effect_port.permit_requests == []
