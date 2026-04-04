"""Tests for per-agent OpenRouter key management endpoints."""

from uuid import UUID, uuid4

import pytest

from models.agent import AgentTemplate
from services.encryption_service import EncryptionService
from services.openrouter_validator import KeyValidationResult


def build_agent_payload() -> dict[str, object]:
    return {
        "name": "Key test agent",
        "system_prompt": "Be helpful.",
        "model_name": "google/gemini-2.5-flash",
        "temperature": 0.4,
        "is_active": False,
    }


def build_headers(user_id: UUID | None = None) -> dict[str, str]:
    return {"x-user-id": str(user_id or uuid4())}


VALID_KEY = "sk-or-v1-valid-test-key-1234567890"


@pytest.mark.integration
async def test_put_key_with_valid_key_returns_200_and_preview(
    api_client, db_session, monkeypatch
) -> None:
    import api.v1.openrouter_key as key_module

    async def mock_validate(api_key: str) -> KeyValidationResult:
        return KeyValidationResult.VALID

    monkeypatch.setattr(key_module, "validate_openrouter_key", mock_validate)

    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    response = await api_client.put(
        f"/v1/agents/{agent_id}/openrouter-key",
        json={"api_key": VALID_KEY},
        headers=build_headers(owner_id),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["valid"] is True
    assert "***" in data["key_preview"]
    assert VALID_KEY not in data["key_preview"]

    # Verify key is encrypted in DB
    async with db_session() as session:
        agent = await session.get(AgentTemplate, UUID(agent_id))
    assert agent.encrypted_openrouter_key is not None
    assert agent.encrypted_openrouter_key != VALID_KEY


@pytest.mark.integration
async def test_put_key_with_invalid_key_returns_401(
    api_client, db_session, monkeypatch
) -> None:
    import api.v1.openrouter_key as key_module

    async def mock_validate(api_key: str) -> KeyValidationResult:
        return KeyValidationResult.INVALID

    monkeypatch.setattr(key_module, "validate_openrouter_key", mock_validate)

    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    response = await api_client.put(
        f"/v1/agents/{agent_id}/openrouter-key",
        json={"api_key": "sk-or-v1-bad-key"},
        headers=build_headers(owner_id),
    )

    assert response.status_code == 401


@pytest.mark.integration
async def test_put_key_with_insufficient_funds_returns_402(
    api_client, db_session, monkeypatch
) -> None:
    import api.v1.openrouter_key as key_module

    async def mock_validate(api_key: str) -> KeyValidationResult:
        return KeyValidationResult.INSUFFICIENT_FUNDS

    monkeypatch.setattr(key_module, "validate_openrouter_key", mock_validate)

    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    response = await api_client.put(
        f"/v1/agents/{agent_id}/openrouter-key",
        json={"api_key": "sk-or-v1-no-funds"},
        headers=build_headers(owner_id),
    )

    assert response.status_code == 402


@pytest.mark.integration
async def test_put_key_with_empty_key_returns_422(api_client, db_session) -> None:
    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    response = await api_client.put(
        f"/v1/agents/{agent_id}/openrouter-key",
        json={"api_key": ""},
        headers=build_headers(owner_id),
    )

    assert response.status_code == 422


@pytest.mark.integration
async def test_get_key_when_key_exists_returns_preview(
    api_client, db_session, monkeypatch
) -> None:
    import api.v1.openrouter_key as key_module

    async def mock_validate(api_key: str) -> KeyValidationResult:
        return KeyValidationResult.VALID

    monkeypatch.setattr(key_module, "validate_openrouter_key", mock_validate)

    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    await api_client.put(
        f"/v1/agents/{agent_id}/openrouter-key",
        json={"api_key": VALID_KEY},
        headers=build_headers(owner_id),
    )

    response = await api_client.get(
        f"/v1/agents/{agent_id}/openrouter-key",
        headers=build_headers(owner_id),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["has_key"] is True
    assert data["key_preview"] is not None
    assert VALID_KEY not in data["key_preview"]


@pytest.mark.integration
async def test_get_key_when_no_key_returns_false(api_client, db_session) -> None:
    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    response = await api_client.get(
        f"/v1/agents/{agent_id}/openrouter-key",
        headers=build_headers(owner_id),
    )

    assert response.status_code == 200
    data = response.json()
    assert data["has_key"] is False
    assert data["key_preview"] is None


@pytest.mark.integration
async def test_delete_key_removes_key_and_deactivates_agent(
    api_client, db_session, monkeypatch
) -> None:
    import api.v1.openrouter_key as key_module

    async def mock_validate(api_key: str) -> KeyValidationResult:
        return KeyValidationResult.VALID

    monkeypatch.setattr(key_module, "validate_openrouter_key", mock_validate)

    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    await api_client.put(
        f"/v1/agents/{agent_id}/openrouter-key",
        json={"api_key": VALID_KEY},
        headers=build_headers(owner_id),
    )

    response = await api_client.delete(
        f"/v1/agents/{agent_id}/openrouter-key",
        headers=build_headers(owner_id),
    )

    assert response.status_code == 204

    async with db_session() as session:
        agent = await session.get(AgentTemplate, UUID(agent_id))
    assert agent.encrypted_openrouter_key is None
    assert agent.is_active is False


@pytest.mark.integration
async def test_key_operations_return_404_for_foreign_agent(
    api_client, db_session
) -> None:
    owner_id = uuid4()
    stranger_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    get_resp = await api_client.get(
        f"/v1/agents/{agent_id}/openrouter-key",
        headers=build_headers(stranger_id),
    )
    put_resp = await api_client.put(
        f"/v1/agents/{agent_id}/openrouter-key",
        json={"api_key": VALID_KEY},
        headers=build_headers(stranger_id),
    )
    delete_resp = await api_client.delete(
        f"/v1/agents/{agent_id}/openrouter-key",
        headers=build_headers(stranger_id),
    )

    assert get_resp.status_code == 404
    assert put_resp.status_code == 404
    assert delete_resp.status_code == 404


@pytest.mark.integration
async def test_activate_agent_without_key_returns_400(
    api_client, db_session
) -> None:
    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    response = await api_client.put(
        f"/v1/agents/{agent_id}",
        json={"is_active": True},
        headers=build_headers(owner_id),
    )

    assert response.status_code == 400
    assert "OpenRouter API key" in response.json()["detail"]


@pytest.mark.integration
async def test_activate_agent_with_key_succeeds(
    api_client, db_session, monkeypatch
) -> None:
    import api.v1.openrouter_key as key_module

    async def mock_validate(api_key: str) -> KeyValidationResult:
        return KeyValidationResult.VALID

    monkeypatch.setattr(key_module, "validate_openrouter_key", mock_validate)

    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    # Set the key first
    await api_client.put(
        f"/v1/agents/{agent_id}/openrouter-key",
        json={"api_key": VALID_KEY},
        headers=build_headers(owner_id),
    )

    # Now activate
    response = await api_client.put(
        f"/v1/agents/{agent_id}",
        json={"is_active": True},
        headers=build_headers(owner_id),
    )

    assert response.status_code == 200
    assert response.json()["is_active"] is True


@pytest.mark.integration
async def test_agent_response_includes_key_info(
    api_client, db_session, monkeypatch
) -> None:
    import api.v1.openrouter_key as key_module

    async def mock_validate(api_key: str) -> KeyValidationResult:
        return KeyValidationResult.VALID

    monkeypatch.setattr(key_module, "validate_openrouter_key", mock_validate)

    owner_id = uuid4()
    create_resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = create_resp.json()["id"]

    # Before setting key
    get_resp = await api_client.get(
        f"/v1/agents/{agent_id}", headers=build_headers(owner_id)
    )
    assert get_resp.json()["has_openrouter_key"] is False
    assert get_resp.json()["openrouter_key_preview"] is None

    # Set the key
    await api_client.put(
        f"/v1/agents/{agent_id}/openrouter-key",
        json={"api_key": VALID_KEY},
        headers=build_headers(owner_id),
    )

    # After setting key
    get_resp = await api_client.get(
        f"/v1/agents/{agent_id}", headers=build_headers(owner_id)
    )
    assert get_resp.json()["has_openrouter_key"] is True
    assert get_resp.json()["openrouter_key_preview"] is not None
    assert VALID_KEY not in get_resp.json()["openrouter_key_preview"]
