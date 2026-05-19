import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from models.agent import AgentCredentials, AgentTemplate


def build_agent_payload() -> dict[str, object]:
    return {
        "name": "Support agent",
        "system_prompt": "Be concise and helpful.",
        "model_name": "google/gemini-2.5-flash",
        "temperature": 0.4,
        "is_active": True,
    }


def build_headers(user_id: UUID | None = None) -> dict[str, str]:
    return {"x-user-id": str(user_id or uuid4())}


@pytest.mark.integration
async def test_create_agent_persists_owner_from_header_and_default_provider(
    api_client,
    db_session,
) -> None:
    owner_id = uuid4()
    payload = build_agent_payload()

    response = await api_client.post("/v1/agents/", json=payload, headers=build_headers(owner_id))

    assert response.status_code == 201
    data = response.json()
    async with db_session() as session:
        agent = await session.get(AgentTemplate, UUID(data["id"]))
    assert agent is not None
    assert data["name"] == payload["name"]
    assert data["llm_provider"] == "openrouter"
    assert str(agent.user_id) == str(owner_id)


@pytest.mark.integration
async def test_list_agents_returns_only_owned_agents(api_client) -> None:
    first_owner = uuid4()
    second_owner = uuid4()

    await api_client.post("/v1/agents/", json=build_agent_payload(), headers=build_headers(first_owner))
    await api_client.post(
        "/v1/agents/",
        json={**build_agent_payload(), "name": "Private agent"},
        headers=build_headers(second_owner),
    )

    response = await api_client.get("/v1/agents/", headers=build_headers(first_owner))

    assert response.status_code == 200
    assert [agent["name"] for agent in response.json()] == ["Support agent"]


@pytest.mark.integration
async def test_get_agent_returns_persisted_configuration_for_owner(api_client) -> None:
    owner_id = uuid4()
    create_response = await api_client.post(
        "/v1/agents/",
        json=build_agent_payload(),
        headers=build_headers(owner_id),
    )
    agent_id = create_response.json()["id"]

    response = await api_client.get(f"/v1/agents/{agent_id}", headers=build_headers(owner_id))

    assert response.status_code == 200
    assert response.json()["id"] == agent_id


@pytest.mark.integration
async def test_get_agent_returns_404_for_missing_or_foreign_agent(api_client) -> None:
    owner_id = uuid4()
    foreign_owner = uuid4()
    create_response = await api_client.post(
        "/v1/agents/",
        json=build_agent_payload(),
        headers=build_headers(owner_id),
    )
    agent_id = create_response.json()["id"]

    missing_response = await api_client.get(f"/v1/agents/{uuid4()}", headers=build_headers(owner_id))
    foreign_response = await api_client.get(f"/v1/agents/{agent_id}", headers=build_headers(foreign_owner))

    assert missing_response.status_code == 404
    assert foreign_response.status_code == 404
    assert missing_response.json() == {"detail": "Agent not found"}
    assert foreign_response.json() == {"detail": "Agent not found"}


@pytest.mark.integration
async def test_update_agent_partially_updates_only_owned_agent(api_client, db_session) -> None:
    owner_id = uuid4()
    create_response = await api_client.post(
        "/v1/agents/",
        json=build_agent_payload(),
        headers=build_headers(owner_id),
    )
    agent_id = create_response.json()["id"]

    response = await api_client.put(
        f"/v1/agents/{agent_id}",
        json={"name": "Sales agent", "temperature": 0.9},
        headers=build_headers(owner_id),
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


@pytest.mark.integration
async def test_update_agent_returns_404_for_foreign_owner(api_client) -> None:
    owner_id = uuid4()
    create_response = await api_client.post(
        "/v1/agents/",
        json=build_agent_payload(),
        headers=build_headers(owner_id),
    )
    agent_id = create_response.json()["id"]

    response = await api_client.put(
        f"/v1/agents/{agent_id}",
        json={"name": "Hijacked"},
        headers=build_headers(uuid4()),
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Agent not found"}


@pytest.mark.integration
async def test_soft_delete_disables_agent_and_blocks_chat(api_client, db_session, fake_redis) -> None:
    owner_id = uuid4()
    create_response = await api_client.post(
        "/v1/agents/",
        json=build_agent_payload(),
        headers=build_headers(owner_id),
    )
    agent_id = create_response.json()["id"]

    update_response = await api_client.put(
        f"/v1/agents/{agent_id}",
        json={"is_active": False},
        headers=build_headers(owner_id),
    )
    get_response = await api_client.get(f"/v1/agents/{agent_id}", headers=build_headers(owner_id))
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
    assert get_response.status_code == 200
    assert get_response.json()["is_active"] is False
    assert chat_response.status_code == 409
    assert await fake_redis.keys("chat_history:*") == []

    async with db_session() as session:
        stored_agent = await session.get(AgentTemplate, UUID(agent_id))
    assert stored_agent is not None
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
async def test_agent_validation_errors_return_422(
    api_client,
    method: str,
    path: str,
    payload,
) -> None:
    headers = build_headers()
    if payload is None:
        response = await getattr(api_client, method)(path, headers=headers)
    else:
        response = await getattr(api_client, method)(path, json=payload, headers=headers)

    assert response.status_code == 422


@pytest.mark.integration
async def test_missing_x_user_id_header_returns_422(api_client) -> None:
    response = await api_client.post("/v1/agents/", json=build_agent_payload())

    assert response.status_code == 422


@pytest.mark.integration
async def test_upsert_credentials_creates_and_updates_single_owned_record(api_client, db_session) -> None:
    owner_id = uuid4()
    create_response = await api_client.post(
        "/v1/agents/",
        json=build_agent_payload(),
        headers=build_headers(owner_id),
    )
    agent_id = create_response.json()["id"]

    create_credentials = await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={
            "service_name": "bitrix",
            "credentials": {"webhook_url": "https://example.bitrix/rest"},
        },
        headers=build_headers(owner_id),
    )
    update_credentials = await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={
            "service_name": "bitrix",
            "credentials": {"webhook_url": "https://example.bitrix/rest/updated"},
        },
        headers=build_headers(owner_id),
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
async def test_upsert_credentials_returns_404_for_missing_or_foreign_agent(api_client) -> None:
    owner_id = uuid4()
    create_response = await api_client.post(
        "/v1/agents/",
        json=build_agent_payload(),
        headers=build_headers(owner_id),
    )
    agent_id = create_response.json()["id"]

    missing_response = await api_client.put(
        f"/v1/agents/{uuid4()}/credentials",
        json={
            "service_name": "bitrix",
            "credentials": {"webhook_url": "https://example.bitrix/rest"},
        },
        headers=build_headers(owner_id),
    )
    foreign_response = await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={
            "service_name": "bitrix",
            "credentials": {"webhook_url": "https://example.bitrix/rest"},
        },
        headers=build_headers(uuid4()),
    )

    assert missing_response.status_code == 404
    assert foreign_response.status_code == 404
    assert missing_response.json() == {"detail": "Agent not found"}
    assert foreign_response.json() == {"detail": "Agent not found"}


@pytest.mark.integration
async def test_multiple_credential_services_are_persisted_for_same_agent(api_client, db_session) -> None:
    owner_id = uuid4()
    create_response = await api_client.post(
        "/v1/agents/",
        json=build_agent_payload(),
        headers=build_headers(owner_id),
    )
    agent_id = create_response.json()["id"]

    await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={
            "service_name": "bitrix",
            "credentials": {"webhook_url": "https://example.bitrix/rest"},
        },
        headers=build_headers(owner_id),
    )
    await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={
            "service_name": "custom",
            "credentials": {"token": "tenant-secret"},
        },
        headers=build_headers(owner_id),
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


@pytest.mark.integration
async def test_mcp_service_discovery_returns_supported_integrations(api_client) -> None:
    response = await api_client.get("/v1/mcp-services")

    assert response.status_code == 200
    assert response.json() == [
        {
            "service_name": "bitrix",
            "display_name": "Bitrix24 CRM",
            "description": "Интеграция с вашей CRM Bitrix24",
            "required_keys": ["webhook_url"],
        }
    ]


@pytest.mark.integration
async def test_list_credentials_returns_summary_without_secrets(api_client) -> None:
    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={"service_name": "bitrix", "credentials": {"webhook_url": "https://secret.bitrix/rest"}},
        headers=build_headers(owner_id),
    )
    await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={"service_name": "amocrm", "credentials": {"token": "secret-token"}},
        headers=build_headers(owner_id),
    )

    response = await api_client.get(
        f"/v1/agents/{agent_id}/credentials", headers=build_headers(owner_id)
    )

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    service_names = {item["service_name"] for item in data}
    assert service_names == {"bitrix", "amocrm"}
    for item in data:
        assert item["agent_id"] == agent_id
        assert item["has_credentials"] is True
        assert "credentials" not in item


@pytest.mark.integration
async def test_list_credentials_returns_404_for_foreign_agent(api_client) -> None:
    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    stranger_id = uuid4()
    response = await api_client.get(
        f"/v1/agents/{agent_id}/credentials", headers=build_headers(stranger_id)
    )
    assert response.status_code == 404


@pytest.mark.integration
async def test_list_credentials_returns_empty_list_for_agent_without_credentials(api_client) -> None:
    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    response = await api_client.get(
        f"/v1/agents/{agent_id}/credentials", headers=build_headers(owner_id)
    )
    assert response.status_code == 200
    assert response.json() == []


@pytest.mark.integration
async def test_delete_credentials_removes_record(api_client, db_session) -> None:
    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={"service_name": "bitrix", "credentials": {"webhook_url": "https://example.bitrix/rest"}},
        headers=build_headers(owner_id),
    )

    delete_response = await api_client.delete(
        f"/v1/agents/{agent_id}/credentials/bitrix", headers=build_headers(owner_id)
    )
    assert delete_response.status_code == 204

    async with db_session() as session:
        record = await session.get(AgentCredentials, {"agent_id": UUID(agent_id), "service_name": "bitrix"})
    assert record is None


@pytest.mark.integration
async def test_delete_credentials_returns_404_for_missing(api_client) -> None:
    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    response = await api_client.delete(
        f"/v1/agents/{agent_id}/credentials/nonexistent", headers=build_headers(owner_id)
    )
    assert response.status_code == 404


@pytest.mark.integration
async def test_delete_credentials_returns_404_for_foreign_agent(api_client) -> None:
    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    stranger_id = uuid4()
    response = await api_client.delete(
        f"/v1/agents/{agent_id}/credentials/bitrix", headers=build_headers(stranger_id)
    )
    assert response.status_code == 404
