from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import RedisError

import main
from api.v1 import chat as chat_api
import services.llm_service as llm_service_module
from tests.fakes import FakeMCPController, ScriptedLLMClient, build_llm_response


def build_agent_payload() -> dict[str, object]:
    return {
        "user_id": str(uuid4()),
        "name": "Support agent",
        "system_prompt": "Be concise and helpful.",
        "llm_provider": "openrouter",
        "model_name": "google/gemini-2.5-flash",
        "temperature": 0.4,
        "is_active": True,
    }


async def create_agent(api_client) -> dict[str, object]:
    response = await api_client.post("/v1/agents/", json=build_agent_payload())
    assert response.status_code == 201
    return response.json()


@pytest.mark.diagnostic
async def test_openai_provider_exception_results_in_http_500(
    api_client_no_raise,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = ScriptedLLMClient()
    controller.chat.completions.queue(RuntimeError("provider down"))
    monkeypatch.setattr(chat_api.llm_service, "llm", controller)
    agent = await create_agent(api_client_no_raise)

    response = await api_client_no_raise.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "user-openai-500",
            "channel": "telegram",
            "message": "Hello",
        },
    )

    assert response.status_code == 500


@pytest.mark.diagnostic
async def test_mcp_init_generic_exception_results_in_http_500(
    api_client_no_raise,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = ScriptedLLMClient()
    controller.chat.completions.queue(build_llm_response(content="unused"))
    monkeypatch.setattr(chat_api.llm_service, "llm", controller)

    mcp_controller = FakeMCPController(
        enter_errors={"bitrix": RuntimeError("mcp init failed")},
    )
    monkeypatch.setattr(llm_service_module, "MCPManager", mcp_controller.make_manager_class())

    agent = await create_agent(api_client_no_raise)
    credentials_response = await api_client_no_raise.put(
        f"/v1/agents/{agent['id']}/credentials",
        json={
            "service_name": "bitrix",
            "credentials": {"webhook_url": "https://example.bitrix/rest"},
        },
    )
    assert credentials_response.status_code == 200

    response = await api_client_no_raise.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent["id"],
            "external_client_id": "user-mcp-500",
            "channel": "telegram",
            "message": "Hello",
        },
    )

    assert response.status_code == 500


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
