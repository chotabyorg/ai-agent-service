"""Tests for subscription check on agent activation (PUT /v1/agents/{id})."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

import main
from models.agent import AgentTemplate
from services.encryption_service import EncryptionService


def build_agent_payload() -> dict[str, object]:
    return {
        "name": "Support agent",
        "system_prompt": "Be concise and helpful.",
        "model_name": "google/gemini-2.5-flash",
        "temperature": 0.4,
        "is_active": False,
    }


def build_headers(user_id: UUID | None = None) -> dict[str, str]:
    return {"x-user-id": str(user_id or uuid4())}


async def _create_agent_with_key(api_client, db_session, owner_id: UUID) -> str:
    """Create an agent and set an OpenRouter key directly in the DB."""

    resp = await api_client.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = resp.json()["id"]

    enc = EncryptionService()
    async with db_session() as session:
        agent = await session.get(AgentTemplate, UUID(agent_id))
        agent.encrypted_openrouter_key = enc.encrypt("sk-or-test-key-1234")
        await session.commit()

    return agent_id


# ------------------------------------------------------------------
# 1. Subscription allowed → agent activated
# ------------------------------------------------------------------
@pytest.mark.integration
async def test_activate_agent_subscription_allowed(
    api_client_no_raise, db_session, enable_subscription_check
) -> None:
    owner_id = uuid4()
    agent_id = await _create_agent_with_key(api_client_no_raise, db_session, owner_id)

    enable_subscription_check.check.return_value = {"allowed": True}

    resp = await api_client_no_raise.put(
        f"/v1/agents/{agent_id}",
        json={"is_active": True},
        headers=build_headers(owner_id),
    )

    assert resp.status_code == 200
    assert resp.json()["is_active"] is True
    enable_subscription_check.check.assert_awaited_once_with(str(owner_id))


# ------------------------------------------------------------------
# 2. No subscription → 402
# ------------------------------------------------------------------
@pytest.mark.integration
async def test_activate_agent_no_subscription_returns_402(
    api_client_no_raise, db_session, enable_subscription_check
) -> None:
    owner_id = uuid4()
    agent_id = await _create_agent_with_key(api_client_no_raise, db_session, owner_id)

    enable_subscription_check.check.return_value = {
        "allowed": False,
        "reason": "no_subscription",
    }

    resp = await api_client_no_raise.put(
        f"/v1/agents/{agent_id}",
        json={"is_active": True},
        headers=build_headers(owner_id),
    )

    assert resp.status_code == 402
    assert resp.json()["detail"] == "no_subscription"


# ------------------------------------------------------------------
# 3. Agent limit reached → 402
# ------------------------------------------------------------------
@pytest.mark.integration
async def test_activate_agent_limit_reached_returns_402(
    api_client_no_raise, db_session, enable_subscription_check
) -> None:
    owner_id = uuid4()
    agent_id = await _create_agent_with_key(api_client_no_raise, db_session, owner_id)

    enable_subscription_check.check.return_value = {
        "allowed": False,
        "reason": "agent_limit_reached",
    }

    resp = await api_client_no_raise.put(
        f"/v1/agents/{agent_id}",
        json={"is_active": True},
        headers=build_headers(owner_id),
    )

    assert resp.status_code == 402
    assert resp.json()["detail"] == "agent_limit_reached"


# ------------------------------------------------------------------
# 4. Subscription-service unavailable → fail-open (agent activated)
#    The real SubscriptionClient.check catches all exceptions and
#    returns {"allowed": True}. We simulate that resolved value.
# ------------------------------------------------------------------
@pytest.mark.integration
async def test_activate_agent_subscription_service_failopen(
    api_client_no_raise, db_session, enable_subscription_check
) -> None:
    owner_id = uuid4()
    agent_id = await _create_agent_with_key(api_client_no_raise, db_session, owner_id)

    # Simulates what SubscriptionClient.check returns when the remote
    # service is down (the try/except in the real client returns this).
    enable_subscription_check.check.return_value = {"allowed": True}

    resp = await api_client_no_raise.put(
        f"/v1/agents/{agent_id}",
        json={"is_active": True},
        headers=build_headers(owner_id),
    )

    assert resp.status_code == 200
    assert resp.json()["is_active"] is True


# ------------------------------------------------------------------
# 5. Feature flag off (subscription_client is None) → skip check
# ------------------------------------------------------------------
@pytest.mark.integration
async def test_activate_agent_feature_flag_off_skips_check(
    api_client_no_raise, db_session
) -> None:
    # Ensure no subscription_client is set (default state)
    main.app.state.subscription_client = None

    owner_id = uuid4()
    agent_id = await _create_agent_with_key(api_client_no_raise, db_session, owner_id)

    resp = await api_client_no_raise.put(
        f"/v1/agents/{agent_id}",
        json={"is_active": True},
        headers=build_headers(owner_id),
    )

    assert resp.status_code == 200
    assert resp.json()["is_active"] is True


# ------------------------------------------------------------------
# 6. Deactivation (is_active=false) → subscription check NOT called
# ------------------------------------------------------------------
@pytest.mark.integration
async def test_deactivate_agent_does_not_check_subscription(
    api_client_no_raise, db_session, enable_subscription_check
) -> None:
    owner_id = uuid4()
    agent_id = await _create_agent_with_key(api_client_no_raise, db_session, owner_id)

    # First activate
    enable_subscription_check.check.return_value = {"allowed": True}
    await api_client_no_raise.put(
        f"/v1/agents/{agent_id}",
        json={"is_active": True},
        headers=build_headers(owner_id),
    )
    enable_subscription_check.check.reset_mock()

    # Now deactivate
    resp = await api_client_no_raise.put(
        f"/v1/agents/{agent_id}",
        json={"is_active": False},
        headers=build_headers(owner_id),
    )

    assert resp.status_code == 200
    assert resp.json()["is_active"] is False
    enable_subscription_check.check.assert_not_awaited()


# ------------------------------------------------------------------
# 7. Subscription ok but no OpenRouter key → 400 (existing check)
# ------------------------------------------------------------------
@pytest.mark.integration
async def test_activate_agent_subscription_ok_but_no_key_returns_400(
    api_client_no_raise, db_session, enable_subscription_check
) -> None:
    owner_id = uuid4()
    # Create agent WITHOUT setting an OpenRouter key
    resp = await api_client_no_raise.post(
        "/v1/agents/", json=build_agent_payload(), headers=build_headers(owner_id)
    )
    agent_id = resp.json()["id"]

    enable_subscription_check.check.return_value = {"allowed": True}

    resp = await api_client_no_raise.put(
        f"/v1/agents/{agent_id}",
        json={"is_active": True},
        headers=build_headers(owner_id),
    )

    assert resp.status_code == 400
    assert "OpenRouter API key" in resp.json()["detail"]
    enable_subscription_check.check.assert_awaited_once()
