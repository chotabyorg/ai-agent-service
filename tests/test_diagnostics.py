import asyncio
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from redis.exceptions import RedisError

import api.v1.chat as chat_api_module
import main
import services.llm_service as llm_service_module
from api.v1.chat import ChatDiagnosticRoute
from models.agent import AgentTemplate
from services.encryption_service import EncryptionService
from services.chat_diagnostics import (
    ChatDiagnosticContext,
    ChatFailureStage,
    bind_chat_diagnostic_context,
    reset_chat_diagnostic_context,
    set_chat_failure_stage,
)
from tests.fakes import FakeMCPController, ScriptedLLMClient, build_llm_response


FAKE_OPENROUTER_KEY = "sk-or-v1-test-key-for-diagnostics"


def build_agent_payload() -> dict[str, object]:
    return {
        "name": "Support agent",
        "system_prompt": "Be concise and helpful.",
        "llm_provider": "openrouter",
        "model_name": "google/gemini-2.5-flash",
        "temperature": 0.4,
        "is_active": True,
    }


def build_headers(user_id=None) -> dict[str, str]:
    return {"x-user-id": str(user_id or uuid4())}


def assert_safe_chat_failure_log(
    log_calls: list[tuple[tuple[object, ...], dict[str, object]]],
    *,
    expected_stage: str,
    sensitive_values: list[str],
) -> None:
    assert len(log_calls) == 1
    args, kwargs = log_calls[0]
    assert args == (
        "chat_processing_failed failure_stage=%s exception_type=%s",
        expected_stage,
        "RuntimeError",
    )
    assert kwargs == {}
    assert "exc_info" not in kwargs
    assert "stack_info" not in kwargs

    rendered_fields = repr((args, kwargs))
    for sensitive_value in sensitive_values:
        assert sensitive_value not in rendered_fields


def capture_chat_failure_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[tuple[object, ...], dict[str, object]]]:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def capture(*args: object, **kwargs: object) -> None:
        calls.append((args, kwargs))

    monkeypatch.setattr(chat_api_module.logger, "error", capture)
    return calls


def assert_chat_diagnostic_route_installed() -> None:
    routes = [
        route
        for route in main.app.routes
        if getattr(route, "path", None) == "/v1/chat/completions"
    ]
    assert len(routes) == 1
    assert isinstance(routes[0], ChatDiagnosticRoute)


@pytest.mark.diagnostic
async def test_request_validation_still_returns_422_without_failure_log(
    api_client_no_raise,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert_chat_diagnostic_route_installed()
    log_calls = capture_chat_failure_logs(monkeypatch)

    response = await api_client_no_raise.post("/v1/chat/completions", json={})

    assert response.status_code == 422
    assert log_calls == []


@pytest.mark.diagnostic
async def test_invalid_s2s_auth_keeps_original_401_without_failure_log(
    api_client_no_raise,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert_chat_diagnostic_route_installed()
    monkeypatch.setattr(
        chat_api_module,
        "get_settings",
        lambda: SimpleNamespace(
            chat_s2s_token=SecretStr("expected-chat-token"),
            chat_s2s_auth_required=True,
        ),
    )
    log_calls = capture_chat_failure_logs(monkeypatch)

    response = await api_client_no_raise.post(
        "/v1/chat/completions",
        json={
            "agent_id": str(uuid4()),
            "external_client_id": "sentinel-unauthorized-client",
            "channel": "website",
            "message": "sentinel-unauthorized-request",
        },
        headers={"X-Service-Token": "invalid-sentinel-chat-token"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid service token"}
    assert response.headers["content-type"] == "application/json"
    assert log_calls == []


@pytest.mark.diagnostic
async def test_chat_diagnostic_contexts_are_isolated_across_awaits() -> None:
    both_bound = asyncio.Event()
    bound_count = 0

    async def exercise_context(
        initial_stage: ChatFailureStage,
        next_stage: ChatFailureStage,
    ) -> None:
        nonlocal bound_count
        context = ChatDiagnosticContext()
        token = bind_chat_diagnostic_context(context)
        try:
            set_chat_failure_stage(initial_stage)
            bound_count += 1
            if bound_count == 2:
                both_bound.set()

            await both_bound.wait()
            assert context.failure_stage is initial_stage

            set_chat_failure_stage(next_stage)
            await asyncio.sleep(0)
            assert context.failure_stage is next_stage
        finally:
            reset_chat_diagnostic_context(token)

    await asyncio.gather(
        exercise_context(ChatFailureStage.AGENT_LOOKUP, ChatFailureStage.CONTEXT_LOAD),
        exercise_context(ChatFailureStage.LLM_REQUEST, ChatFailureStage.HISTORY_PERSIST),
    )


async def create_agent(api_client, db_session_factory=None, owner_id=None) -> dict[str, object]:
    owner_id = owner_id or uuid4()
    response = await api_client.post(
        "/v1/agents/",
        json=build_agent_payload(),
        headers=build_headers(owner_id),
    )
    assert response.status_code == 201
    agent_data = response.json()
    if db_session_factory is not None:
        enc = EncryptionService()
        async with db_session_factory() as session:
            agent = await session.get(AgentTemplate, UUID(agent_data["id"]))
            agent.encrypted_openrouter_key = enc.encrypt(FAKE_OPENROUTER_KEY)
            await session.commit()
    return agent_data


@pytest.mark.diagnostic
async def test_agent_lookup_failure_logs_only_pre_llm_stage_and_class(
    api_client_no_raise,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert_chat_diagnostic_route_installed()
    exception_text = "sentinel-agent-lookup-error-detail"

    async def fail_agent_lookup(*args, **kwargs):
        raise RuntimeError(exception_text)

    monkeypatch.setattr(
        chat_api_module.llm_service.agent_service,
        "get_agent_by_id",
        fail_agent_lookup,
    )
    log_calls = capture_chat_failure_logs(monkeypatch)
    owner_id = uuid4()
    agent_id = uuid4()
    request_text = "sentinel-pre-llm-request-message"
    external_client_id = "sentinel-pre-llm-external-client"

    response = await api_client_no_raise.post(
        "/v1/chat/completions",
        json={
            "agent_id": str(agent_id),
            "external_client_id": external_client_id,
            "channel": "website",
            "message": request_text,
        },
        headers=build_headers(owner_id),
    )

    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert_safe_chat_failure_log(
        log_calls,
        expected_stage="agent_lookup",
        sensitive_values=[
            exception_text,
            request_text,
            external_client_id,
            str(owner_id),
            str(agent_id),
            FAKE_OPENROUTER_KEY,
        ],
    )


@pytest.mark.diagnostic
async def test_openai_provider_exception_results_in_http_500(
    api_client_no_raise,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert_chat_diagnostic_route_installed()
    controller = ScriptedLLMClient()
    exception_text = "sentinel-provider-error-detail"
    controller.chat.completions.queue(RuntimeError(exception_text))
    monkeypatch.setattr(llm_service_module, "AsyncOpenAI", lambda **kwargs: controller)
    owner_id = uuid4()
    agent = await create_agent(
        api_client_no_raise,
        db_session_factory=db_session,
        owner_id=owner_id,
    )
    request_text = "sentinel-request-message"
    external_client_id = "sentinel-external-client"
    log_calls = capture_chat_failure_logs(monkeypatch)

    response = await api_client_no_raise.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": external_client_id,
            "channel": "telegram",
            "message": request_text,
        },
        headers=build_headers(owner_id),
    )

    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert len(log_calls) == 1
    assert_safe_chat_failure_log(
        log_calls,
        expected_stage="llm_request",
        sensitive_values=[
            exception_text,
            request_text,
            external_client_id,
            str(owner_id),
            agent["id"],
            FAKE_OPENROUTER_KEY,
        ],
    )


@pytest.mark.diagnostic
async def test_mcp_init_generic_exception_results_in_http_500(
    api_client_no_raise,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert_chat_diagnostic_route_installed()
    controller = ScriptedLLMClient()
    controller.chat.completions.queue(build_llm_response(content="unused"))
    monkeypatch.setattr(llm_service_module, "AsyncOpenAI", lambda **kwargs: controller)

    exception_text = "sentinel-mcp-error-detail"
    mcp_controller = FakeMCPController(
        enter_errors={"bitrix": RuntimeError(exception_text)},
    )
    monkeypatch.setattr(llm_service_module, "MCPManager", mcp_controller.make_manager_class())

    owner_id = uuid4()
    agent = await create_agent(api_client_no_raise, db_session_factory=db_session, owner_id=owner_id)
    credential_value = "https://sensitive-bitrix.example/rest"
    credentials_response = await api_client_no_raise.put(
        f"/v1/agents/{agent['id']}/credentials",
        json={
            "service_name": "bitrix",
            "credentials": {"webhook_url": credential_value},
        },
        headers=build_headers(owner_id),
    )
    assert credentials_response.status_code == 200

    request_text = "sentinel-mcp-request-message"
    external_client_id = "sentinel-mcp-external-client"
    log_calls = capture_chat_failure_logs(monkeypatch)
    response = await api_client_no_raise.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": external_client_id,
            "channel": "telegram",
            "message": request_text,
        },
        headers=build_headers(owner_id),
    )

    assert response.status_code == 500
    assert response.text == "Internal Server Error"
    assert response.headers["content-type"] == "text/plain; charset=utf-8"
    assert_safe_chat_failure_log(
        log_calls,
        expected_stage="mcp_setup",
        sensitive_values=[
            exception_text,
            request_text,
            external_client_id,
            str(owner_id),
            agent["id"],
            credential_value,
            FAKE_OPENROUTER_KEY,
        ],
    )


@pytest.mark.diagnostic
def test_app_startup_fails_when_redis_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def failing_init_redis() -> None:
        raise RedisError("redis unavailable")

    async def noop_close_redis() -> None:
        return None

    monkeypatch.setattr(main, "init_redis", failing_init_redis)
    monkeypatch.setattr(main, "close_redis", noop_close_redis)

    with pytest.raises(RedisError):
        with TestClient(main.app):
            pass
