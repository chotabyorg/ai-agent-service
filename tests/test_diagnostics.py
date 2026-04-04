from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from redis.exceptions import RedisError

import main
import services.llm_service as llm_service_module
from models.agent import AgentTemplate
from services.encryption_service import EncryptionService
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
async def test_openai_provider_exception_results_in_http_500(
    api_client_no_raise,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = ScriptedLLMClient()
    controller.chat.completions.queue(RuntimeError("provider down"))
    monkeypatch.setattr(llm_service_module, "AsyncOpenAI", lambda **kwargs: controller)
    agent = await create_agent(api_client_no_raise, db_session_factory=db_session)

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
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller = ScriptedLLMClient()
    controller.chat.completions.queue(build_llm_response(content="unused"))
    monkeypatch.setattr(llm_service_module, "AsyncOpenAI", lambda **kwargs: controller)

    mcp_controller = FakeMCPController(
        enter_errors={"bitrix": RuntimeError("mcp init failed")},
    )
    monkeypatch.setattr(llm_service_module, "MCPManager", mcp_controller.make_manager_class())

    owner_id = uuid4()
    agent = await create_agent(api_client_no_raise, db_session_factory=db_session, owner_id=owner_id)
    credentials_response = await api_client_no_raise.put(
        f"/v1/agents/{agent['id']}/credentials",
        json={
            "service_name": "bitrix",
            "credentials": {"webhook_url": "https://example.bitrix/rest"},
        },
        headers=build_headers(owner_id),
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
