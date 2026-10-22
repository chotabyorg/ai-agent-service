"""Startup configuration failures must not render configured S2S credentials."""

import json
import traceback

import pytest

from core.config import Settings


def test_missing_strict_chat_token_does_not_leak_ownership_registry() -> None:
    token = "ownership-registry-last-token-0123456789"
    registry = json.dumps([
        {
            "principal_id": "startup-privacy",
            "caller": "control-plane-service",
            "purpose": "agent-ownership-check",
            "current": {"version": "v1", "token": token, "revoked": False},
            "revoked": False,
            "all_tenants": False,
            "grants": [{
                "owner_id": "11111111-1111-1111-1111-111111111111",
                "agent_id": "22222222-2222-2222-2222-222222222222",
            }],
        }
    ])

    with pytest.raises(RuntimeError) as exc_info:
        Settings(
            _env_file=None,
            internal_ai_auth_principals=registry,
            internal_ownership_auth_required=True,
            chat_s2s_auth_required=True,
            chat_s2s_token="",
        )

    error = exc_info.value
    assert str(error) == (
        "CHAT_S2S_TOKEN must be configured when CHAT_S2S_AUTH_REQUIRED is enabled"
    )
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    assert token not in rendered
    assert registry not in rendered
    assert "input_value" not in rendered
    assert "ValidationError" not in rendered
