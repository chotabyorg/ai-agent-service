import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from models.agent import AgentCredentials, AgentTemplate


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


@pytest.mark.integration
async def test_create_agent_persists_all_fields(api_client, db_session) -> None:
    payload = build_agent_payload()

    response = await api_client.post("/v1/agents/", json=payload)

    assert response.status_code == 201
    data = response.json()
    async with db_session() as session:
        agent = await session.get(AgentTemplate, UUID(data["id"]))
    assert agent is not None
    assert data["name"] == payload["name"]
    assert data["system_prompt"] == payload["system_prompt"]
    assert data["llm_provider"] == payload["llm_provider"]
    assert data["model_name"] == payload["model_name"]
    assert data["temperature"] == payload["temperature"]
    assert data["is_active"] is True
    assert str(agent.user_id) == payload["user_id"]


@pytest.mark.integration
async def test_get_agent_returns_persisted_configuration(api_client) -> None:
    create_response = await api_client.post("/v1/agents/", json=build_agent_payload())
    agent_id = create_response.json()["id"]

    response = await api_client.get(f"/v1/agents/{agent_id}")

    assert response.status_code == 200
    assert response.json()["id"] == agent_id


@pytest.mark.integration
async def test_get_agent_returns_404_for_missing_agent(api_client) -> None:
    response = await api_client.get(f"/v1/agents/{uuid4()}")

    assert response.status_code == 404
    assert response.json() == {"detail": "Agent not found"}


@pytest.mark.integration
async def test_update_agent_partially_updates_only_supplied_fields(api_client, db_session) -> None:
    create_response = await api_client.post("/v1/agents/", json=build_agent_payload())
    agent_id = create_response.json()["id"]

    response = await api_client.put(
        f"/v1/agents/{agent_id}",
        json={"name": "Sales agent", "temperature": 0.9},
    )

    assert response.status_code == 200
    data = response.json()
    async with db_session() as session:
        agent = await session.get(AgentTemplate, UUID(agent_id))
    assert data["name"] == "Sales agent"
    assert data["temperature"] == 0.9
    assert data["system_prompt"] == "Be concise and helpful."
    assert agent is not None
    assert agent.model_name == "google/gemini-2.5-flash"
    assert agent.system_prompt == "Be concise and helpful."


@pytest.mark.integration
async def test_soft_delete_disables_agent_and_blocks_chat(api_client, db_session, fake_redis) -> None:
    create_response = await api_client.post("/v1/agents/", json=build_agent_payload())
    agent_id = create_response.json()["id"]

    update_response = await api_client.put(
        f"/v1/agents/{agent_id}",
        json={"is_active": False},
    )
    get_response = await api_client.get(f"/v1/agents/{agent_id}")
    chat_response = await api_client.post(
        "/v1/chat/completions",
        json={
            "agent_id": agent_id,
            "external_client_id": "telegram:42",
            "channel": "telegram",
            "message": "Hello",
        },
    )

    assert update_response.status_code == 200
    assert update_response.json()["is_active"] is False
    assert get_response.status_code == 200
    assert get_response.json()["is_active"] is False
    assert chat_response.status_code == 409
    assert await fake_redis.keys("chat_history:*") == []

    async with db_session() as session:
        sessions = await session.execute(select(AgentTemplate).where(AgentTemplate.id == UUID(agent_id)))
        stored_agent = sessions.scalar_one()
    assert stored_agent.is_active is False


@pytest.mark.integration
@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/v1/agents/", {**build_agent_payload(), "temperature": 2.5}),
        ("post", "/v1/agents/", {**build_agent_payload(), "name": ""}),
        ("put", f"/v1/agents/{uuid4()}", {"temperature": -0.1}),
        ("get", "/v1/agents/not-a-uuid", None),
    ],
)
async def test_agent_validation_errors_return_422(api_client, method: str, path: str, payload) -> None:
    response = await getattr(api_client, method)(path, json=payload) if payload is not None else await getattr(api_client, method)(path)

    assert response.status_code == 422


@pytest.mark.integration
async def test_upsert_credentials_creates_and_updates_single_record(api_client, db_session) -> None:
    create_response = await api_client.post("/v1/agents/", json=build_agent_payload())
    agent_id = create_response.json()["id"]

    create_credentials = await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={
            "service_name": "bitrix",
            "credentials": {"webhook_url": "https://example.bitrix/rest"},
        },
    )
    update_credentials = await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={
            "service_name": "bitrix",
            "credentials": {"webhook_url": "https://example.bitrix/rest/updated"},
        },
    )

    assert create_credentials.status_code == 200
    assert update_credentials.status_code == 200

    async with db_session() as session:
        records = await session.execute(
            select(AgentCredentials).where(AgentCredentials.agent_id == UUID(agent_id))
        )
        credentials = records.scalars().all()
    assert len(credentials) == 1
    assert credentials[0].credentials == {"webhook_url": "https://example.bitrix/rest/updated"}


@pytest.mark.integration
async def test_upsert_credentials_returns_404_for_missing_agent(api_client) -> None:
    response = await api_client.put(
        f"/v1/agents/{uuid4()}/credentials",
        json={
            "service_name": "bitrix",
            "credentials": {"webhook_url": "https://example.bitrix/rest"},
        },
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Agent not found"}


@pytest.mark.integration
async def test_multiple_credential_services_are_persisted_for_same_agent(api_client, db_session) -> None:
    create_response = await api_client.post("/v1/agents/", json=build_agent_payload())
    agent_id = create_response.json()["id"]

    await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={
            "service_name": "bitrix",
            "credentials": {"webhook_url": "https://example.bitrix/rest"},
        },
    )
    await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={
            "service_name": "custom",
            "credentials": {"token": "tenant-secret"},
        },
    )

    async with db_session() as session:
        records = await session.execute(
            select(AgentCredentials).where(AgentCredentials.agent_id == UUID(agent_id))
        )
        credentials = records.scalars().all()
    assert {(item.service_name, json.dumps(item.credentials, sort_keys=True)) for item in credentials} == {
        ("bitrix", json.dumps({"webhook_url": "https://example.bitrix/rest"}, sort_keys=True)),
        ("custom", json.dumps({"token": "tenant-secret"}, sort_keys=True)),
    }
