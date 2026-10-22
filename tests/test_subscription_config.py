"""Startup configuration checks for the subscription permission client."""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import SecretStr

from core.config import Settings


BASE_URL = "http://subscription-service:8000"
VALID_TOKEN = "0123456789abcdef0123456789abcdef"


def test_explicitly_disabled_subscription_check_keeps_free_mode():
    settings = Settings(
        _env_file=None,
        subscription_check_enabled=False,
        subscription_check_auth_mode="required",
    )

    assert settings.subscription_service_url == ""
    assert settings.subscription_check_token.get_secret_value() == ""


def test_disabled_feature_ignores_unused_subscription_url_and_token_mode():
    settings = Settings(
        _env_file=None,
        subscription_check_enabled=False,
        subscription_service_url="not-a-url",
        subscription_check_auth_mode="required",
    )

    assert settings.subscription_check_enabled is False


def test_enabled_legacy_mode_allows_tokenless_rolling_migration():
    settings = Settings(
        _env_file=None,
        subscription_check_enabled=True,
        subscription_service_url=BASE_URL,
    )

    assert settings.subscription_check_auth_mode == "legacy"
    assert settings.subscription_check_token.get_secret_value() == ""


def test_enabled_required_mode_accepts_a_dedicated_valid_token():
    settings = Settings(
        _env_file=None,
        subscription_check_enabled=True,
        subscription_service_url=BASE_URL,
        subscription_check_auth_mode="required",
        subscription_check_token=SecretStr(VALID_TOKEN),
    )

    assert settings.subscription_check_token.get_secret_value() == VALID_TOKEN


@pytest.mark.parametrize(
    "overrides",
    [
        {"subscription_service_url": ""},
        {"subscription_service_url": "not-a-url"},
        {"subscription_service_url": "ftp://subscription-service:8000"},
        {"subscription_service_url": "http://user:password@subscription-service:8000"},
        {"subscription_service_url": "http://subscription-service:8000?token=secret"},
        {"subscription_service_url": "http://subscription-service:8000#fragment"},
        {"subscription_service_url": "http://subscription-service:8000/base"},
        {
            "subscription_check_auth_mode": "required",
            "subscription_check_token": "",
        },
        {"subscription_check_token": "short"},
        {"subscription_check_auth_mode": "invalid-mode-sentinel"},
    ],
    ids=[
        "missing-url",
        "invalid-url",
        "wrong-scheme",
        "embedded-credentials",
        "query-string",
        "fragment",
        "base-path",
        "required-token-missing",
        "invalid-token-format",
        "invalid-auth-mode",
    ],
)
def test_enabled_mode_rejects_missing_or_unsafe_configuration(overrides):
    with pytest.raises(RuntimeError, match="Invalid subscription check configuration"):
        values = {
            "subscription_check_enabled": True,
            "subscription_service_url": BASE_URL,
            **overrides,
        }
        Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"internal_api_token": VALID_TOKEN},
        {"chat_s2s_token": SecretStr(VALID_TOKEN)},
        {
            "business_conversation_url": "http://conversation-service:8000",
            "business_conversation_token": SecretStr(VALID_TOKEN),
        },
        {
            "handoff_conversation_url": "http://conversation-service:8000",
            "handoff_lifecycle_token": SecretStr(VALID_TOKEN),
        },
        {
            "managed_ai_auth_principals": json.dumps(
                [
                    {
                        "principal_id": "conversation-tests",
                        "token": VALID_TOKEN,
                        "caller": "conversation-service",
                        "execution_namespace": "main-chota.conversation.v1",
                        "grants": [
                            {
                                "owner_id": "shop-1",
                                "integration_id": str(uuid4()),
                                "channel": "TELEGRAM",
                                "agent_id": str(uuid4()),
                                "agent_owner_id": str(uuid4()),
                            }
                        ],
                    }
                ]
            )
        },
    ],
    ids=["internal", "chat", "business", "handoff", "managed-principal"],
)
def test_subscription_credential_cannot_be_reused_for_other_authorities(overrides):
    with pytest.raises(RuntimeError, match="Invalid subscription check configuration"):
        Settings(
            _env_file=None,
            subscription_check_enabled=True,
            subscription_service_url=BASE_URL,
            subscription_check_auth_mode="required",
            subscription_check_token=SecretStr(VALID_TOKEN),
            **overrides,
        )


def test_configuration_errors_never_render_the_supplied_secret():
    with pytest.raises(RuntimeError) as exc_info:
        Settings(
            _env_file=None,
            subscription_check_enabled=True,
            subscription_service_url=BASE_URL,
            subscription_check_auth_mode="required",
            subscription_check_token=SecretStr("invalid-secret-value"),
        )

    assert "invalid-secret-value" not in str(exc_info.value)


@pytest.mark.parametrize("principal_kind", ["ownership", "gateway"])
def test_subscription_token_cannot_match_any_versioned_owner_credential(principal_kind):
    current = SimpleNamespace(token=SecretStr("a" * 32), revoked=False)
    previous = SimpleNamespace(token=SecretStr(VALID_TOKEN), revoked=True)

    class FuturePrincipal:
        def credentials(self):
            return current, previous

    principal = FuturePrincipal()

    class FuturePrincipalSettings(Settings):
        @property
        def ownership_principals(self):
            return (principal,) if principal_kind == "ownership" else ()

        @property
        def gateway_principals(self):
            return (principal,) if principal_kind == "gateway" else ()

    with pytest.raises(RuntimeError, match="Invalid subscription check configuration"):
        FuturePrincipalSettings(
            _env_file=None,
            subscription_check_enabled=True,
            subscription_service_url=BASE_URL,
            subscription_check_auth_mode="required",
            subscription_check_token=SecretStr(VALID_TOKEN),
        )
