import json
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from models.agent import AgentCredentials, AgentTemplate
from services.bitrix_webhook_validator import (
    BitrixCredentialsValidationResult,
    BitrixCredentialsValidationStatus,
)
from services.google_workspace_validator import (
    GoogleCredentialsValidationResult,
    GoogleCredentialsValidationStatus,
)


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
async def test_upsert_bitrix_credentials_missing_agent_returns_404_before_validation(
    api_client,
    monkeypatch,
) -> None:
    import api.v1.agents as agents_module

    async def fail_if_called(_: dict[str, object]) -> BitrixCredentialsValidationResult:
        raise AssertionError("Bitrix validation must not run for missing agent.")

    monkeypatch.setattr(agents_module, "validate_bitrix_credentials", fail_if_called)

    owner_id = uuid4()
    response = await api_client.put(
        f"/v1/agents/{uuid4()}/credentials",
        json={
            "service_name": "bitrix",
            "credentials": {"webhook_url": "https://portal.bitrix24.com/rest/1/secret123/"},
        },
        headers=build_headers(owner_id),
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Agent not found"}


@pytest.mark.integration
async def test_upsert_bitrix_credentials_foreign_agent_returns_404_before_validation(
    api_client,
    monkeypatch,
) -> None:
    import api.v1.agents as agents_module

    async def fail_if_called(_: dict[str, object]) -> BitrixCredentialsValidationResult:
        raise AssertionError("Bitrix validation must not run for foreign agent.")

    monkeypatch.setattr(agents_module, "validate_bitrix_credentials", fail_if_called)

    owner_id = uuid4()
    create_response = await api_client.post(
        "/v1/agents/",
        json=build_agent_payload(),
        headers=build_headers(owner_id),
    )
    agent_id = create_response.json()["id"]

    response = await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={
            "service_name": "bitrix",
            "credentials": {"webhook_url": "https://portal.bitrix24.com/rest/1/secret123/"},
        },
        headers=build_headers(uuid4()),
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Agent not found"}


@pytest.mark.integration
async def test_upsert_bitrix_credentials_requires_webhook_url_before_validation(
    api_client,
    monkeypatch,
) -> None:
    import api.v1.agents as agents_module

    async def fail_if_called(_: dict[str, object]) -> BitrixCredentialsValidationResult:
        raise AssertionError("Bitrix validation must not run when webhook_url is missing.")

    monkeypatch.setattr(agents_module, "validate_bitrix_credentials", fail_if_called)

    owner_id = uuid4()
    create_response = await api_client.post(
        "/v1/agents/",
        json=build_agent_payload(),
        headers=build_headers(owner_id),
    )
    agent_id = create_response.json()["id"]

    response = await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={"service_name": "bitrix", "credentials": {"portal_url": "https://portal.bitrix24.com"}},
        headers=build_headers(owner_id),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "webhook_url is required for Bitrix credentials."


@pytest.mark.integration
async def test_upsert_bitrix_credentials_returns_422_on_invalid_validator_result(
    api_client,
    monkeypatch,
) -> None:
    import api.v1.agents as agents_module

    async def mock_validate(_: dict[str, object]) -> BitrixCredentialsValidationResult:
        return BitrixCredentialsValidationResult(
            status=BitrixCredentialsValidationStatus.INVALID_WEBHOOK,
            details="invalid webhook",
        )

    monkeypatch.setattr(agents_module, "validate_bitrix_credentials", mock_validate)

    owner_id = uuid4()
    create_response = await api_client.post(
        "/v1/agents/",
        json=build_agent_payload(),
        headers=build_headers(owner_id),
    )
    agent_id = create_response.json()["id"]

    response = await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={
            "service_name": "bitrix",
            "credentials": {"webhook_url": "https://portal.bitrix24.com/rest/1/secret123/"},
        },
        headers=build_headers(owner_id),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "invalid webhook"


@pytest.mark.integration
async def test_upsert_bitrix_credentials_returns_502_on_upstream_validator_result(
    api_client,
    monkeypatch,
) -> None:
    import api.v1.agents as agents_module

    async def mock_validate(_: dict[str, object]) -> BitrixCredentialsValidationResult:
        return BitrixCredentialsValidationResult(
            status=BitrixCredentialsValidationStatus.RATE_LIMITED,
            details="Could not validate Bitrix webhook due to upstream issue.",
        )

    monkeypatch.setattr(agents_module, "validate_bitrix_credentials", mock_validate)

    owner_id = uuid4()
    create_response = await api_client.post(
        "/v1/agents/",
        json=build_agent_payload(),
        headers=build_headers(owner_id),
    )
    agent_id = create_response.json()["id"]

    response = await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={
            "service_name": "bitrix",
            "credentials": {"webhook_url": "https://portal.bitrix24.com/rest/1/secret123/"},
        },
        headers=build_headers(owner_id),
    )

    assert response.status_code == 502
    assert response.json()["detail"] == "Could not validate Bitrix webhook due to upstream issue."


@pytest.mark.integration
async def test_upsert_bitrix_credentials_persists_normalized_webhook_url(
    api_client,
    db_session,
    monkeypatch,
) -> None:
    import api.v1.agents as agents_module

    async def mock_validate(_: dict[str, object]) -> BitrixCredentialsValidationResult:
        return BitrixCredentialsValidationResult(
            status=BitrixCredentialsValidationStatus.VALID,
            normalized_credentials={
                "webhook_url": "https://portal.bitrix24.com/rest/1/secret123/",
                "portal_url": "https://portal.bitrix24.com",
            },
        )

    monkeypatch.setattr(agents_module, "validate_bitrix_credentials", mock_validate)

    owner_id = uuid4()
    create_response = await api_client.post(
        "/v1/agents/",
        json=build_agent_payload(),
        headers=build_headers(owner_id),
    )
    agent_id = create_response.json()["id"]

    response = await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={
            "service_name": "bitrix",
            "credentials": {
                "webhook_url": "https://portal.bitrix24.com/rest/1/secret123/crm.lead.add.json?ID=42",
                "portal_url": "https://portal.bitrix24.com/",
            },
        },
        headers=build_headers(owner_id),
    )

    assert response.status_code == 200
    async with db_session() as session:
        record = await session.get(AgentCredentials, {"agent_id": UUID(agent_id), "service_name": "bitrix"})
    assert record is not None
    assert record.credentials["webhook_url"] == "https://portal.bitrix24.com/rest/1/secret123/"
    assert record.credentials["portal_url"] == "https://portal.bitrix24.com"


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
        },
        {
            "service_name": "google_workspace",
            "display_name": "Google Workspace",
            "description": "Интеграция с Google Sheets, Calendar, Gmail, Drive и Contacts",
            "required_keys": ["client_id", "client_secret", "refresh_token", "scopes"],
        },
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


# ── Google Workspace credential tests ──────────────────────────────


def build_google_credentials() -> dict[str, object]:
    return {
        "service_name": "google_workspace",
        "credentials": {
            "client_id": "test-client-id.apps.googleusercontent.com",
            "client_secret": "GOCSPX-test-secret-123",
            "refresh_token": "1//test-refresh-token-abc",
            "scopes": "https://www.googleapis.com/auth/spreadsheets https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/gmail.readonly",
        },
    }


@pytest.mark.integration
async def test_upsert_google_credentials_returns_safe_response_no_secrets(api_client, db_session) -> None:
    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    response = await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json=build_google_credentials(),
        headers=build_headers(owner_id),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["agent_id"] == agent_id
    assert data["service_name"] == "google_workspace"
    assert data["has_credentials"] is True
    assert "credential_keys" in data
    assert "credentials" not in data

    sensitive_keys = {"client_secret", "refresh_token"}
    for key in sensitive_keys:
        assert key not in data


@pytest.mark.integration
async def test_upsert_google_credentials_persists_in_db(api_client, db_session) -> None:
    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json=build_google_credentials(),
        headers=build_headers(owner_id),
    )

    async with db_session() as session:
        record = await session.get(AgentCredentials, {"agent_id": UUID(agent_id), "service_name": "google_workspace"})
    assert record is not None
    assert record.credentials["client_id"] == "test-client-id.apps.googleusercontent.com"
    assert record.credentials["client_secret"] == "GOCSPX-test-secret-123"
    assert record.credentials["refresh_token"] == "1//test-refresh-token-abc"


@pytest.mark.integration
async def test_upsert_google_credentials_missing_required_field_returns_422(api_client, monkeypatch) -> None:
    import api.v1.agents as agents_module

    monkeypatch.undo()

    from services.google_workspace_validator import validate_google_workspace_credentials as real_validate

    async def always_pass_check(_cid, _csec, _rt):
        return True

    monkeypatch.setattr(agents_module, "validate_google_workspace_credentials", real_validate)
    import services.google_workspace_validator as gv_module
    monkeypatch.setattr(gv_module, "check_refresh_token", always_pass_check)
    monkeypatch.setattr(gv_module, "_default_refresh_check", always_pass_check)

    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    response = await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={
            "service_name": "google_workspace",
            "credentials": {
                "client_id": "test.apps.googleusercontent.com",
            },
        },
        headers=build_headers(owner_id),
    )

    assert response.status_code == 422
    assert "Missing required fields" in response.json()["detail"]


@pytest.mark.integration
async def test_upsert_google_credentials_invalid_scopes_returns_422(api_client, monkeypatch) -> None:
    import api.v1.agents as agents_module

    monkeypatch.undo()

    from services.google_workspace_validator import validate_google_workspace_credentials as real_validate

    async def always_pass_check(_cid, _csec, _rt):
        return True

    monkeypatch.setattr(agents_module, "validate_google_workspace_credentials", real_validate)
    import services.google_workspace_validator as gv_module
    monkeypatch.setattr(gv_module, "check_refresh_token", always_pass_check)
    monkeypatch.setattr(gv_module, "_default_refresh_check", always_pass_check)

    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    response = await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={
            "service_name": "google_workspace",
            "credentials": {
                "client_id": "test.apps.googleusercontent.com",
                "client_secret": "GOCSPX-secret",
                "refresh_token": "1//refresh",
                "scopes": "https://www.googleapis.com/auth/bad.scope",
            },
        },
        headers=build_headers(owner_id),
    )

    assert response.status_code == 422
    assert "Unsupported scopes" in response.json()["detail"]


@pytest.mark.integration
async def test_upsert_google_credentials_unknown_field_returns_422(api_client, monkeypatch) -> None:
    import api.v1.agents as agents_module

    monkeypatch.undo()

    from services.google_workspace_validator import validate_google_workspace_credentials as real_validate

    async def always_pass_check(_cid, _csec, _rt):
        return True

    monkeypatch.setattr(agents_module, "validate_google_workspace_credentials", real_validate)
    import services.google_workspace_validator as gv_module
    monkeypatch.setattr(gv_module, "check_refresh_token", always_pass_check)
    monkeypatch.setattr(gv_module, "_default_refresh_check", always_pass_check)

    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    response = await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={
            "service_name": "google_workspace",
            "credentials": {
                "client_id": "test.apps.googleusercontent.com",
                "client_secret": "GOCSPX-secret",
                "refresh_token": "1//refresh",
                "private_key": "-----BEGIN PRIVATE KEY-----",
            },
        },
        headers=build_headers(owner_id),
    )

    assert response.status_code == 422
    assert "Unknown credential fields" in response.json()["detail"]


@pytest.mark.integration
async def test_mixed_bitrix_and_google_credentials_for_same_agent(api_client, db_session) -> None:
    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

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
        json=build_google_credentials(),
        headers=build_headers(owner_id),
    )

    list_resp = await api_client.get(
        f"/v1/agents/{agent_id}/credentials", headers=build_headers(owner_id)
    )
    assert list_resp.status_code == 200
    service_names = {item["service_name"] for item in list_resp.json()}
    assert service_names == {"bitrix", "google_workspace"}

    async with db_session() as session:
        bitrix_record = await session.get(AgentCredentials, {"agent_id": UUID(agent_id), "service_name": "bitrix"})
        google_record = await session.get(AgentCredentials, {"agent_id": UUID(agent_id), "service_name": "google_workspace"})
    assert bitrix_record is not None
    assert google_record is not None
    assert bitrix_record.credentials.get("webhook_url") is not None
    assert google_record.credentials.get("client_id") is not None


@pytest.mark.integration
async def test_put_credentials_does_not_return_secrets_in_response(api_client) -> None:
    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    response = await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={
            "service_name": "bitrix",
            "credentials": {"webhook_url": "https://secret-bitrix.bitrix24.ru/rest/1/token/"},
        },
        headers=build_headers(owner_id),
    )

    assert response.status_code == 200
    data = response.json()
    assert "credentials" not in data
    assert data["has_credentials"] is True
    assert "credential_keys" in data
