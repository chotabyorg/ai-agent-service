"""Managed wire, shared execution parity, separation and ambiguity behavior."""
import asyncio
import copy
import json
import logging
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from openai import APIError, APITimeoutError, AuthenticationError, RateLimitError
from pydantic import ValidationError
from sqlalchemy import func, select

import main
import services.llm_service as llm_module
from core.config import Settings, get_settings
from core.managed_boundary import MANAGED_PATH, ManagedTurnBoundary
from core.managed_security import managed_logging, parse_principals
from models.agent import AgentTemplate
from models.session import ConversationSession
from schemas.managed_turn import ManagedTurnRequest
from services.history_service import HistoryService
from services.managed_runtime import privacy_id
from services.session_service import SessionService
from tests.fakes import ScriptedLLMClient, build_llm_response, build_tool_call, build_tool_result
from tests.test_chat_flow import FakeKnowledgeBaseClient, create_agent, store_credentials

TOKEN = "test_managed_token_" + "x" * 32
SENTINEL = "managed-secret-sentinel"


def payload(agent_id=None, **overrides):
    message_id = uuid4()
    value = dict(contract_version=1, conversation_id=str(uuid4()), turn_id=str(uuid4()),
                 owner_id="Opaque-owner", integration_id=str(uuid4()), agent_id=str(agent_id or uuid4()),
                 channel="TELEGRAM", input_revision=1, authority_revision=0,
                 input_first_sequence=1, input_last_sequence=1, context_last_sequence=1,
                 messages=[dict(id=str(message_id), sequence=1, actor="CUSTOMER", direction="INBOUND",
                                content_type="TEXT", text="Привет")], selected_input_message_ids=[str(message_id)])
    value.update(overrides)
    return value


def configure(monkeypatch, value, owner):
    registry = [dict(principal_id="conversation-tests", token=TOKEN, caller="conversation-service",
                     execution_namespace="main-chota.conversation.v1", grants=[dict(
                         owner_id=value["owner_id"], integration_id=value["integration_id"],
                         channel=value["channel"], agent_id=value["agent_id"], agent_owner_id=str(owner))])]
    principals = parse_principals(json.dumps(registry), ("legacy-token", "internal-token"))
    monkeypatch.setattr(get_settings(), "_managed_principals", principals)
    monkeypatch.setattr(get_settings(), "privacy_filter_enabled", False)
    return registry


class CapturedClient(ScriptedLLMClient):
    def __init__(self):
        super().__init__()
        self.closed = False
        original = self.chat.completions.create

        async def capture(**kwargs):
            return await original(**copy.deepcopy(kwargs))
        self.chat.completions.create = capture

    async def close(self):
        self.closed = True


@pytest.fixture
def model(monkeypatch):
    client = CapturedClient()
    monkeypatch.setattr(llm_module, "AsyncOpenAI", lambda **kwargs: client)
    return client


async def setup(api_client, db_session, monkeypatch, **overrides):
    agent = await create_agent(api_client, db_session_factory=db_session)
    value = payload(agent["id"], **overrides)
    configure(monkeypatch, value, agent["user_id"])
    return agent, value


async def execute(api_client, value):
    return await api_client.post(MANAGED_PATH, json=value, headers={"X-Service-Token": TOKEN})


@pytest.mark.integration
async def test_managed_no_session_history_and_legacy_prompt_parity(api_client, db_session, fake_redis, monkeypatch, model):
    agent, value = await setup(api_client, db_session, monkeypatch)
    value["messages"][0]["text"] = "same input"
    model.chat.completions.queue(build_llm_response(content=" <thinking>private</thinking>Answer "),
                                 build_llm_response(content="Answer"))
    legacy = await api_client.post("/v1/chat/completions", json=dict(agent_id=agent["id"],
        external_client_id="legacy", channel="telegram", message="same input"))
    assert legacy.status_code == 200
    async with db_session() as db:
        before = await db.scalar(select(func.count()).select_from(ConversationSession))
    keys = await fake_redis.keys("*")
    values = {key: await fake_redis.get(key) for key in keys}

    async def forbidden(*args, **kwargs):
        raise AssertionError("Managed touched legacy history/session")
    monkeypatch.setattr(SessionService, "get_or_create_session", forbidden)
    monkeypatch.setattr(HistoryService, "load_history", forbidden)
    monkeypatch.setattr(HistoryService, "save_history", forbidden)
    identities = []
    original_core = llm_module.LLMService._execute_core

    async def captured_core(self, *args, **kwargs):
        runtime = kwargs["runtime"]
        identities.append((runtime.request.conversation_id, runtime.request.turn_id,
                           runtime.execution_namespace))
        return await original_core(self, *args, **kwargs)

    monkeypatch.setattr(llm_module.LLMService, "_execute_core", captured_core)
    response = await execute(api_client, value)
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "DRAFT_READY" and result["draft_text"] == "Answer"
    assert result["error_code"] is None and result["operation_refs"] == []
    assert identities == [(UUID(value["conversation_id"]), UUID(value["turn_id"]),
                           "main-chota.conversation.v1")]
    for field in ("conversation_id", "turn_id", "agent_id", "input_revision", "authority_revision",
                  "input_first_sequence", "input_last_sequence", "context_last_sequence"):
        assert result[field] == value[field]
    assert model.chat.completions.calls[0] == model.chat.completions.calls[1]
    assert model.closed
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(ConversationSession)) == before
    assert await fake_redis.keys("*") == keys
    assert {key: await fake_redis.get(key) for key in keys} == values


@pytest.mark.integration
@pytest.mark.parametrize("first,last", [(1, 1), (1, 2), (2, 3)])
async def test_projection_non_suffix_kb_roles_and_selector(api_client, db_session, monkeypatch, model, first, last):
    agent, value = await setup(api_client, db_session, monkeypatch)
    value["messages"] = [dict(id=str(uuid4()), sequence=i, actor=actor, direction="INBOUND",
        content_type="TEXT", text=text) for i, (actor, text) in enumerate([
        ("CUSTOMER", "first"), ("HUMAN", "operator"), ("SYSTEM", "event")], 1)]
    value.update(input_first_sequence=first, input_last_sequence=last, context_last_sequence=3,
                 selected_input_message_ids=[m["id"] for m in value["messages"][first - 1:last]])
    FakeKnowledgeBaseClient.reset()
    monkeypatch.setattr(llm_module, "KnowledgeBaseClient", FakeKnowledgeBaseClient)
    monkeypatch.setattr(get_settings(), "knowledge_base_enabled", True)
    monkeypatch.setattr(get_settings(), "internal_api_token", "fixture-only-internal")
    model.chat.completions.queue(build_llm_response(content="result"))
    response = await execute(api_client, value)
    assert response.json()["status"] == "DRAFT_READY"
    messages = model.chat.completions.calls[0]["messages"]
    assert messages[-3:] == [dict(role="user", content="first"), dict(role="assistant", content="operator"),
                            dict(role="user", content="[System communication event]\nevent")]
    assert messages[1]["role"] == "system"
    assert ", ".join(map(str, range(first, last + 1))) in messages[1]["content"]
    call = FakeKnowledgeBaseClient.calls[0]
    assert call["owner_id"] == agent["user_id"] and str(call["agent_id"]) == agent["id"]
    assert call["query"] == "\n\n".join(m["text"] for m in value["messages"][first-1:last])


@pytest.mark.integration
async def test_kb_failure_is_safe_fail_open(api_client, db_session, monkeypatch, model, caplog):
    _, value = await setup(api_client, db_session, monkeypatch)
    FakeKnowledgeBaseClient.reset()
    FakeKnowledgeBaseClient.error = RuntimeError(SENTINEL)
    monkeypatch.setattr(llm_module, "KnowledgeBaseClient", FakeKnowledgeBaseClient)
    monkeypatch.setattr(get_settings(), "knowledge_base_enabled", True)
    monkeypatch.setattr(get_settings(), "internal_api_token", "test-internal")
    model.chat.completions.queue(build_llm_response(content="answer"))
    with caplog.at_level(logging.DEBUG):
        response = await execute(api_client, value)
    assert response.json()["status"] == "DRAFT_READY"
    assert SENTINEL not in caplog.text
    assert [m["role"] for m in model.chat.completions.calls[0]["messages"]] == ["system", "user"]


class OwnedFakeManager:
    result = None
    calls = []
    cleanup_error = False
    metadata = {"mutate": {"tier": "core"}, "second": {"tier": "core"}}

    def __init__(self, service_name, credentials):
        self.service_name = service_name

    async def acquire_owned(self, stack, runtime):
        async def close():
            if self.cleanup_error:
                raise RuntimeError(SENTINEL)
        stack.push_async_callback(close)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        if self.cleanup_error:
            raise RuntimeError(SENTINEL)

    def get_openai_tools(self):
        return [dict(type="function", function=dict(name=name, parameters={"type": "object"})) for name in self.metadata]

    async def get_tools_metadata(self):
        return self.metadata

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def tools(monkeypatch):
    OwnedFakeManager.calls = []
    OwnedFakeManager.cleanup_error = False
    OwnedFakeManager.metadata = {"mutate": {"tier": "core"}, "second": {"tier": "core"}}
    monkeypatch.setattr(llm_module, "MCPManager", OwnedFakeManager)
    return OwnedFakeManager


@pytest.mark.integration
@pytest.mark.parametrize("service,result", [
    ("bitrix", SimpleNamespace(isError=False, content=[], structuredContent={"error": SENTINEL})),
    ("bitrix", build_tool_result(json.dumps({"error": SENTINEL}))),
    ("bitrix", build_tool_result(json.dumps([{"error": SENTINEL}]))),
    ("bitrix", SimpleNamespace(isError=False, content=[], structuredContent={"result": [{"error": SENTINEL}]})),
    ("bitrix", SimpleNamespace(isError=False, content=[SimpleNamespace(text=json.dumps({"error": SENTINEL}))], structuredContent={"ok": True})),
    ("google_workspace", SimpleNamespace(isError=False, content=[], structuredContent={"ok": False, "error": {"message": SENTINEL}})),
    ("google_workspace", build_tool_result(json.dumps({"ok": False, "error": {"message": SENTINEL}}))),
    ("google_workspace", SimpleNamespace(isError=False, content=[], structuredContent={"result": {"ok": False, "error": SENTINEL}})),
])
async def test_lost_reply_error_stops_before_next_model_or_same_response_tool(api_client, db_session, monkeypatch, model, tools, service, result, caplog):
    agent, value = await setup(api_client, db_session, monkeypatch)
    creds = {"webhook_url": "https://synthetic.invalid/rest"} if service == "bitrix" else dict(client_id="fake", client_secret="fake", refresh_token="fake")
    await store_credentials(api_client, agent["id"], service, creds, owner_id=UUID(agent["user_id"]))
    tools.result = result
    model.chat.completions.queue(build_llm_response(tool_calls=[build_tool_call("one", "mutate", '{"x":1}'), build_tool_call("two", "second", '{}')]),
        build_llm_response(tool_calls=[build_tool_call("repeat", "mutate", '{ "x" : 1 }')]), build_llm_response(content="unsafe draft"))
    with caplog.at_level(logging.DEBUG):
        response = await execute(api_client, value)
    assert response.json()["status"] == "UNKNOWN"
    assert response.json()["error_code"] == "TOOL_EXECUTION_UNKNOWN"
    assert response.json()["draft_text"] is None and response.json()["operation_refs"] == []
    assert len(model.chat.completions.calls) == 1 and tools.calls == [("mutate", {"x": 1})]
    assert len(model.chat.completions._responses) == 2
    assert SENTINEL not in json.dumps(model.chat.completions.calls) + response.text + caplog.text
    assert model.closed


@pytest.mark.integration
async def test_managed_successful_multi_step_tool_and_dedupe(api_client, db_session, monkeypatch, model, tools):
    agent, value = await setup(api_client, db_session, monkeypatch)
    await store_credentials(api_client, agent["id"], "bitrix", {"webhook_url": "https://synthetic.invalid/rest"}, owner_id=UUID(agent["user_id"]))
    tools.result = build_tool_result('{"result":[{"ID":"synthetic"}]}')
    model.chat.completions.queue(build_llm_response(tool_calls=[build_tool_call("one", "mutate", '{}')]),
                                 build_llm_response(tool_calls=[build_tool_call("two", "mutate", '{}'), build_tool_call("three", "second", '{}')]),
                                 build_llm_response(content="answer"))
    result = (await execute(api_client, value)).json()
    assert result["status"] == "DRAFT_READY" and result["operation_refs"] == []
    assert tools.calls == [("mutate", {}), ("second", {})]
    assert len(model.chat.completions.calls) == 3


@pytest.mark.integration
async def test_legacy_error_continues_and_save_precedes_cleanup_failure(api_client, db_session, monkeypatch, model, tools, fake_redis):
    agent = await create_agent(api_client, db_session_factory=db_session)
    await store_credentials(api_client, agent["id"], "bitrix", {"webhook_url": "https://synthetic.invalid/rest"}, owner_id=UUID(agent["user_id"]))
    tools.result = build_tool_result(json.dumps({"error": SENTINEL}))
    tools.cleanup_error = True
    model.chat.completions.queue(build_llm_response(tool_calls=[build_tool_call("one", "mutate", '{}')]), build_llm_response(content="legacy answer"))
    response = await api_client.post("/v1/chat/completions", json=dict(agent_id=agent["id"], external_client_id="cleanup", channel="telegram", message="input"))
    assert response.status_code == 500 and response.text == "Internal Server Error"
    assert SENTINEL in json.dumps(model.chat.completions.calls[1]["messages"])
    keys = await fake_redis.keys("chat_history:*")
    assert len(keys) == 1
    assert json.loads(await fake_redis.get(keys[0])) == [dict(role="user", content="input"), dict(role="assistant", content="legacy answer")]


@pytest.mark.integration
@pytest.mark.parametrize("condition,code", [("model", "MODEL_ERROR"), ("cleanup", "TOOL_CLEANUP_FAILED"), ("tool", "TOOL_EXECUTION_UNKNOWN")])
async def test_post_dispatch_errors_are_unknown_and_safe(api_client, db_session, monkeypatch, model, tools, condition, code, caplog):
    agent, value = await setup(api_client, db_session, monkeypatch)
    await store_credentials(api_client, agent["id"], "bitrix", {"webhook_url": "https://synthetic.invalid/rest"}, owner_id=UUID(agent["user_id"]))
    tools.result = RuntimeError(SENTINEL) if condition == "tool" else build_tool_result('{"ok":true}')
    tools.cleanup_error = condition == "cleanup"
    model.chat.completions.queue(build_llm_response(tool_calls=[build_tool_call("one", "mutate", '{}')]),
                                 APIError(SENTINEL, httpx.Request("POST", "http://synthetic"), body={}) if condition == "model" else build_llm_response(content="answer"))
    with caplog.at_level(logging.DEBUG):
        response = await execute(api_client, value)
    assert response.json()["status"] == "UNKNOWN" and response.json()["error_code"] == code
    assert response.json()["draft_text"] is None
    assert SENTINEL not in response.text + caplog.text + json.dumps(model.chat.completions.calls)
    assert model.closed


@pytest.mark.integration
async def test_scoped_agent_unknown_foreign_and_ungranted_are_equal(api_client, db_session, monkeypatch, model):
    agent, value = await setup(api_client, db_session, monkeypatch)
    configure(monkeypatch, value, uuid4())
    foreign = await execute(api_client, value)
    unknown = copy.deepcopy(value)
    unknown["agent_id"] = str(uuid4())
    configure(monkeypatch, unknown, uuid4())
    absent = await execute(api_client, unknown)
    ungranted = await execute(api_client, value)
    assert foreign.status_code == absent.status_code == ungranted.status_code == 404
    assert foreign.json() == absent.json() == ungranted.json()
    assert model.chat.completions.calls == []


@pytest.mark.parametrize("change", [dict(input_revision=True), dict(input_revision="1"), dict(authority_revision=-1), dict(input_first_sequence=2), dict(context_last_sequence=2), dict(owner_id="*"), dict(owner_id="\x00"), dict(system_prompt="bad"), dict(channel="telegram")])
def test_schema_rejects_malformed_context(change):
    with pytest.raises(ValidationError):
        ManagedTurnRequest.model_validate(payload(**change))


def test_privacy_namespace_is_stable_separated_and_excludes_turn():
    value = payload()
    base = privacy_id(ManagedTurnRequest.model_validate(value))
    assert base != UUID(value["conversation_id"])
    changed = {**value, "turn_id": str(uuid4())}
    assert privacy_id(ManagedTurnRequest.model_validate(changed)) == base
    for field in ("conversation_id", "agent_id", "integration_id"):
        assert privacy_id(ManagedTurnRequest.model_validate({**value, field: str(uuid4())})) != base
    for field, other in (("owner_id", "other"), ("channel", "VOICE")):
        assert privacy_id(ManagedTurnRequest.model_validate({**value, field: other})) != base


@pytest.mark.parametrize("mutation", ["duplicate", "mapping", "wildcard", "bad_token", "shared_token", "wrong_caller"])
def test_registry_invalid_configuration_never_exposes_input(mutation):
    value = payload()
    registry = [dict(principal_id="p", token=TOKEN, caller="conversation-service", execution_namespace="main-chota.conversation.v1", grants=[dict(owner_id=value["owner_id"], integration_id=value["integration_id"], channel=value["channel"], agent_id=value["agent_id"], agent_owner_id=str(uuid4()))])]
    if mutation == "duplicate":
        registry.append(registry[0])
    elif mutation == "mapping":
        second = copy.deepcopy(registry[0]["grants"][0])
        second.update(agent_id=str(uuid4()), agent_owner_id=str(uuid4()))
        registry[0]["grants"].append(second)
    elif mutation == "wildcard":
        registry[0]["grants"][0]["owner_id"] = "*"
    elif mutation == "bad_token":
        registry[0]["token"] = SENTINEL
    elif mutation == "shared_token":
        registry[0]["token"] = TOKEN
    else:
        registry[0]["caller"] = SENTINEL
    with pytest.raises(RuntimeError) as error:
        Settings(_env_file=None, managed_ai_auth_principals=json.dumps(registry), internal_api_token=TOKEN if mutation == "shared_token" else "")
    assert str(error.value) == "Invalid managed AI authentication configuration"
    assert SENTINEL not in str(error.value) and TOKEN not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("provided", [[], [b"wrong"], [TOKEN.encode(), TOKEN.encode()], [b"\xff"], [b"internal-token"]])
async def test_boundary_auth_before_body_or_downstream(monkeypatch, provided):
    value = payload()
    configure(monkeypatch, value, uuid4())
    async def explode(*args):
        raise AssertionError("Unauthorized request consumed body or entered app")
    sent = []
    async def send(event):
        sent.append(event)
    scope = dict(type="http", path=MANAGED_PATH, headers=[(b"x-service-token", token) for token in provided])
    await ManagedTurnBoundary(explode)(scope, explode, send)
    assert sent[0]["status"] == 401 and SENTINEL not in str(sent)


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,body,expected", [
    ([(b"content-type", b"text/plain")], b"{}", 415),
    ([(b"content-type", b"application/json"),(b"content-encoding", b"gzip")], b"{}", 415),
    ([(b"content-type", b"application/json")], b"x"*1048577, 413),
    ([(b"content-type", b"application/json")], b'{"secret":"managed-secret-sentinel"}', 422),
    ([(b"content-type", b"application/json")], b'{"a":1,"a":2}', 422),
])
async def test_boundary_caps_actual_bytes_and_safe_errors(monkeypatch, headers, body, expected):
    configure(monkeypatch, payload(), uuid4())
    async def explode(*args):
        raise AssertionError("Invalid context entered app")
    sent = []
    async def receive():
        return dict(type="http.request", body=body, more_body=False)
    async def send(event):
        sent.append(event)
    await ManagedTurnBoundary(explode)(dict(type="http", path=MANAGED_PATH,
        headers=[(b"x-service-token",TOKEN.encode()),*headers]), receive, send)
    assert sent[0]["status"] == expected and SENTINEL not in str(sent)


@pytest.mark.asyncio
async def test_context_local_log_sanitization_preserves_concurrent_legacy(caplog):
    async def managed():
        mark = managed_logging.set(True)
        try:
            await asyncio.sleep(0)
            logging.getLogger("sqlalchemy.engine").warning(SENTINEL, exc_info=(RuntimeError,RuntimeError(SENTINEL),None), extra={"params": SENTINEL})
        finally:
            managed_logging.reset(mark)
    async def legacy():
        await asyncio.sleep(0)
        logging.getLogger("legacy").warning("legacy-preserved")
    with caplog.at_level(logging.WARNING):
        await asyncio.gather(managed(), legacy())
    assert SENTINEL not in caplog.text and "legacy-preserved" in caplog.text
