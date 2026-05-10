import json
from uuid import uuid4

import httpx
import pytest

from services.knowledge_base_client import (
    KnowledgeBaseBadResponseError,
    KnowledgeBaseClient,
    KnowledgeBaseUnavailableError,
)


@pytest.mark.asyncio
async def test_retrieve_context_posts_internal_request_with_service_token() -> None:
    owner_id = str(uuid4())
    agent_id = uuid4()
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        captured["json"] = json.loads(request.content.decode())
        return httpx.Response(
            200,
            json={
                "contexts": [
                    {
                        "chunk_id": str(uuid4()),
                        "document_id": str(uuid4()),
                        "knowledge_base_id": str(uuid4()),
                        "content": "Warranty is 12 months",
                        "score": 0.88,
                        "source_filename": "faq.pdf",
                        "chunk_index": 2,
                        "metadata": {"content_type": "application/pdf", "raw_extra": "ignored"},
                    }
                ],
                "total": 1,
            },
        )

    client = KnowledgeBaseClient(
        base_url="http://knowledge-base-service:8000/",
        service_token="service-token",
        timeout=2.0,
        transport=httpx.MockTransport(handler),
    )

    result = await client.retrieve_context(
        owner_id=owner_id,
        agent_id=agent_id,
        query="What is warranty?",
    )

    assert captured["url"] == "http://knowledge-base-service:8000/internal/v1/retrieval/context"
    assert captured["headers"]["X-Service-Token"] == "service-token"
    assert captured["json"] == {
        "owner_id": owner_id,
        "agent_id": str(agent_id),
        "query": "What is warranty?",
    }
    assert result.total == 1
    assert result.contexts[0].content == "Warranty is 12 months"


@pytest.mark.asyncio
async def test_retrieve_context_accepts_empty_contexts() -> None:
    client = KnowledgeBaseClient(
        base_url="http://knowledge-base-service:8000",
        service_token="service-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"contexts": [], "total": 0})),
    )

    result = await client.retrieve_context(
        owner_id=str(uuid4()),
        agent_id=uuid4(),
        query="Unknown",
    )

    assert result.contexts == []
    assert result.total == 0


@pytest.mark.asyncio
async def test_retrieve_context_maps_transport_errors_to_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("service down", request=request)

    client = KnowledgeBaseClient(
        base_url="http://knowledge-base-service:8000",
        service_token="service-token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(KnowledgeBaseUnavailableError):
        await client.retrieve_context(
            owner_id=str(uuid4()),
            agent_id=uuid4(),
            query="Question",
        )


@pytest.mark.asyncio
async def test_retrieve_context_maps_http_errors_to_unavailable() -> None:
    client = KnowledgeBaseClient(
        base_url="http://knowledge-base-service:8000",
        service_token="service-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(503, json={"detail": "down"})),
    )

    with pytest.raises(KnowledgeBaseUnavailableError):
        await client.retrieve_context(
            owner_id=str(uuid4()),
            agent_id=uuid4(),
            query="Question",
        )


@pytest.mark.asyncio
async def test_retrieve_context_maps_bad_json_to_bad_response() -> None:
    client = KnowledgeBaseClient(
        base_url="http://knowledge-base-service:8000",
        service_token="service-token",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="not json")),
    )

    with pytest.raises(KnowledgeBaseBadResponseError):
        await client.retrieve_context(
            owner_id=str(uuid4()),
            agent_id=uuid4(),
            query="Question",
        )
