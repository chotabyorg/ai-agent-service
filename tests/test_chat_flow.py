import asyncio
import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

import services.llm_service as llm_service_module
from models.agent import AgentCredentials, AgentTemplate
from models.session import ConversationSession
from services.encryption_service import EncryptionService
from tests.fakes import (
    FakeMCPController,
    ScriptedLLMClient,
    build_llm_response,
    build_tool_call,
    build_tool_result,
)


FAKE_OPENROUTER_KEY = "sk-or-v1-test-key-for-unit-tests"


def build_agent_payload() -> dict[str, object]:
    return {
        "name": "Support agent",
        "system_prompt": "Be concise and helpful.",
        "llm_provider": "openrouter",
        "model_name": "google/gemini-2.5-flash",
        "temperature": 0.4,
        "is_active": True,
    }


def build_headers(user_id: UUID | None = None) -> dict[str, str]:
    return {"x-user-id": str(user_id or uuid4())}


async def create_agent(api_client, db_session_factory=None, **overrides) -> dict[str, object]:
    owner_id = overrides.pop("owner_id", uuid4())
    payload = build_agent_payload()
    payload.update(overrides)
    response = await api_client.post("/v1/agents/", json=payload, headers=build_headers(owner_id))
    assert response.status_code == 201
    agent_data = response.json()
    # Set encrypted OpenRouter key directly in DB so chat works
    if db_session_factory is not None:
        enc = EncryptionService()
        async with db_session_factory() as session:
            agent = await session.get(AgentTemplate, UUID(agent_data["id"]))
            agent.encrypted_openrouter_key = enc.encrypt(FAKE_OPENROUTER_KEY)
            await session.commit()
    return agent_data


async def store_credentials(
    api_client,
    agent_id: str,
    service_name: str,
    credentials: dict[str, object],
    owner_id: UUID | None = None,
) -> None:
    response = await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={"service_name": service_name, "credentials": credentials},
        headers=build_headers(owner_id),
    )
    assert response.status_code == 200


@pytest.fixture
def llm_controller(monkeypatch: pytest.MonkeyPatch) -> ScriptedLLMClient:
    controller = ScriptedLLMClient()
    monkeypatch.setattr(
        llm_service_module, "AsyncOpenAI", lambda **kwargs: controller
    )
    return controller


@pytest.fixture
def mcp_controller(monkeypatch: pytest.MonkeyPatch) -> FakeMCPController:
    controller = FakeMCPController()
    monkeypatch.setattr(llm_service_module, "MCPManager", controller.make_manager_class())
    return controller


@pytest.mark.integration
async def test_chat_creates_and_reuses_session_key(api_client, db_session, llm_controller, fake_redis) -> None:
    llm_controller.chat.completions.queue(
        build_llm_response(content="Echo: Hello"),
        build_llm_response(content="Echo: How are you?"),
    )
    agent = await create_agent(api_client, db_session_factory=db_session)

    first = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "telegram:42",
            "channel": "telegram",
            "message": "Hello",
        },
    )
    second = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "telegram:42",
            "channel": "telegram",
            "message": "How are you?",
        },
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["session_id"] == second.json()["session_id"]

    async with db_session() as session:
        sessions = await session.execute(select(ConversationSession))
        stored_sessions = sessions.scalars().all()
    assert len(stored_sessions) == 1
    history = json.loads(await fake_redis.get(f"chat_history:{first.json()['session_id']}"))
    assert history == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Echo: Hello"},
        {"role": "user", "content": "How are you?"},
        {"role": "assistant", "content": "Echo: How are you?"},
    ]


@pytest.mark.integration
async def test_chat_uses_privacy_proxy_when_enabled(
    api_client,
    db_session,
    fake_redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_requests: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {
                "id": "chatcmpl-privacy",
                "object": "chat.completion",
                "model": "google/gemini-2.5-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Privacy answer",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
                "privacy": {
                    "privacy_session_id": "unused",
                    "replacements_count": 1,
                    "restored_items_count": 1,
                    "detector_model": "openai/privacy-filter",
                },
            }

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, headers, json):
            captured_requests.append({"url": url, "headers": headers, "json": json})
            return FakeResponse()

    def fail_direct_openai(**kwargs):
        raise AssertionError("Direct OpenRouter client must not be created in privacy mode.")

    monkeypatch.setattr(llm_service_module, "AsyncOpenAI", fail_direct_openai)
    monkeypatch.setattr(llm_service_module.httpx, "AsyncClient", FakeAsyncClient)

    from api.v1.chat import llm_service

    monkeypatch.setattr(llm_service.settings, "privacy_filter_enabled", True)
    monkeypatch.setattr(llm_service.settings, "privacy_filter_base_url", "http://privacy-filter-service:8000")
    monkeypatch.setattr(llm_service.settings, "internal_api_token", "service-token")
    monkeypatch.setattr(llm_service.settings, "privacy_filter_timeout_seconds", 5.0)

    agent = await create_agent(api_client, db_session_factory=db_session)

    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "privacy-user",
            "channel": "telegram",
            "message": "Hello with PII",
        },
    )

    assert response.status_code == 200
    assert response.json()["answer"] == "Privacy answer"
    assert captured_requests
    request = captured_requests[0]
    assert request["url"] == "http://privacy-filter-service:8000/api/v1/privacy-filter/chat/completions"
    assert request["headers"]["Authorization"] == "Bearer service-token"
    assert request["json"]["upstream"]["api_key"] == FAKE_OPENROUTER_KEY
    assert request["json"]["privacy_session_id"] == response.json()["session_id"]
    assert request["json"]["chat_completion"]["messages"][-1]["content"] == "Hello with PII"


@pytest.mark.integration
async def test_chat_privacy_proxy_unavailable_fails_closed(
    api_client,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, headers, json):
            raise llm_service_module.httpx.ConnectError("proxy unavailable")

    def fail_direct_openai(**kwargs):
        raise AssertionError("Direct OpenRouter fallback must not be used in privacy mode.")

    monkeypatch.setattr(llm_service_module, "AsyncOpenAI", fail_direct_openai)
    monkeypatch.setattr(llm_service_module.httpx, "AsyncClient", FailingAsyncClient)

    from api.v1.chat import llm_service

    monkeypatch.setattr(llm_service.settings, "privacy_filter_enabled", True)
    monkeypatch.setattr(llm_service.settings, "privacy_filter_base_url", "http://privacy-filter-service:8000")
    monkeypatch.setattr(llm_service.settings, "internal_api_token", "service-token")
    monkeypatch.setattr(llm_service.settings, "privacy_filter_timeout_seconds", 5.0)

    agent = await create_agent(api_client, db_session_factory=db_session)

    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "privacy-user-down",
            "channel": "telegram",
            "message": "Hello with PII",
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Cannot connect to privacy filter service."}


@pytest.mark.integration
async def test_chat_privacy_proxy_preserves_mcp_tool_loop(
    api_client,
    db_session,
    mcp_controller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_requests: list[dict[str, object]] = []
    responses = [
        {
            "id": "chatcmpl-tool-1",
            "object": "chat.completion",
            "model": "google/gemini-2.5-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "tool-1",
                                "type": "function",
                                "function": {
                                    "name": "find_contact",
                                    "arguments": "{\"phone\":\"+375291112233\"}",
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            "privacy": {
                "privacy_session_id": "unused",
                "replacements_count": 1,
                "restored_items_count": 1,
                "detector_model": "openai/privacy-filter",
            },
        },
        {
            "id": "chatcmpl-tool-2",
            "object": "chat.completion",
            "model": "google/gemini-2.5-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "Contact found"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 14, "completion_tokens": 2, "total_tokens": 16},
            "privacy": {
                "privacy_session_id": "unused",
                "replacements_count": 0,
                "restored_items_count": 0,
                "detector_model": "openai/privacy-filter",
            },
        },
    ]

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, headers, json):
            captured_requests.append({"url": url, "headers": headers, "json": json})
            return FakeResponse(responses.pop(0))

    mcp_controller.tools_by_service["bitrix"] = [
        {
            "type": "function",
            "function": {
                "name": "find_contact",
                "description": "Find a contact",
                "parameters": {"type": "object", "properties": {"phone": {"type": "string"}}},
            },
        }
    ]
    mcp_controller.tool_results["find_contact"] = build_tool_result('{"contact_id": 42}')

    def fail_direct_openai(**kwargs):
        raise AssertionError("Direct OpenRouter fallback must not be used in privacy mode.")

    monkeypatch.setattr(llm_service_module, "AsyncOpenAI", fail_direct_openai)
    monkeypatch.setattr(llm_service_module.httpx, "AsyncClient", FakeAsyncClient)

    from api.v1.chat import llm_service

    monkeypatch.setattr(llm_service.settings, "privacy_filter_enabled", True)
    monkeypatch.setattr(llm_service.settings, "privacy_filter_base_url", "http://privacy-filter-service:8000")
    monkeypatch.setattr(llm_service.settings, "internal_api_token", "service-token")
    monkeypatch.setattr(llm_service.settings, "privacy_filter_timeout_seconds", 5.0)

    agent = await create_agent(api_client, db_session_factory=db_session)
    await store_credentials(
        api_client,
        agent["id"],
        "bitrix",
        {"webhook_url": "https://example.bitrix/rest"},
        owner_id=UUID(agent["user_id"]),
    )

    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "privacy-tool-user",
            "channel": "telegram",
            "message": "Find +375291112233",
        },
    )

    assert response.status_code == 200
    assert response.json()["answer"] == "Contact found"
    assert mcp_controller.tool_calls == [
        {
            "service_name": "bitrix",
            "tool_name": "find_contact",
            "tool_args": {"phone": "+375291112233"},
        }
    ]
    assert len(captured_requests) == 2
    assert captured_requests[1]["json"]["chat_completion"]["messages"][-1]["role"] == "tool"


@pytest.mark.integration
async def test_chat_privacy_proxy_insufficient_funds_deactivates_agent(
    api_client,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        status_code = 402

        def json(self):
            return {"detail": "LLM service returned an error."}

    class FakeAsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url, headers, json):
            return FakeResponse()

    monkeypatch.setattr(llm_service_module.httpx, "AsyncClient", FakeAsyncClient)

    from api.v1.chat import llm_service

    monkeypatch.setattr(llm_service.settings, "privacy_filter_enabled", True)
    monkeypatch.setattr(llm_service.settings, "privacy_filter_base_url", "http://privacy-filter-service:8000")
    monkeypatch.setattr(llm_service.settings, "internal_api_token", "service-token")
    monkeypatch.setattr(llm_service.settings, "privacy_filter_timeout_seconds", 5.0)

    agent = await create_agent(api_client, db_session_factory=db_session)

    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "privacy-funds-user",
            "channel": "telegram",
            "message": "Hello",
        },
    )

    assert response.status_code == 402
    assert response.json() == {"detail": "No funds on OpenRouter. Agent deactivated."}

    async with db_session() as session:
        stored_agent = await session.get(AgentTemplate, UUID(agent["id"]))
        assert stored_agent is not None
        assert stored_agent.is_active is False


@pytest.mark.integration
async def test_chat_creates_distinct_sessions_for_different_channel_and_agent(
    api_client,
    db_session,
    llm_controller,
) -> None:
    llm_controller.chat.completions.queue(
        build_llm_response(content="telegram"),
        build_llm_response(content="instagram"),
        build_llm_response(content="other-agent"),
    )
    first_agent = await create_agent(api_client, db_session_factory=db_session)
    second_agent = await create_agent(api_client, db_session_factory=db_session, name="Backup agent")

    first = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": first_agent["id"],
            "external_client_id": "user-1",
            "channel": "telegram",
            "message": "Hello",
        },
    )
    second = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": first_agent["id"],
            "external_client_id": "user-1",
            "channel": "instagram",
            "message": "Hello",
        },
    )
    third = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": second_agent["id"],
            "external_client_id": "user-1",
            "channel": "telegram",
            "message": "Hello",
        },
    )

    assert len({first.json()["session_id"], second.json()["session_id"], third.json()["session_id"]}) == 3

    async with db_session() as session:
        count_result = await session.execute(select(func.count()).select_from(ConversationSession))
        assert count_result.scalar_one() == 3


@pytest.mark.integration
async def test_chat_uses_existing_redis_history_and_strips_thinking_tags(
    api_client,
    db_session,
    llm_controller,
    fake_redis,
) -> None:
    llm_controller.chat.completions.queue(build_llm_response(content="Visible answer"))
    agent = await create_agent(api_client, db_session_factory=db_session)
    first = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "existing-user",
            "channel": "telegram",
            "message": "Initial message",
        },
    )
    session_id = first.json()["session_id"]
    await fake_redis.set(
        f"chat_history:{session_id}",
        json.dumps(
            [
                {"role": "user", "content": "Prev user"},
                {"role": "assistant", "content": "Prev assistant"},
            ]
        ),
    )
    llm_controller.chat.completions.queue(
        build_llm_response(content="<thinking>draft</thinking>Final answer"),
    )

    second = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "existing-user",
            "channel": "telegram",
            "message": "New question",
        },
    )

    assert second.status_code == 200
    assert second.json()["answer"] == "Final answer"
    messages = llm_controller.chat.completions.calls[-1]["messages"]
    assert messages == [
        {"role": "system", "content": "Be concise and helpful."},
        {"role": "user", "content": "Prev user"},
        {"role": "assistant", "content": "Prev assistant"},
        {"role": "user", "content": "New question"},
    ]


@pytest.mark.integration
async def test_chat_falls_back_to_default_answer_when_model_returns_empty_content(
    api_client,
    db_session,
    llm_controller,
) -> None:
    llm_controller.chat.completions.queue(build_llm_response(content=""))
    agent = await create_agent(api_client, db_session_factory=db_session)

    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "user-1",
            "channel": "telegram",
            "message": "Hello",
        },
    )

    assert response.status_code == 200
    assert response.json()["answer"] == "I processed your request."


@pytest.mark.integration
async def test_chat_with_tool_call_executes_mcp_and_saves_only_user_and_assistant_turns(
    api_client,
    db_session,
    llm_controller,
    mcp_controller,
    fake_redis,
) -> None:
    mcp_controller.tools_by_service["bitrix"] = [
        {
            "type": "function",
            "function": {
                "name": "find_contact",
                "description": "Find a contact",
                "parameters": {"type": "object", "properties": {"phone": {"type": "string"}}},
            },
        }
    ]
    mcp_controller.tool_results["find_contact"] = build_tool_result('{"contact_id": 42}')
    llm_controller.chat.completions.queue(
        build_llm_response(
            tool_calls=[build_tool_call("tool-1", "find_contact", '{"phone":"+123"}')]
        ),
        build_llm_response(content="Contact found"),
    )
    agent = await create_agent(api_client, db_session_factory=db_session)
    await store_credentials(
        api_client,
        agent["id"],
        "bitrix",
        {"webhook_url": "https://example.bitrix/rest"},
        owner_id=UUID(agent["user_id"]),
    )

    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "user-42",
            "channel": "telegram",
            "message": "Find this contact",
        },
    )

    assert response.status_code == 200
    assert response.json()["answer"] == "Contact found"
    assert mcp_controller.tool_calls == [
        {
            "service_name": "bitrix",
            "tool_name": "find_contact",
            "tool_args": {"phone": "+123"},
        }
    ]
    first_call = llm_controller.chat.completions.calls[0]
    second_call = llm_controller.chat.completions.calls[1]
    assert first_call["tools"] == mcp_controller.tools_by_service["bitrix"]
    assert second_call["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "tool-1",
        "name": "find_contact",
        "content": '{"contact_id": 42}',
    }
    history = json.loads(await fake_redis.get(f"chat_history:{response.json()['session_id']}"))
    assert history == [
        {"role": "user", "content": "Find this contact"},
        {"role": "assistant", "content": "Contact found"},
    ]


@pytest.mark.integration
async def test_chat_continues_after_tool_execution_failure(api_client, db_session, llm_controller, mcp_controller) -> None:
    mcp_controller.tools_by_service["bitrix"] = [
        {
            "type": "function",
            "function": {
                "name": "find_contact",
                "description": "Find a contact",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    mcp_controller.call_errors["find_contact"] = RuntimeError("bitrix unavailable")
    llm_controller.chat.completions.queue(
        build_llm_response(tool_calls=[build_tool_call("tool-1", "find_contact", "{}")]),
        build_llm_response(content="Fallback answer"),
    )
    agent = await create_agent(api_client, db_session_factory=db_session)
    await store_credentials(
        api_client,
        agent["id"],
        "bitrix",
        {"webhook_url": "https://example.bitrix/rest"},
        owner_id=UUID(agent["user_id"]),
    )

    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "user-42",
            "channel": "telegram",
            "message": "Find this contact",
        },
    )

    assert response.status_code == 200
    tool_message = llm_controller.chat.completions.calls[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert "Tool execution failed: bitrix unavailable" in tool_message["content"]


@pytest.mark.integration
async def test_chat_passes_empty_dict_when_tool_arguments_are_invalid_json(
    api_client,
    db_session,
    llm_controller,
    mcp_controller,
) -> None:
    mcp_controller.tools_by_service["bitrix"] = [
        {
            "type": "function",
            "function": {
                "name": "find_contact",
                "description": "Find a contact",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    llm_controller.chat.completions.queue(
        build_llm_response(tool_calls=[build_tool_call("tool-1", "find_contact", "{not-json}")]),
        build_llm_response(content="Handled invalid args"),
    )
    agent = await create_agent(api_client, db_session_factory=db_session)
    await store_credentials(
        api_client,
        agent["id"],
        "bitrix",
        {"webhook_url": "https://example.bitrix/rest"},
        owner_id=UUID(agent["user_id"]),
    )

    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "user-99",
            "channel": "telegram",
            "message": "Call tool",
        },
    )

    assert response.status_code == 200
    assert mcp_controller.tool_calls[0]["tool_args"] == {}


@pytest.mark.integration
async def test_chat_skips_unsupported_service_credentials_when_mcp_raises_value_error(
    api_client,
    db_session,
    llm_controller,
    mcp_controller,
) -> None:
    mcp_controller.enter_errors["custom"] = ValueError("Unsupported MCP service: custom")
    llm_controller.chat.completions.queue(build_llm_response(content="No tools needed"))
    agent = await create_agent(api_client, db_session_factory=db_session)
    await store_credentials(
        api_client,
        agent["id"],
        "custom",
        {"token": "tenant-secret"},
        owner_id=UUID(agent["user_id"]),
    )

    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "user-1",
            "channel": "telegram",
            "message": "Hello",
        },
    )

    assert response.status_code == 200
    assert llm_controller.chat.completions.calls[0]["tools"] is None


@pytest.mark.integration
async def test_chat_skips_bitrix_credentials_without_webhook_url(
    api_client,
    db_session,
    llm_controller,
    mcp_controller,
) -> None:
    mcp_controller.enter_errors["bitrix"] = ValueError("Bitrix credentials must contain 'webhook_url'.")
    llm_controller.chat.completions.queue(build_llm_response(content="Proceed without tools"))
    agent = await create_agent(api_client, db_session_factory=db_session)
    await store_credentials(
        api_client,
        agent["id"],
        "bitrix",
        {"token": "incomplete"},
        owner_id=UUID(agent["user_id"]),
    )

    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "user-2",
            "channel": "telegram",
            "message": "Hello",
        },
    )

    assert response.status_code == 200
    assert llm_controller.chat.completions.calls[0]["tools"] is None


@pytest.mark.integration
async def test_chat_returns_404_for_missing_agent(api_client, llm_controller) -> None:
    llm_controller.chat.completions.queue(build_llm_response(content="unused"))

    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": str(uuid4()),
            "external_client_id": "user-404",
            "channel": "telegram",
            "message": "Hello",
        },
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Agent not found."}


@pytest.mark.integration
async def test_chat_returns_409_for_inactive_agent(api_client, llm_controller) -> None:
    llm_controller.chat.completions.queue(build_llm_response(content="unused"))
    agent = await create_agent(api_client, is_active=False)

    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "user-409",
            "channel": "telegram",
            "message": "Hello",
        },
    )

    assert response.status_code == 409
    assert response.json() == {"detail": "Agent is inactive."}


@pytest.mark.integration
async def test_chat_ignores_redis_load_failures(api_client, db_session, llm_controller, fake_redis, monkeypatch) -> None:
    from redis.exceptions import RedisError

    llm_controller.chat.completions.queue(build_llm_response(content="No history loaded"))
    agent = await create_agent(api_client, db_session_factory=db_session)

    async def broken_get(*args, **kwargs):
        raise RedisError("read failure")

    monkeypatch.setattr(fake_redis, "get", broken_get)
    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "user-redis-load",
            "channel": "telegram",
            "message": "Hello",
        },
    )

    assert response.status_code == 200
    assert llm_controller.chat.completions.calls[0]["messages"] == [
        {"role": "system", "content": "Be concise and helpful."},
        {"role": "user", "content": "Hello"},
    ]


@pytest.mark.integration
async def test_chat_ignores_redis_save_failures(api_client, db_session, llm_controller, fake_redis, monkeypatch) -> None:
    from redis.exceptions import RedisError

    llm_controller.chat.completions.queue(build_llm_response(content="History save failed"))
    agent = await create_agent(api_client, db_session_factory=db_session)

    async def broken_set(*args, **kwargs):
        raise RedisError("write failure")

    monkeypatch.setattr(fake_redis, "set", broken_set)
    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "user-redis-save",
            "channel": "telegram",
            "message": "Hello",
        },
    )

    assert response.status_code == 200
    assert response.json()["answer"] == "History save failed"
    assert await fake_redis.keys("chat_history:*") == []


@pytest.mark.integration
async def test_identical_tool_call_is_blocked_to_prevent_infinite_loop(
    api_client,
    db_session,
    llm_controller,
    mcp_controller,
) -> None:
    mcp_controller.tools_by_service["bitrix"] = [
        {
            "type": "function",
            "function": {
                "name": "find_contact",
                "description": "Find a contact",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    mcp_controller.tool_results["find_contact"] = build_tool_result('{"contact_id": 42}')
    llm_controller.chat.completions.queue(
        build_llm_response(
            tool_calls=[build_tool_call("tool-1", "find_contact", '{"phone":"+123"}')]
        ),
        build_llm_response(
            tool_calls=[build_tool_call("tool-2", "find_contact", '{"phone":"+123"}')]
        ),
        build_llm_response(content="Stopped looping"),
    )
    agent = await create_agent(api_client, db_session_factory=db_session)
    await store_credentials(
        api_client,
        agent["id"],
        "bitrix",
        {"webhook_url": "https://example.bitrix/rest"},
        owner_id=UUID(agent["user_id"]),
    )

    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "loop-user",
            "channel": "telegram",
            "message": "Repeat tool",
        },
    )

    assert response.status_code == 200
    blocked_message = llm_controller.chat.completions.calls[2]["messages"][-1]
    assert blocked_message["role"] == "tool"
    assert "identical tool call blocked to prevent an infinite loop" in blocked_message["content"]


@pytest.mark.integration
async def test_loop_exhaustion_returns_422_when_tool_calls_never_finish(
    api_client,
    db_session,
    llm_controller,
    mcp_controller,
) -> None:
    mcp_controller.tools_by_service["bitrix"] = [
        {
            "type": "function",
            "function": {
                "name": "find_contact",
                "description": "Find a contact",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    for index in range(20):
        llm_controller.chat.completions.queue(
            build_llm_response(
                tool_calls=[
                    build_tool_call(
                        f"tool-{index}",
                        "find_contact",
                        json.dumps({"iteration": index}),
                    )
                ]
            )
        )
    agent = await create_agent(api_client, db_session_factory=db_session)
    await store_credentials(
        api_client,
        agent["id"],
        "bitrix",
        {"webhook_url": "https://example.bitrix/rest"},
        owner_id=UUID(agent["user_id"]),
    )

    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "loop-user",
            "channel": "telegram",
            "message": "Never stop",
        },
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Maximum number of LLM tool steps reached."}
    assert len(mcp_controller.tool_calls) == 20


@pytest.mark.diagnostic
async def test_concurrent_first_messages_can_create_duplicate_sessions(
    api_client,
    db_session,
    llm_controller,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm_controller.chat.completions.queue(
        build_llm_response(content="First"),
        build_llm_response(content="Second"),
    )
    agent = await create_agent(api_client, db_session_factory=db_session)
    gate = asyncio.Event()
    state = {"seen": 0}

    async def racing_get_or_create(self, session, agent_id, external_client_id, channel):
        result = await session.execute(
            select(ConversationSession).where(
                ConversationSession.agent_id == agent_id,
                ConversationSession.external_client_id == external_client_id,
                ConversationSession.channel == channel,
            )
        )
        conversation_session = result.scalar_one_or_none()
        if conversation_session is not None:
            return conversation_session

        state["seen"] += 1
        if state["seen"] == 2:
            gate.set()
        await gate.wait()

        duplicate_session = ConversationSession(
            agent_id=agent_id,
            external_client_id=external_client_id,
            channel=channel,
        )
        session.add(duplicate_session)
        await session.commit()
        await session.refresh(duplicate_session)
        return duplicate_session

    monkeypatch.setattr(
        llm_service_module.SessionService,
        "get_or_create_session",
        racing_get_or_create,
    )

    async def send_message(text: str):
        return await api_client.post(
            "/v1/chat/completions",
            json={
                "agent_id": agent["id"],
                "external_client_id": "racy-user",
                "channel": "telegram",
                "message": text,
            },
        )

    first, second = await asyncio.gather(send_message("Hello"), send_message("Hi"))

    assert first.status_code == 200
    assert second.status_code == 200
    async with db_session() as session:
        count_result = await session.execute(select(func.count()).select_from(ConversationSession))
        assert count_result.scalar_one() == 2


@pytest.mark.integration
async def test_multiple_credentials_are_loaded_for_chat_flow(
    api_client,
    db_session,
    llm_controller,
    mcp_controller,
) -> None:
    mcp_controller.tools_by_service["bitrix"] = []
    mcp_controller.tools_by_service["custom"] = []
    llm_controller.chat.completions.queue(build_llm_response(content="No tools used"))
    agent = await create_agent(api_client, db_session_factory=db_session)
    await store_credentials(
        api_client,
        agent["id"],
        "bitrix",
        {"webhook_url": "https://example.bitrix/rest"},
        owner_id=UUID(agent["user_id"]),
    )
    await store_credentials(
        api_client,
        agent["id"],
        "custom",
        {"token": "tenant-secret"},
        owner_id=UUID(agent["user_id"]),
    )

    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "user-3",
            "channel": "telegram",
            "message": "Hello",
        },
    )

    assert response.status_code == 200
    async with db_session() as session:
        credentials = await session.execute(
            select(AgentCredentials).where(AgentCredentials.agent_id == UUID(agent["id"]))
        )
        assert {item.service_name for item in credentials.scalars().all()} == {"bitrix", "custom"}
    assert [call["service_name"] for call in mcp_controller.init_calls] == ["bitrix", "custom"]


@pytest.mark.integration
async def test_chat_without_key_returns_400(api_client, llm_controller) -> None:
    """Agent with is_active=True but no key should get 400."""
    # Create agent with is_active=True but no key in DB
    owner_id = uuid4()
    payload = build_agent_payload()
    response = await api_client.post("/v1/agents/", json=payload, headers=build_headers(owner_id))
    assert response.status_code == 201
    agent = response.json()

    chat_response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "user-nokey",
            "channel": "telegram",
            "message": "Hello",
        },
    )

    assert chat_response.status_code == 400
    assert "OpenRouter API key" in chat_response.json()["detail"]


@pytest.mark.integration
async def test_chat_authentication_error_deactivates_agent_returns_403(
    api_client, db_session, monkeypatch
) -> None:
    import httpx
    from openai import AuthenticationError

    mock_response = httpx.Response(401, request=httpx.Request("POST", "https://openrouter.ai"))
    controller = ScriptedLLMClient()
    controller.chat.completions.queue(
        AuthenticationError(
            message="Invalid API key",
            response=mock_response,
            body=None,
        )
    )
    monkeypatch.setattr(llm_service_module, "AsyncOpenAI", lambda **kwargs: controller)
    agent = await create_agent(api_client, db_session_factory=db_session)

    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "user-auth-err",
            "channel": "telegram",
            "message": "Hello",
        },
    )

    assert response.status_code == 403
    assert "invalid" in response.json()["detail"].lower()

    # Verify agent is deactivated
    async with db_session() as session:
        agent_db = await session.get(AgentTemplate, UUID(agent["id"]))
    assert agent_db.is_active is False


@pytest.mark.integration
async def test_chat_rate_limit_with_402_deactivates_agent(
    api_client, db_session, monkeypatch
) -> None:
    import httpx
    from openai import RateLimitError

    mock_response = httpx.Response(402, request=httpx.Request("POST", "https://openrouter.ai"))
    controller = ScriptedLLMClient()
    controller.chat.completions.queue(
        RateLimitError(
            message="402 insufficient credits",
            response=mock_response,
            body=None,
        )
    )
    monkeypatch.setattr(llm_service_module, "AsyncOpenAI", lambda **kwargs: controller)
    agent = await create_agent(api_client, db_session_factory=db_session)

    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "user-402",
            "channel": "telegram",
            "message": "Hello",
        },
    )

    assert response.status_code == 402

    async with db_session() as session:
        agent_db = await session.get(AgentTemplate, UUID(agent["id"]))
    assert agent_db.is_active is False


@pytest.mark.integration
async def test_chat_rate_limit_normal_returns_429_without_deactivation(
    api_client, db_session, monkeypatch
) -> None:
    import httpx
    from openai import RateLimitError

    mock_response = httpx.Response(429, request=httpx.Request("POST", "https://openrouter.ai"))
    controller = ScriptedLLMClient()
    controller.chat.completions.queue(
        RateLimitError(
            message="Rate limit exceeded",
            response=mock_response,
            body=None,
        )
    )
    monkeypatch.setattr(llm_service_module, "AsyncOpenAI", lambda **kwargs: controller)
    agent = await create_agent(api_client, db_session_factory=db_session)

    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "user-429",
            "channel": "telegram",
            "message": "Hello",
        },
    )

    assert response.status_code == 429

    # Agent should NOT be deactivated
    async with db_session() as session:
        agent_db = await session.get(AgentTemplate, UUID(agent["id"]))
    assert agent_db.is_active is True


@pytest.mark.integration
async def test_chat_timeout_error_returns_422(
    api_client, db_session, monkeypatch
) -> None:
    from openai import APITimeoutError

    controller = ScriptedLLMClient()
    controller.chat.completions.queue(
        APITimeoutError(request=None)
    )
    monkeypatch.setattr(llm_service_module, "AsyncOpenAI", lambda **kwargs: controller)
    agent = await create_agent(api_client, db_session_factory=db_session)

    response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "user-timeout",
            "channel": "telegram",
            "message": "Hello",
        },
    )

    assert response.status_code == 422
