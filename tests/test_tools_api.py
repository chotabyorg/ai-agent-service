"""Tests for MCP tool filtering endpoints."""

from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

FAKE_OPENAI_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_contact",
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

FAKE_TOOLS_METADATA = {
    "search_contact": {"category": "contacts", "tier": "core"},
    "get_contact": {"category": "contacts", "tier": "core"},
    "add_lead": {"category": "leads", "tier": "core"},
}

# Extended fake tool for mixed-tier tests
FAKE_EXTENDED_TOOL = {
    "type": "function",
    "function": {
        "name": "list_catalogs",
        "description": "List product catalogs",
        "parameters": {"type": "object", "properties": {}},
    },
}

FAKE_EXTENDED_METADATA = {
    "list_catalogs": {"category": "catalogs", "tier": "extended"},
}


def _build_agent_payload() -> dict:
    return {
        "name": "Test agent",
        "system_prompt": "Be helpful.",
        "model_name": "google/gemini-2.5-flash",
        "temperature": 0.4,
    }


def _headers(user_id=None):
    return {"x-user-id": str(user_id or uuid4())}


def _patch_mcp_manager(monkeypatch, *, include_extended: bool = False):
    """Patch MCPManager in api.v1.tools so it returns FAKE_OPENAI_TOOLS."""
    tools = list(FAKE_OPENAI_TOOLS)
    metadata = dict(FAKE_TOOLS_METADATA)
    if include_extended:
        tools = tools + [FAKE_EXTENDED_TOOL]
        metadata = {**metadata, **FAKE_EXTENDED_METADATA}

    manager_instance = MagicMock()
    manager_instance.get_openai_tools.return_value = tools
    manager_instance.get_tools_metadata = AsyncMock(return_value=metadata)
    manager_instance.__aenter__ = AsyncMock(return_value=manager_instance)
    manager_instance.__aexit__ = AsyncMock(return_value=None)

    mock_cls = MagicMock(return_value=manager_instance)
    monkeypatch.setattr("api.v1.tools.MCPManager", mock_cls)


@pytest.mark.integration
async def test_get_tools_null_enables_core_only(api_client, monkeypatch) -> None:
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
    # All fake tools are core, so all should be enabled
    assert all(t["enabled"] is True for t in data)
    names = {t["name"] for t in data}
    assert names == {"search_contact", "get_contact", "add_lead"}


@pytest.mark.integration
async def test_get_tools_null_excludes_extended(api_client, monkeypatch) -> None:
    _patch_mcp_manager(monkeypatch, include_extended=True)
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
    assert len(data) == 4
    status_map = {t["name"]: t["enabled"] for t in data}
    assert status_map["search_contact"] is True
    assert status_map["get_contact"] is True
    assert status_map["add_lead"] is True
    assert status_map["list_catalogs"] is False  # extended, disabled by default


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
        cred.enabled_tools = ["search_contact", "add_lead"]
        await session.commit()

    resp = await api_client.get(f"/v1/agents/{agent_id}/tools/bitrix", headers=_headers(owner_id))

    assert resp.status_code == 200
    data = resp.json()
    status_map = {t["name"]: t["enabled"] for t in data}
    assert status_map == {
        "search_contact": True,
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
        "search_contact": False,
        "get_contact": True,
        "add_lead": False,
    }

    # Verify GET returns the same
    get_resp = await api_client.get(f"/v1/agents/{agent_id}/tools/bitrix", headers=_headers(owner_id))
    assert get_resp.json() == put_resp.json()


@pytest.mark.integration
async def test_put_tools_null_enables_core_only(api_client, monkeypatch) -> None:
    _patch_mcp_manager(monkeypatch, include_extended=True)
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

    # Then reset to null (core only)
    put_resp = await api_client.put(
        f"/v1/agents/{agent_id}/tools/bitrix",
        json={"enabled_tools": None},
        headers=_headers(owner_id),
    )

    assert put_resp.status_code == 200
    data = put_resp.json()
    status_map = {t["name"]: t["enabled"] for t in data}
    # Core tools enabled, extended disabled
    assert status_map["search_contact"] is True
    assert status_map["get_contact"] is True
    assert status_map["add_lead"] is True
    assert status_map["list_catalogs"] is False


@pytest.mark.integration
async def test_put_tools_wildcard_enables_all(api_client, monkeypatch) -> None:
    _patch_mcp_manager(monkeypatch, include_extended=True)
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
        json={"enabled_tools": ["*"]},
        headers=_headers(owner_id),
    )

    assert put_resp.status_code == 200
    assert all(t["enabled"] is True for t in put_resp.json())


@pytest.mark.integration
async def test_tools_include_category_and_tier(api_client, monkeypatch) -> None:
    _patch_mcp_manager(monkeypatch, include_extended=True)
    owner_id = uuid4()

    resp = await api_client.post("/v1/agents/", json=_build_agent_payload(), headers=_headers(owner_id))
    agent_id = resp.json()["id"]

    await api_client.put(
        f"/v1/agents/{agent_id}/credentials",
        json={"service_name": "bitrix", "credentials": {"webhook_url": "https://example.bitrix/rest"}},
        headers=_headers(owner_id),
    )

    resp = await api_client.get(f"/v1/agents/{agent_id}/tools/bitrix", headers=_headers(owner_id))
    data = resp.json()

    for t in data:
        assert "category" in t
        assert "tier" in t

    by_name = {t["name"]: t for t in data}
    assert by_name["search_contact"]["category"] == "contacts"
    assert by_name["search_contact"]["tier"] == "core"
    assert by_name["list_catalogs"]["category"] == "catalogs"
    assert by_name["list_catalogs"]["tier"] == "extended"


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
