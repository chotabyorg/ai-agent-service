from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import ANY, AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from api.dependencies import get_db
from api.internal import agents as internal_agents
from main import app


@pytest.fixture
async def internal_client() -> AsyncIterator[AsyncClient]:
    async def override_db() -> AsyncIterator[object]:
        yield object()

    app.dependency_overrides[get_db] = override_db
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def service_token(monkeypatch: pytest.MonkeyPatch) -> str:
    from core.config import get_settings

    value = "test-internal-token"
    monkeypatch.setenv("INTERNAL_API_TOKEN", value)
    get_settings.cache_clear()
    yield value
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_ownership_route_returns_no_content_for_matching_owner(
    internal_client: AsyncClient,
    service_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = AsyncMock(return_value=object())
    monkeypatch.setattr(internal_agents.agent_service, "get_agent_by_id", lookup)
    agent_id, owner_id = uuid4(), uuid4()

    response = await internal_client.post(
        "/internal/v1/agents/ownership-check",
        json={"agent_id": str(agent_id), "owner_id": str(owner_id)},
        headers={"X-Service-Token": service_token},
    )

    assert response.status_code == 204
    assert response.content == b""
    lookup.assert_awaited_once_with(
        ANY,
        agent_id,
        user_id=owner_id,
    )


@pytest.mark.asyncio
async def test_ownership_route_returns_404_for_foreign_or_unknown_owner(
    internal_client: AsyncClient,
    service_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(internal_agents.agent_service, "get_agent_by_id", lookup)

    response = await internal_client.post(
        "/internal/v1/agents/ownership-check",
        json={"agent_id": str(uuid4()), "owner_id": str(uuid4())},
        headers={"X-Service-Token": service_token},
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Agent not found"}


@pytest.mark.asyncio
async def test_ownership_route_requires_service_token(
    internal_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lookup = AsyncMock(return_value=object())
    monkeypatch.setattr(internal_agents.agent_service, "get_agent_by_id", lookup)

    response = await internal_client.post(
        "/internal/v1/agents/ownership-check",
        json={"agent_id": str(uuid4()), "owner_id": str(uuid4())},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid service token"}
    lookup.assert_not_awaited()
