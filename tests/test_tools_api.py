"""Tests for MCP tool filtering endpoints."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

FAKE_OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_contacts",
            "description": "Search contacts by name",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_contact",
            "description": "Get contact by ID",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "add_lead",
            "description": "Add a new lead",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


def _build_agent_payload() -> dict:
    return {
        "name": "Test agent",
        "system_prompt": "Be helpful.",
        "model_name": "google/gemini-2.5-flash",
        "temperature": 0.4,
    }


def _headers(user_id=None):
    return {"x-user-id": str(user_id or uuid4())}


def _patch_mcp_manager(monkeypatch):
    """Patch MCPManager in api.v1.tools so it returns FAKE_OPENAI_TOOLS."""
    manager_instance = MagicMock()
    manager_instance.get_openai_tools.return_value = FAKE_OPENAI_TOOLS
    manager_instance.__aenter__ = AsyncMock(return_value=manager_instance)
    manager_instance.__aexit__ = AsyncMock(return_value=None)

    mock_cls = MagicMock(return_value=manager_instance)
    monkeypatch.setattr("api.v1.tools.MCPManager", mock_cls)


@pytest.mark.integration
async def test_get_tools_returns_all_enabled_when_no_filter(api_client, monkeypatch) -> None:
    _patch_mcp_manager(monkeypatch)
    owner_id = uuid4()

    resp = await api_client.post("/v1/agents/", json=_build_agent_payload(), headers=_headers(owner_id))
    agent_id = resp.json()["id"]

    await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={"service_name": "bitrix", "credentials": {"webhook_url": "https://example.bitrix/rest"}},
        headers=_headers(owner_id),
    )

    resp = await api_client.get(f"/v1/agents/{agent_id}/tools/bitrix", headers=_headers(owner_id))

    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 3
    assert all(t["enabled"] is True for t in data)
    names = {t["name"] for t in data}
    assert names == {"search_contacts", "get_contact", "add_lead"}


@pytest.mark.integration
async def test_get_tools_returns_filtered_status(api_client, db_session, monkeypatch) -> None:
    _patch_mcp_manager(monkeypatch)
    owner_id = uuid4()

    resp = await api_client.post("/v1/agents/", json=_build_agent_payload(), headers=_headers(owner_id))
    agent_id = resp.json()["id"]

    await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={"service_name": "bitrix", "credentials": {"webhook_url": "https://example.bitrix/rest"}},
        headers=_headers(owner_id),
    )

    # Set enabled_tools directly in DB
    from uuid import UUID

    from models.agent import AgentCredentials

    async with db_session() as session:
        cred = await session.get(AgentCredentials, {"agent_id": UUID(agent_id), "service_name": "bitrix"})
        cred.enabled_tools = ["search_contacts", "add_lead"]
        await session.commit()

    resp = await api_client.get(f"/v1/agents/{agent_id}/tools/bitrix", headers=_headers(owner_id))

    assert resp.status_code == 200
    data = resp.json()
    status_map = {t["name"]: t["enabled"] for t in data}
    assert status_map == {
        "search_contacts": True,
        "get_contact": False,
        "add_lead": True,
    }


@pytest.mark.integration
async def test_put_tools_updates_enabled_list(api_client, monkeypatch) -> None:
    _patch_mcp_manager(monkeypatch)
    owner_id = uuid4()

    resp = await api_client.post("/v1/agents/", json=_build_agent_payload(), headers=_headers(owner_id))
    agent_id = resp.json()["id"]

    await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={"service_name": "bitrix", "credentials": {"webhook_url": "https://example.bitrix/rest"}},
        headers=_headers(owner_id),
    )

    put_resp = await api_client.put(
        f"/v1/agents/{agent_id}/tools/bitrix",
        json={"enabled_tools": ["get_contact"]},
        headers=_headers(owner_id),
    )

    assert put_resp.status_code == 200
    data = put_resp.json()
    status_map = {t["name"]: t["enabled"] for t in data}
    assert status_map == {
        "search_contacts": False,
        "get_contact": True,
        "add_lead": False,
    }

    # Verify GET returns the same
    get_resp = await api_client.get(f"/v1/agents/{agent_id}/tools/bitrix", headers=_headers(owner_id))
    assert get_resp.json() == put_resp.json()


@pytest.mark.integration
async def test_put_tools_null_enables_all(api_client, monkeypatch) -> None:
    _patch_mcp_manager(monkeypatch)
    owner_id = uuid4()

    resp = await api_client.post("/v1/agents/", json=_build_agent_payload(), headers=_headers(owner_id))
    agent_id = resp.json()["id"]

    await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={"service_name": "bitrix", "credentials": {"webhook_url": "https://example.bitrix/rest"}},
        headers=_headers(owner_id),
    )

    # First restrict
    await api_client.put(
        f"/v1/agents/{agent_id}/tools/bitrix",
        json={"enabled_tools": ["get_contact"]},
        headers=_headers(owner_id),
    )

    # Then enable all via null
    put_resp = await api_client.put(
        f"/v1/agents/{agent_id}/tools/bitrix",
        json={"enabled_tools": None},
        headers=_headers(owner_id),
    )

    assert put_resp.status_code == 200
    assert all(t["enabled"] is True for t in put_resp.json())


@pytest.mark.integration
async def test_get_tools_404_no_credentials(api_client, monkeypatch) -> None:
    _patch_mcp_manager(monkeypatch)
    owner_id = uuid4()

    resp = await api_client.post("/v1/agents/", json=_build_agent_payload(), headers=_headers(owner_id))
    agent_id = resp.json()["id"]

    resp = await api_client.get(f"/v1/agents/{agent_id}/tools/bitrix", headers=_headers(owner_id))

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Credentials not found"}


@pytest.mark.integration
async def test_get_tools_404_foreign_agent(api_client, monkeypatch) -> None:
    _patch_mcp_manager(monkeypatch)
    owner_id = uuid4()
    stranger_id = uuid4()

    resp = await api_client.post("/v1/agents/", json=_build_agent_payload(), headers=_headers(owner_id))
    agent_id = resp.json()["id"]

    await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={"service_name": "bitrix", "credentials": {"webhook_url": "https://example.bitrix/rest"}},
        headers=_headers(owner_id),
    )

    resp = await api_client.get(f"/v1/agents/{agent_id}/tools/bitrix", headers=_headers(stranger_id))

    assert resp.status_code == 404
    assert resp.json() == {"detail": "Agent not found"}
