"""Tests for GET /v1/models endpoint."""

import httpx
import pytest
import pytest_asyncio

import main


@pytest_asyncio.fixture
async def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@pytest.mark.asyncio
async def test_list_models_returns_200(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/models")
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_list_models_returns_non_empty_list(client: httpx.AsyncClient) -> None:
    data = (await client.get("/v1/models")).json()
    assert isinstance(data, list)
    assert len(data) > 0


@pytest.mark.asyncio
async def test_list_models_items_have_required_fields(client: httpx.AsyncClient) -> None:
    data = (await client.get("/v1/models")).json()
    for item in data:
        assert "display_name" in item
        assert "model_id" in item
        assert isinstance(item["display_name"], str)
        assert isinstance(item["model_id"], str)
