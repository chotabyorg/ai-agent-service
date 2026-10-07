"""Exact bounded authenticated read-only evidence surface, with real ledger rows."""

import copy
import json
from uuid import uuid4

import pytest
from sqlalchemy import select

import main
from core.config import get_settings
from core.managed_boundary import EVIDENCE_PATH, ManagedTurnBoundary
from models.business import BusinessOperation, BusinessPlan, BusinessScope, BusinessTurnContext, BusinessTurnPlanLink
from schemas.managed_turn import ManagedTurnRequest
from services.business_runtime import BusinessRuntime
from tests.fakes import build_llm_response, build_tool_call
from tests.test_managed_business_fakes import install_business, plan_arguments
from tests.test_managed_turns import TOKEN, configure, execute, model, payload, setup, tools


def evidence_value(value):
    return {key: value[key] for key in ("contract_version", "owner_id", "integration_id", "channel",
                                        "agent_id", "conversation_id", "turn_id")}


async def evidence(api_client, value):
    return await api_client.post(EVIDENCE_PATH, json=evidence_value(value), headers={"X-Service-Token": TOKEN})


async def retained_rows(factory):
    result = {}
    async with factory() as db:
        for model_type in (BusinessOperation, BusinessPlan, BusinessScope, BusinessTurnContext, BusinessTurnPlanLink):
            rows = (await db.scalars(select(model_type))).all()
            result[model_type.__name__] = sorted(
                [tuple((column.name, repr(getattr(row, column.name))) for column in model_type.__table__.columns)
                 for row in rows], key=repr)
    return result


@pytest.mark.integration
@pytest.mark.parametrize("mode", ["success", "unknown", "proposal"])
async def test_evidence_is_pure_retained_read_with_zero_execution_or_provider_work(
        api_client, db_session, monkeypatch, model, tools, mode):
    _, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session,
                                      confirmation=mode == "proposal", mode=mode)
    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call("plan", "business_execute_plan", plan_arguments())]))
    if mode == "success":
        model.chat.completions.queue(build_llm_response(content="done"))
    executed = (await execute(api_client, value)).json()
    assert len(executed["operation_refs"]) == 1
    before = await retained_rows(db_session)
    model_calls, provider_calls, fact_calls = len(model.chat.completions.calls), len(provider.commands), provider.fact_calls
    async def forbidden(*args, **kwargs):
        raise AssertionError("Evidence entered execution/recovery")
    for name in ("open", "recover_attempts", "reconcile", "execute_plan", "select_recovery_plan"):
        monkeypatch.setattr(BusinessRuntime, name, forbidden)
    response = await evidence(api_client, value)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {"contract_version": 1, "conversation_id": value["conversation_id"],
        "turn_id": value["turn_id"], "agent_id": value["agent_id"], "operation_refs": executed["operation_refs"]}
    assert await retained_rows(db_session) == before
    assert (len(model.chat.completions.calls), len(provider.commands), provider.fact_calls) == (model_calls, provider_calls, fact_calls)
    assert tools.init_calls == tools.acquisition_calls == tools.metadata_calls == tools.calls == []


@pytest.mark.integration
async def test_retained_evidence_survives_live_agent_deletion_but_execute_stays_404(
        api_client, db_session, monkeypatch, model):
    agent, value = await setup(api_client, db_session, monkeypatch)
    provider = await install_business(monkeypatch, value, db_session)
    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call("effect", "business_execute_plan", plan_arguments())]), build_llm_response(content="done"))
    executed = (await execute(api_client, value)).json()
    deleted = await api_client.delete("/v1/agents/" + agent["id"], headers={"X-User-Id": agent["user_id"]})
    assert deleted.status_code == 204
    assert (await evidence(api_client, value)).json()["operation_refs"] == executed["operation_refs"]
    assert (await execute(api_client, value)).status_code == 404
    assert len(provider.commands) == 1 and len(model.chat.completions.calls) == 2


@pytest.mark.integration
async def test_existing_turn_without_plan_has_empty_evidence_missing_turn_is_safe_404(
        api_client, db_session, monkeypatch, model):
    _, value = await setup(api_client, db_session, monkeypatch)
    await install_business(monkeypatch, value, db_session)
    request = ManagedTurnRequest.model_validate(value)
    await BusinessRuntime.open(request, get_settings().managed_principals[0].grants[0], db_session,
                               main.app.state.business_registry)
    response = await evidence(api_client, value)
    assert response.status_code == 200 and response.json()["operation_refs"] == []
    unknown = {**value, "turn_id": str(uuid4())}
    response = await evidence(api_client, unknown)
    assert response.status_code == 404 and response.json()["detail"]["code"] == "AGENT_NOT_FOUND"
    assert response.headers["cache-control"] == "no-store" and model.chat.completions.calls == []


@pytest.mark.integration
@pytest.mark.parametrize("field", ["owner_id", "integration_id", "channel", "agent_id", "conversation_id", "turn_id"])
async def test_full_scope_and_identity_prevent_guessed_evidence(
        api_client, db_session, monkeypatch, model, field):
    _, value = await setup(api_client, db_session, monkeypatch)
    await install_business(monkeypatch, value, db_session)
    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call("effect", "business_execute_plan", plan_arguments())]), build_llm_response(content="done"))
    await execute(api_client, value)
    foreign = copy.deepcopy(value)
    foreign[field] = "another-owner" if field == "owner_id" else "VOICE" if field == "channel" else str(uuid4())
    response = await evidence(api_client, foreign)
    assert response.status_code == 404 and response.headers["cache-control"] == "no-store"
    assert "operation_refs" not in response.json()


@pytest.mark.integration
async def test_retained_evidence_includes_the_complete_agent_owner_mapping(
        api_client, db_session, monkeypatch, model):
    _, value = await setup(api_client, db_session, monkeypatch)
    await install_business(monkeypatch, value, db_session)
    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call("effect", "business_execute_plan", plan_arguments())]), build_llm_response(content="done"))
    await execute(api_client, value)
    # The visible tuple still matches, but the immutable retained tenant also
    # includes the current principal's separately configured agent owner.
    configure(monkeypatch, value, uuid4())
    response = await evidence(api_client, value)
    assert response.status_code == 404 and "operation_refs" not in response.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("path,method,token,status", [
    (EVIDENCE_PATH, "POST", None, 401), (EVIDENCE_PATH + "/", "POST", None, 401),
    (EVIDENCE_PATH, "GET", None, 401), (EVIDENCE_PATH + "/", "POST", TOKEN, 404),
    (EVIDENCE_PATH + "//", "POST", TOKEN, 404), (EVIDENCE_PATH, "GET", TOKEN, 405),
    (EVIDENCE_PATH, "PUT", TOKEN, 405), (EVIDENCE_PATH, "HEAD", TOKEN, 405),
])
async def test_evidence_auth_precedes_body_alias_or_method_handling(monkeypatch, path, method, token, status):
    configure(monkeypatch, payload(), uuid4())
    async def forbidden(*args):
        raise AssertionError("Body or downstream accessed before boundary decision")
    sent = []
    async def send(event):
        sent.append(event)
    headers = [(b"x-service-token", token.encode())] if token else []
    await ManagedTurnBoundary(forbidden)(dict(type="http", path=path, method=method, headers=headers), forbidden, send)
    assert sent[0]["status"] == status
    assert (b"cache-control", b"no-store") in sent[0]["headers"]


@pytest.mark.asyncio
@pytest.mark.parametrize("provided", [[b"wrong"], [TOKEN.encode(), TOKEN.encode()], [b"\xff"], [b"internal-token"]])
async def test_evidence_rejects_bad_or_duplicate_tokens_before_body_and_db(monkeypatch, provided):
    configure(monkeypatch, payload(), uuid4())
    async def forbidden(*args):
        raise AssertionError("Unauthenticated evidence accessed body or database")
    sent = []
    async def send(event):
        sent.append(event)
    await ManagedTurnBoundary(forbidden)(dict(type="http", path=EVIDENCE_PATH, method="POST",
        headers=[(b"x-service-token", token) for token in provided]), forbidden, send)
    assert sent[0]["status"] == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation,status", [
    ("4096", 200), ("4097", 413), ("duplicate", 422), ("nonfinite", 422),
    ("extra", 422), ("utf16", 422), ("numeric_version", 422), ("malformed", 422),
    ("compressed", 415), ("scope", 404),
])
async def test_evidence_actual_stream_cap_strict_json_and_safe_errors(monkeypatch, mutation, status):
    value = payload()
    configure(monkeypatch, value, uuid4())
    body = json.dumps(evidence_value(value)).encode()
    if mutation in {"4096", "4097"}:
        body += b" " * (int(mutation) - len(body))
    elif mutation == "duplicate":
        body = body[:-1] + b',"owner_id":"private-sentinel"}'
    elif mutation == "nonfinite":
        body = body.replace(b'"contract_version": 1', b'"contract_version": NaN')
    elif mutation == "extra":
        body = body[:-1] + b',"messages":"private-sentinel"}'
    elif mutation == "utf16":
        body = body.decode().encode("utf-16")
    elif mutation == "numeric_version":
        body = body.replace(b'"contract_version": 1', b'"contract_version": true')
    elif mutation == "malformed":
        body = b"private-sentinel"
    elif mutation == "scope":
        body = body.replace(value["owner_id"].encode(), b"private-sentinel")
    headers = [(b"x-service-token", TOKEN.encode()), (b"content-type", b"application/json"),
               (b"content-length", b"1")]
    if mutation == "compressed":
        headers.append((b"content-encoding", b"gzip"))
    sent, entered = [], []
    chunks = iter(((body[:2048], True), (body[2048:], False)))
    async def receive():
        chunk, more = next(chunks)
        return dict(type="http.request", body=chunk, more_body=more)
    async def send(event):
        sent.append(event)
    async def app(scope, receive, send):
        entered.append(scope["managed_evidence_request"])
        await send(dict(type="http.response.start", status=200, headers=[]))
        await send(dict(type="http.response.body", body=b"{}"))
    await ManagedTurnBoundary(app)(dict(type="http", path=EVIDENCE_PATH, method="POST", headers=headers), receive, send)
    assert sent[0]["status"] == status and len(entered) == (1 if status == 200 else 0)
    assert (b"cache-control", b"no-store") in sent[0]["headers"]
    assert "private-sentinel" not in repr(sent)


@pytest.mark.integration
async def test_overridden_agent_db_requires_explicit_independent_business_factory(api_client, monkeypatch):
    value = payload()
    configure(monkeypatch, value, uuid4())
    with monkeypatch.context() as absent:
        absent.delattr(main.app.state, "business_session_factory")
        response = await evidence(api_client, value)
    assert response.status_code == 500 and response.json()["detail"]["code"] == "INTERNAL_ERROR"
    assert response.headers["cache-control"] == "no-store"


def test_evidence_openapi_has_closed_inline_request_schema():
    operation = main.app.openapi()["paths"][EVIDENCE_PATH]["post"]
    schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert schema["additionalProperties"] is False and "$ref" not in json.dumps(schema)
    assert set(schema["properties"]) == set(evidence_value(payload()))
