"""Optional P0a adapter; invoke scripts/run_p0a_ai.py with the pinned metrics repo.

The upstream secure assertions remain unchanged. Only receiver provisioning and
the repaired purpose gap's xfail marker differ from the P0a baseline.
"""

import json
import secrets

import pytest

from core.config import get_settings
from core.managed_security import parse_ownership_principals
from security.cases import test_ai as upstream
from security.cases.support import AGENT_A, OWNER_A

# Reuse the actual P0a tests and managed fixtures, including the separate owner
# admission gap that is intentionally outside P1a.
from security.cases.test_ai import (  # noqa: F401
    managed,
    test_owner_missing_or_malformed_identity_does_not_read,
    test_forged_owner_admin_headers_require_gateway_identity,
    test_strict_legacy_chat_rejects_missing_wrong_and_other_purpose_tokens,
    test_strict_legacy_chat_valid_token_preserves_owner_context,
    test_internal_ownership_rejects_other_purpose_before_lookup,
    test_managed_exact_grant_positive_control,
    test_managed_grants_cannot_be_combined_across_tenants,
    test_managed_owner_mapping_cannot_change_between_grants,
    test_managed_rejects_unauthenticated_before_parsing_body,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.security_seam("asgi")]


@pytest.fixture
def boundary(monkeypatch):
    legacy_fixture = upstream.boundary.__wrapped__(monkeypatch)
    app, calls, chat_token, management_token = next(legacy_fixture)
    ownership_token = secrets.token_urlsafe(36)
    registry = [dict(
        principal_id="p0a-cp-ownership", caller="control-plane-service",
        purpose="agent-ownership-check",
        current=dict(version="v1", token=ownership_token),
        grants=[dict(owner_id=str(OWNER_A), agent_id=str(AGENT_A))],
    )]
    principals = parse_ownership_principals(json.dumps(registry), (chat_token, management_token))
    monkeypatch.setattr(get_settings(), "_ownership_principals", principals)
    monkeypatch.setattr(get_settings(), "internal_ownership_auth_required", True)
    calls.ownership_token = ownership_token
    try:
        yield app, calls, chat_token, management_token
    finally:
        next(legacy_fixture, None)


@pytest.fixture
def ownership_boundary(boundary):
    app, calls, chat_token, _ = boundary
    return app, calls, chat_token, calls.ownership_token


async def test_internal_ownership_valid_token_preserves_pair(ownership_boundary):
    await upstream.test_internal_ownership_valid_token_preserves_pair(ownership_boundary)


async def test_ownership_credential_cannot_read_management_counts(ownership_boundary):
    # Calling the exact upstream assertion without its former gap marker makes
    # an unexpected admission an ordinary test failure.
    await upstream.test_ownership_credential_cannot_read_management_counts(ownership_boundary)
