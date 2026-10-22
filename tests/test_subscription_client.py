"""Focused protocol tests for subscription-service permission checks."""

from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr

from services.subscription_client import (
    SubscriptionCheckUnavailable,
    SubscriptionClient,
)


@pytest.mark.asyncio
async def test_allowed_paid_user_sends_dedicated_credential_and_canonical_user_id():
    owner_id = uuid4()
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "allowed": True,
                "reason": None,
                "limits": {"max_agents": 5, "used_agents": 2},
            },
        )

    client = SubscriptionClient(
        "http://subscription-service:8000/",
        subscription_check_token=SecretStr("dedicated-check-token"),
        transport=httpx.MockTransport(respond),
    )

    result = await client.check("{" + str(owner_id).upper() + "}")

    assert result == {
        "allowed": True,
        "reason": None,
        "limits": {"max_agents": 5, "used_agents": 2},
    }
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/v1/subscriptions/check"
    assert dict(request.url.params) == {"action": "activate_agent"}
    assert request.headers["x-user-id"] == str(UUID(str(owner_id)))
    assert request.headers["x-subscription-check-token"] == "dedicated-check-token"
    assert "x-service-token" not in request.headers
    assert "authorization" not in request.headers
    assert "x-user-role" not in request.headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision",
    [
        {"allowed": False, "reason": "no_subscription", "limits": None},
        {"allowed": False, "reason": "subscription_expired", "limits": None},
        {
            "allowed": False,
            "reason": "agent_limit_reached",
            "limits": {"max_agents": 3, "used_agents": 3},
        },
    ],
    ids=["no-subscription", "expired", "at-limit"],
)
async def test_valid_subscription_denials_are_returned(decision):
    client = SubscriptionClient(
        "http://subscription-service:8000",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=decision)
        ),
    )

    assert await client.check(uuid4()) == decision


@pytest.mark.asyncio
async def test_legacy_optional_token_sends_only_the_user_authentication_header():
    requests = []
    client = SubscriptionClient(
        "http://subscription-service:8000",
        transport=httpx.MockTransport(
            lambda request: (
                requests.append(request)
                or httpx.Response(
                    200,
                    json={
                        "allowed": True,
                        "limits": {"max_agents": 1, "used_agents": 0},
                    },
                )
            )
        ),
    )

    await client.check(uuid4())

    assert len(requests) == 1
    assert "x-user-id" in requests[0].headers
    assert "x-subscription-check-token" not in requests[0].headers
    assert "x-service-token" not in requests[0].headers
    assert "authorization" not in requests[0].headers


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [201, 302, 401, 403, 500, 503])
async def test_every_non_200_status_fails_closed_without_redirect_or_retry(status):
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(status, headers={"location": "/different-route"})

    client = SubscriptionClient(
        "http://subscription-service:8000",
        transport=httpx.MockTransport(respond),
    )

    with pytest.raises(SubscriptionCheckUnavailable, match="subscription check unavailable"):
        await client.check(uuid4())

    assert len(requests) == 1
    assert requests[0].url.path == "/v1/subscriptions/check"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("connection failed"),
        httpx.ReadTimeout("timed out"),
    ],
    ids=["network", "timeout"],
)
async def test_network_and_timeout_errors_fail_closed_without_retry(error):
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        raise error

    client = SubscriptionClient(
        "http://subscription-service:8000",
        transport=httpx.MockTransport(respond),
    )

    with pytest.raises(SubscriptionCheckUnavailable, match="subscription check unavailable"):
        await client.check(uuid4())

    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"allowed": true, "allowed": false}',
        b'{"allowed": true, "limits": {"max_agents": 2, "max_agents": 3, "used_agents": 0}}',
        b"[]",
        b"null",
        b"{\"allowed\": \"false\", \"reason\": \"no_subscription\"}",
        b"{}",
        b'{"allowed": true, "reason": "no_subscription", "limits": {"max_agents": 1, "used_agents": 0}}',
        b'{"allowed": true}',
        b'{"allowed": true, "limits": null}',
        b'{"allowed": true, "limits": {"max_agents": true, "used_agents": 0}}',
        b'{"allowed": true, "limits": {"max_agents": 1, "used_agents": -1}}',
        b'{"allowed": true, "limits": {"max_agents": 1, "used_agents": 1}}',
        b'{"allowed": false}',
        b'{"allowed": false, "reason": "other"}',
        b'{"allowed": false, "reason": "agent_limit_reached"}',
        b'{"allowed": false, "reason": "agent_limit_reached", "limits": {"max_agents": 2, "used_agents": 1}}',
        b'{"allowed": false, "reason": "no_subscription", "limits": {"max_agents": 1, "used_agents": 1}}',
        b'{"allowed": false, "reason": "no_subscription", "extra": true}',
        b'{"allowed": true, "limits": {"max_agents": 1, "used_agents": 0}, "unexpected": true}',
        b'{"allowed": true, "limits": {"max_agents": 1, "used_agents": 0}, "reason": NaN}',
        b"\xff",
    ],
    ids=[
        "malformed-json",
        "duplicate-root-key",
        "duplicate-nested-key",
        "array-root",
        "null-root",
        "truthy-allowed",
        "missing-allowed",
        "allowed-with-denial-reason",
        "missing-allowed-limits",
        "null-allowed-limits",
        "boolean-limit",
        "negative-limit",
        "allowed-at-limit",
        "missing-denial-reason",
        "unknown-reason",
        "limit-denial-without-limits",
        "limit-denial-below-limit",
        "different-denial-at-limit",
        "unknown-denial-field",
        "unknown-allowed-field",
        "non-json-number",
        "invalid-utf8",
    ],
)
async def test_malformed_or_inconsistent_decision_fails_closed(body):
    client = SubscriptionClient(
        "http://subscription-service:8000",
        subscription_check_token="do-not-leak-this-token",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, content=body, headers={"content-type": "application/json"}
            )
        ),
    )

    with pytest.raises(SubscriptionCheckUnavailable) as exc_info:
        await client.check(uuid4())

    assert str(exc_info.value) == "subscription check unavailable"
    assert "do-not-leak-this-token" not in str(exc_info.value)
    decoded_body = body.decode("utf-8", errors="ignore")
    if decoded_body:
        assert decoded_body not in str(exc_info.value)


@pytest.mark.asyncio
async def test_non_json_content_type_fails_closed():
    body = b'{"allowed": true, "limits": {"max_agents": 1, "used_agents": 0}}'
    client = SubscriptionClient(
        "http://subscription-service:8000",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200, content=body, headers={"content-type": "text/plain"}
            )
        ),
    )

    with pytest.raises(SubscriptionCheckUnavailable):
        await client.check(uuid4())


@pytest.mark.asyncio
async def test_invalid_user_id_fails_closed_before_network_request():
    calls = []
    client = SubscriptionClient(
        "http://subscription-service:8000",
        transport=httpx.MockTransport(
            lambda request: calls.append(request) or httpx.Response(200, json={})
        ),
    )

    with pytest.raises(SubscriptionCheckUnavailable):
        await client.check("not-a-uuid")

    assert calls == []
