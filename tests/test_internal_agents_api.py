"""Integration tests for /internal/v1/agents endpoints."""

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from models.agent import AgentTemplate


VALID_TOKEN = "test-internal-token"


@pytest.fixture
def internal_token(monkeypatch: pytest.MonkeyPatch) -> str:
    from core.config import get_settings

    monkeypatch.setenv("INTERNAL_API_TOKEN", VALID_TOKEN)
    get_settings.cache_clear()
    try:
        yield VALID_TOKEN
    finally:
        get_settings.cache_clear()


def _auth_headers(token: str = VALID_TOKEN) -> dict[str, str]:
    return {"X-Service-Token": token}


def _make_agent(user_id: UUID, *, name: str, is_active: bool) -> AgentTemplate:
    return AgentTemplate(
        user_id=user_id,
        name=name,
        system_prompt="",
        llm_provider="openrouter",
        model_name="google/gemini-2.5-flash",
        temperature=0.5,
        is_active=is_active,
    )


@pytest.mark.integration
async def test_ownership_check_returns_204_for_owner(
    api_client,
    db_session,
    internal_token: str,
) -> None:
    owner_id = uuid4()
    agent = _make_agent(owner_id, name="owned", is_active=True)
    async with db_session() as session:
        session.add(agent)
        await session.commit()
        agent_id = agent.id

    response = await api_client.post(
        "/internal/v1/agents/ownership-check",
        json={"agent_id": str(agent_id), "owner_id": str(owner_id)},
        headers=_auth_headers(),
    )

    assert response.status_code == 204
    assert response.content == b""


@pytest.mark.integration
async def test_ownership_check_returns_same_404_for_foreign_and_unknown_agents(
    api_client,
    db_session,
    internal_token: str,
) -> None:
    owner_id = uuid4()
    foreign_owner_id = uuid4()
    agent = _make_agent(owner_id, name="owned", is_active=True)
    async with db_session() as session:
        session.add(agent)
        await session.commit()
        agent_id = agent.id

    foreign_response = await api_client.post(
        "/internal/v1/agents/ownership-check",
        json={"agent_id": str(agent_id), "owner_id": str(foreign_owner_id)},
        headers=_auth_headers(),
    )
    unknown_response = await api_client.post(
        "/internal/v1/agents/ownership-check",
        json={"agent_id": str(uuid4()), "owner_id": str(foreign_owner_id)},
        headers=_auth_headers(),
    )

    assert foreign_response.status_code == unknown_response.status_code == 404
    assert foreign_response.json() == unknown_response.json() == {"detail": "Agent not found"}


@pytest.mark.integration
async def test_ownership_check_requires_service_token(
    api_client,
    internal_token: str,
) -> None:
    response = await api_client.post(
        "/internal/v1/agents/ownership-check",
        json={"agent_id": str(uuid4()), "owner_id": str(uuid4())},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid service token"}


# ==================== GET /internal/v1/agents/count ====================


@pytest.mark.integration
async def test_count_returns_total_and_active(
    api_client,
    db_session,
    internal_token: str,
) -> None:
    owner_id = uuid4()
    async with db_session() as session:
        session.add(_make_agent(owner_id, name="active1", is_active=True))
        session.add(_make_agent(owner_id, name="inactive1", is_active=False))
        # Agent of a different user — must not leak into counts.
        session.add(_make_agent(uuid4(), name="other", is_active=True))
        await session.commit()

    response = await api_client.get(
        f"/internal/v1/agents/count?user_id={owner_id}",
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {"total": 2, "active": 1}


@pytest.mark.integration
async def test_count_returns_zeros_when_no_agents(
    api_client,
    internal_token: str,
) -> None:
    response = await api_client.get(
        f"/internal/v1/agents/count?user_id={uuid4()}",
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {"total": 0, "active": 0}


@pytest.mark.integration
async def test_count_401_when_token_missing(
    api_client,
    internal_token: str,
) -> None:
    response = await api_client.get(
        f"/internal/v1/agents/count?user_id={uuid4()}",
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid service token"}


@pytest.mark.integration
async def test_count_401_when_token_invalid(
    api_client,
    internal_token: str,
) -> None:
    response = await api_client.get(
        f"/internal/v1/agents/count?user_id={uuid4()}",
        headers=_auth_headers("wrong-token"),
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid service token"}


@pytest.mark.integration
async def test_count_400_when_user_id_not_uuid(
    api_client,
    internal_token: str,
) -> None:
    response = await api_client.get(
        "/internal/v1/agents/count?user_id=not-a-uuid",
        headers=_auth_headers(),
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid user_id"}


@pytest.mark.integration
async def test_count_401_when_server_token_not_configured(
    api_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.config import get_settings

    monkeypatch.delenv("INTERNAL_API_TOKEN", raising=False)
    get_settings.cache_clear()
    try:
        response = await api_client.get(
            f"/internal/v1/agents/count?user_id={uuid4()}",
            headers=_auth_headers("any-token"),
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid service token"}


# ==================== POST /internal/v1/agents/deactivate-all ====================


@pytest.mark.integration
async def test_deactivate_all_sets_is_active_false_and_returns_count(
    api_client,
    db_session,
    internal_token: str,
) -> None:
    owner_id = uuid4()
    async with db_session() as session:
        session.add(_make_agent(owner_id, name="a1", is_active=True))
        session.add(_make_agent(owner_id, name="a2", is_active=True))
        session.add(_make_agent(owner_id, name="a3", is_active=False))
        # Other user's active agent must remain active.
        foreign_id = uuid4()
        session.add(_make_agent(foreign_id, name="other", is_active=True))
        await session.commit()

    response = await api_client.post(
        "/internal/v1/agents/deactivate-all",
        json={"user_id": str(owner_id)},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {"deactivated": 2}

    async with db_session() as session:
        result = await session.execute(
            select(AgentTemplate).where(AgentTemplate.user_id == owner_id)
        )
        agents = result.scalars().all()
        assert len(agents) == 3
        assert all(agent.is_active is False for agent in agents)

        foreign_result = await session.execute(
            select(AgentTemplate).where(AgentTemplate.user_id == foreign_id)
        )
        foreign_agents = foreign_result.scalars().all()
        assert len(foreign_agents) == 1
        assert foreign_agents[0].is_active is True


@pytest.mark.integration
async def test_deactivate_all_returns_zero_when_no_active_agents(
    api_client,
    db_session,
    internal_token: str,
) -> None:
    owner_id = uuid4()
    async with db_session() as session:
        session.add(_make_agent(owner_id, name="a1", is_active=False))
        await session.commit()

    response = await api_client.post(
        "/internal/v1/agents/deactivate-all",
        json={"user_id": str(owner_id)},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {"deactivated": 0}


@pytest.mark.integration
async def test_deactivate_all_returns_zero_when_user_has_no_agents(
    api_client,
    internal_token: str,
) -> None:
    response = await api_client.post(
        "/internal/v1/agents/deactivate-all",
        json={"user_id": str(uuid4())},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json() == {"deactivated": 0}


@pytest.mark.integration
async def test_deactivate_all_401_when_token_missing(
    api_client,
    internal_token: str,
) -> None:
    response = await api_client.post(
        "/internal/v1/agents/deactivate-all",
        json={"user_id": str(uuid4())},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid service token"}


@pytest.mark.integration
async def test_deactivate_all_422_when_user_id_missing(
    api_client,
    internal_token: str,
) -> None:
    response = await api_client.post(
        "/internal/v1/agents/deactivate-all",
        json={},
        headers=_auth_headers(),
    )

    assert response.status_code == 422
