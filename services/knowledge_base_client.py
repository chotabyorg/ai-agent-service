"""HTTP client for knowledge-base-service internal retrieval."""

from typing import Any
from uuid import UUID

import httpx
from pydantic import BaseModel, Field, ValidationError

from services.managed_http_cleanup import close_managed_helper, own_managed_http


class KnowledgeBaseClientError(Exception):
    """Base error for knowledge-base-service client failures."""


class KnowledgeBaseUnavailableError(KnowledgeBaseClientError):
    """Raised when knowledge-base-service is unavailable or returns an error."""


class KnowledgeBaseBadResponseError(KnowledgeBaseClientError):
    """Raised when knowledge-base-service returns malformed data."""


class KnowledgeBaseContext(BaseModel):
    chunk_id: UUID
    document_id: UUID
    knowledge_base_id: UUID
    content: str
    score: float | None = None
    source_filename: str
    chunk_index: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeBaseContextResponse(BaseModel):
    contexts: list[KnowledgeBaseContext]
    total: int


class KnowledgeBaseClient:
    """Thin async wrapper around knowledge-base-service retrieval endpoint."""

    def __init__(
        self,
        *,
        base_url: str,
        service_token: str,
        timeout: float = 2.0,
        transport: httpx.AsyncBaseTransport | None = None,
        managed: bool = False,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._service_token = service_token
        self._managed = managed
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport,
                                        **({"trust_env": False} if managed else {}))
        if managed:
            own_managed_http(self._client)

    async def aclose(self) -> None:
        if self._managed:
            await close_managed_helper(self._client)
        else:
            await self._client.aclose()

    async def retrieve_context(
        self,
        *,
        owner_id: str,
        agent_id: UUID,
        query: str,
    ) -> KnowledgeBaseContextResponse:
        url = f"{self._base_url}/internal/v1/retrieval/context"
        payload = {
            "owner_id": owner_id,
            "agent_id": str(agent_id),
            "query": query,
        }
        headers = {
            "X-Service-Token": self._service_token,
            "Content-Type": "application/json",
        }

        try:
            response = await self._client.post(url, headers=headers, json=payload)
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            raise KnowledgeBaseUnavailableError("knowledge-base-service unavailable") from exc

        if response.status_code >= 400:
            raise KnowledgeBaseUnavailableError("knowledge-base-service returned an error")

        try:
            return KnowledgeBaseContextResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise KnowledgeBaseBadResponseError("knowledge-base-service returned invalid response") from exc
