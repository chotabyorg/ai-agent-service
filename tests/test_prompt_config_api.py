"""Tests for prompt-config and prompt-preview API endpoints."""

from uuid import uuid4

import pytest


MINIMAL_PROMPT_CONFIG = {
    "version": 1,
    "company": {"name": "TestCo", "description": "We test things"},
    "personality": {"tone": "friendly", "languages": ["ru"], "use_emoji": True},
    "capabilities": ["Консультация"],
}


def build_agent_payload(**overrides: object) -> dict[str, object]:
    base = {
        "name": "Support agent",
        "system_prompt": "Be concise and helpful.",
        "model_name": "google/gemini-2.5-flash",
        "temperature": 0.4,
        "is_active": True,
    }
    base.update(overrides)
    return base


def build_headers(user_id=None) -> dict[str, str]:
    return {"x-user-id": str(user_id or uuid4())}


async def create_agent(api_client, headers, **overrides) -> dict:
    payload = build_agent_payload(**overrides)
    resp = await api_client.post("/v1/agents/", json=payload, headers=headers)
    assert resp.status_code == 201
    return resp.json()


# ── GET /v1/agents/{id}/prompt-config ──────────────────────────────


@pytest.mark.integration
async def test_get_prompt_config_returns_config_for_structured_agent(
    api_client, db_session, fake_redis,
) -> None:
    headers = build_headers()
    agent = await create_agent(
        api_client,
        headers,
        prompt_config=MINIMAL_PROMPT_CONFIG,
        system_prompt=None,
    )

    resp = await api_client.get(
        f"/v1/agents/{agent['id']}/prompt-config", headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["prompt_config"] is not None
    assert body["prompt_config"]["company"]["name"] == "TestCo"


@pytest.mark.integration
async def test_get_prompt_config_returns_null_for_legacy_agent(
    api_client, db_session, fake_redis,
) -> None:
    headers = build_headers()
    agent = await create_agent(api_client, headers)

    resp = await api_client.get(
        f"/v1/agents/{agent['id']}/prompt-config", headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["prompt_config"] is None


# ── PUT /v1/agents/{id}/prompt-config ──────────────────────────────


@pytest.mark.integration
async def test_put_prompt_config_updates_config_and_rebuilds_system_prompt(
    api_client, db_session, fake_redis,
) -> None:
    headers = build_headers()
    agent = await create_agent(api_client, headers)

    resp = await api_client.put(
        f"/v1/agents/{agent['id']}/prompt-config",
        json=MINIMAL_PROMPT_CONFIG,
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["prompt_config"]["company"]["name"] == "TestCo"

    # Verify system_prompt was rebuilt
    agent_resp = await api_client.get(
        f"/v1/agents/{agent['id']}", headers=headers,
    )
    assert agent_resp.status_code == 200
    rebuilt_prompt = agent_resp.json()["system_prompt"]
    assert "<identity>" in rebuilt_prompt
    assert "TestCo" in rebuilt_prompt
    assert "Текущая дата" not in rebuilt_prompt
    assert "<platform_rules>" not in rebuilt_prompt


@pytest.mark.integration
async def test_responsibilities_config_roundtrips_without_rewriting_legacy_alias(
    api_client, db_session, fake_redis,
) -> None:
    headers = build_headers()
    agent = await create_agent(api_client, headers)
    config = {**MINIMAL_PROMPT_CONFIG, "responsibilities": ["Помочь выбрать специалиста"]}
    response = await api_client.put(
        f"/v1/agents/{agent['id']}/prompt-config", json=config, headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["prompt_config"] == config
    preview = await api_client.get(
        f"/v1/agents/{agent['id']}/prompt-preview", headers=headers,
    )
    rendered = preview.json()["rendered_prompt"]
    assert "Помочь выбрать специалиста" in rendered
    assert "Консультация" not in rendered
    assert rendered.startswith("<platform_policy>")


@pytest.mark.integration
async def test_legacy_preview_adds_policy_but_does_not_rewrite_stored_prompt(
    api_client, db_session, fake_redis,
) -> None:
    headers = build_headers()
    original = "You are our helpful salon assistant."
    agent = await create_agent(api_client, headers, system_prompt=original)
    preview = await api_client.get(
        f"/v1/agents/{agent['id']}/prompt-preview", headers=headers,
    )
    assert preview.status_code == 200
    assert preview.json()["rendered_prompt"].startswith("<platform_policy>")
    assert original in preview.json()["rendered_prompt"]
    current = await api_client.get(f"/v1/agents/{agent['id']}", headers=headers)
    assert current.json()["system_prompt"] == original
    assert current.json()["prompt_config"] is None


@pytest.mark.integration
async def test_put_prompt_config_returns_422_for_invalid_data(
    api_client, db_session, fake_redis,
) -> None:
    headers = build_headers()
    agent = await create_agent(api_client, headers)

    resp = await api_client.put(
        f"/v1/agents/{agent['id']}/prompt-config",
        json={"invalid": "data"},
        headers=headers,
    )
    assert resp.status_code == 422


# ── GET /v1/agents/{id}/prompt-preview ─────────────────────────────


@pytest.mark.integration
async def test_get_prompt_preview_returns_rendered_xml_for_telegram(
    api_client, db_session, fake_redis,
) -> None:
    headers = build_headers()
    agent = await create_agent(
        api_client,
        headers,
        prompt_config=MINIMAL_PROMPT_CONFIG,
        system_prompt=None,
    )

    resp = await api_client.get(
        f"/v1/agents/{agent['id']}/prompt-preview",
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["channel"] == "telegram"
    assert "<identity>" in body["rendered_prompt"]
    assert "4096 символов" in body["rendered_prompt"]


@pytest.mark.integration
async def test_get_prompt_preview_returns_instagram_rules_for_instagram_channel(
    api_client, db_session, fake_redis,
) -> None:
    headers = build_headers()
    agent = await create_agent(
        api_client,
        headers,
        prompt_config=MINIMAL_PROMPT_CONFIG,
        system_prompt=None,
    )

    resp = await api_client.get(
        f"/v1/agents/{agent['id']}/prompt-preview?channel=instagram",
        headers=headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["channel"] == "instagram"
    assert "1000 символов" in body["rendered_prompt"]


# ── 404 for foreign agents ─────────────────────────────────────────


@pytest.mark.integration
async def test_all_prompt_endpoints_return_404_for_foreign_agent(
    api_client, db_session, fake_redis,
) -> None:
    owner_headers = build_headers()
    stranger_headers = build_headers()

    agent = await create_agent(api_client, owner_headers)
    agent_id = agent["id"]

    # GET prompt-config
    resp = await api_client.get(
        f"/v1/agents/{agent_id}/prompt-config", headers=stranger_headers,
    )
    assert resp.status_code == 404

    # PUT prompt-config
    resp = await api_client.put(
        f"/v1/agents/{agent_id}/prompt-config",
        json=MINIMAL_PROMPT_CONFIG,
        headers=stranger_headers,
    )
    assert resp.status_code == 404

    # GET prompt-preview
    resp = await api_client.get(
        f"/v1/agents/{agent_id}/prompt-preview", headers=stranger_headers,
    )
    assert resp.status_code == 404
