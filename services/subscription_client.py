"""HTTP client for subscription-service health/limit checks."""

import logging

import httpx

logger = logging.getLogger(__name__)


class SubscriptionClient:
    """Thin wrapper around subscription-service /v1/subscriptions/check.

    Fail-open: when the remote service is unreachable or returns an error
    the client returns ``{"allowed": True}`` so that paying users are never
    blocked by an infrastructure outage.
    """

    def __init__(self, base_url: str, timeout: float = 5.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def check(self, user_id: str, action: str = "activate_agent") -> dict:
        """Return ``{"allowed": bool, "reason": ...}`` for *user_id*."""

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.get(
                    f"{self._base_url}/v1/subscriptions/check",
                    params={"action": action},
                    headers={"x-user-id": user_id},
                )
                resp.raise_for_status()
                return resp.json()
        except Exception:
            logger.exception(
                "subscription-service unavailable, allowing activation (fail-open)"
            )
            return {"allowed": True}
