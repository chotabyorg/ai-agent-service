"""HTTP client for subscription-service activation checks."""

import json
from typing import Any
from uuid import UUID

import httpx
from pydantic import SecretStr


_CHECK_REASONS = frozenset(
    {"no_subscription", "subscription_expired", "agent_limit_reached"}
)
_RESPONSE_KEYS = frozenset({"allowed", "reason", "limits"})
_LIMIT_KEYS = frozenset({"max_agents", "used_agents"})


class SubscriptionCheckUnavailable(RuntimeError):
    """Raised when a subscription check cannot return a trusted decision."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build JSON objects while rejecting duplicate member names."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON member")
        result[key] = value
    return result


def _reject_non_json_constant(_value: str) -> None:
    # Python's json module accepts NaN and Infinity by default; neither is JSON.
    raise ValueError("non-JSON numeric constant")


def _validated_limits(value: Any) -> dict[str, int]:
    if type(value) is not dict or set(value) != _LIMIT_KEYS:
        raise ValueError("invalid limits")

    max_agents = value["max_agents"]
    used_agents = value["used_agents"]
    if (
        type(max_agents) is not int
        or type(used_agents) is not int
        or max_agents < 0
        or used_agents < 0
    ):
        raise ValueError("invalid limits")

    return {"max_agents": max_agents, "used_agents": used_agents}


def _validate_decision(value: Any) -> dict[str, Any]:
    if type(value) is not dict or not set(value).issubset(_RESPONSE_KEYS):
        raise ValueError("invalid response object")
    if "allowed" not in value or type(value["allowed"]) is not bool:
        raise ValueError("invalid allowed value")

    allowed = value["allowed"]
    reason = value.get("reason")
    if allowed:
        if reason is not None:
            raise ValueError("inconsistent allowed decision")
        if "limits" not in value:
            raise ValueError("missing limits")
        limits = _validated_limits(value["limits"])
        if limits["used_agents"] >= limits["max_agents"]:
            raise ValueError("inconsistent allowed limits")
    else:
        if not isinstance(reason, str) or reason not in _CHECK_REASONS:
            raise ValueError("invalid denial reason")
        limits = None
        if value.get("limits") is not None:
            limits = _validated_limits(value["limits"])
        if reason == "agent_limit_reached":
            if limits is None or limits["used_agents"] < limits["max_agents"]:
                raise ValueError("inconsistent limit denial")
        elif limits is not None and limits["used_agents"] >= limits["max_agents"]:
            raise ValueError("inconsistent denial limits")

    # Preserve the service's established response shape for existing callers.
    result: dict[str, Any] = {"allowed": allowed}
    if "reason" in value:
        result["reason"] = reason
    if "limits" in value:
        result["limits"] = limits
    return result


class SubscriptionClient:
    """Call subscription-service's ``activate_agent`` permission check.

    ``subscription_check_token`` is a dedicated optional credential for the
    staged service-to-service authentication rollout. It is independent of the
    AI service's management and chat credentials.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        subscription_check_token: str | SecretStr | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        if isinstance(subscription_check_token, SecretStr):
            subscription_check_token = subscription_check_token.get_secret_value()
        self._subscription_check_token = subscription_check_token or None
        self._transport = transport

    async def check(
        self, user_id: str | UUID, action: str = "activate_agent"
    ) -> dict[str, Any]:
        """Return a validated decision, or raise if no trusted decision exists."""

        try:
            canonical_user_id = str(UUID(str(user_id)))
        except (AttributeError, TypeError, ValueError):
            raise SubscriptionCheckUnavailable(
                "subscription check unavailable"
            ) from None

        headers = {"X-User-Id": canonical_user_id}
        if self._subscription_check_token:
            headers["X-Subscription-Check-Token"] = self._subscription_check_token

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                response = await client.get(
                    f"{self._base_url}/v1/subscriptions/check",
                    params={"action": action},
                    headers=headers,
                )
        except httpx.HTTPError:
            raise SubscriptionCheckUnavailable(
                "subscription check unavailable"
            ) from None

        if response.status_code != 200:
            raise SubscriptionCheckUnavailable("subscription check unavailable")

        content_type = response.headers.get("content-type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json" and not media_type.endswith("+json"):
            raise SubscriptionCheckUnavailable("subscription check unavailable")

        try:
            payload = json.loads(
                response.content,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_non_json_constant,
            )
            return _validate_decision(payload)
        except (TypeError, ValueError, UnicodeDecodeError):
            raise SubscriptionCheckUnavailable(
                "subscription check unavailable"
            ) from None
