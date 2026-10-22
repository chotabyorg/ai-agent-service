"""Purpose-scoped admission for AI internal ownership checks.

These tests exercise the real FastAPI routes and settings validator. They use
synthetic credentials and stub only the database/service boundary; no lifespan,
Redis, or provider calls are started.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, ANY
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from api.dependencies import get_db
from api.internal import agents as internal_agents
from core.config import Settings, get_settings
from main import app
from mcp_clients.manager import MCPManager


OWNER_A = UUID(int=1101)
OWNER_B = UUID(int=1102)
AGENT_A = UUID(int=1201)
AGENT_B = UUID(int=1202)

TOKEN_OWNERSHIP = "ownership-principal-token-" + "a" * 32
TOKEN_OWNERSHIP_PREVIOUS = "ownership-principal-token-" + "b" * 32
TOKEN_OWNERSHIP_REVOKED = "ownership-principal-token-" + "c" * 32
TOKEN_SECOND_PRINCIPAL = "ownership-secondary-principal-" + "d" * 32
TOKEN_INTERNAL = "legacy-internal-token-" + "e" * 32
TOKEN_CHAT = "chat-s2s-token-" + "f" * 32
TOKEN_MANAGED = "managed-ai-token-" + "g" * 32
TOKEN_BUSINESS = "business-conversation-token-" + "h" * 32

OWNERSHIP_PATH = "/internal/v1/agents/ownership-check"
COUNT_PATH = "/internal/v1/agents/count"
DEACTIVATE_PATH = "/internal/v1/agents/deactivate-all"


def ownership_principal(
    token: str = TOKEN_OWNERSHIP,
    *,
    principal_id: str = "control-plane-main",
    caller: str = "control-plane-service",
    purpose: str = "agent-ownership-check",
    version: str = "v2",
    previous: dict[str, object] | None = None,
    revoked: bool = False,
    current_revoked: bool = False,
    all_tenants: bool = False,
    grants: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    if grants is None:
        grants = [{"owner_id": str(OWNER_A), "agent_id": str(AGENT_A)}]
    return {
        "principal_id": principal_id,
        "caller": caller,
        "purpose": purpose,
        "current": {
            "version": version,
            "token": token,
            "revoked": current_revoked,
        },
        **({"previous": previous} if previous is not None else {}),
        "revoked": revoked,
        "all_tenants": all_tenants,
        "grants": grants,
    }


def ownership_registry(*principals: dict[str, object]) -> str:
    return json.dumps(list(principals))


def managed_registry(token: str = TOKEN_MANAGED) -> str:
    return json.dumps([
        {
            "principal_id": "conversation-test",
            "token": token,
            "caller": "conversation-service",
            "execution_namespace": "main-chota.conversation.v1",
            "grants": [
                {
                    "owner_id": str(OWNER_A),
                    "integration_id": str(UUID(int=1301)),
                    "channel": "TELEGRAM",
                    "agent_id": str(AGENT_A),
                    "agent_owner_id": str(OWNER_A),
                }
            ],
        }
    ])


def settings_for_registry(
    registry: str,
    *,
    strict: bool = True,
    internal_token: str = TOKEN_INTERNAL,
    chat_token: str = TOKEN_CHAT,
    managed_token: str = TOKEN_MANAGED,
    business_token: str = TOKEN_BUSINESS,
) -> Settings:
    return Settings(
        _env_file=None,
        internal_ai_auth_principals=registry,
        internal_ownership_auth_required=strict,
        internal_api_token=internal_token,
        chat_s2s_token=chat_token,
        managed_ai_auth_principals=managed_registry(managed_token),
        business_conversation_url="http://conversation-service:8000",
        business_conversation_token=business_token,
    )


def ownership_body(owner_id: UUID = OWNER_A, agent_id: UUID = AGENT_A) -> dict[str, str]:
    return {"owner_id": str(owner_id), "agent_id": str(agent_id)}


def token_header(token: str) -> dict[str, str]:
    return {"X-Service-Token": token}


def ambiguous_service_headers(token: str) -> dict[str, list[tuple[bytes, bytes]]]:
    token_bytes = token.encode()
    authorization = ("Bearer " + token).encode()
    return {
        "duplicate-case-variant": [
            (b"x-service-token", token_bytes),
            (b"X-Service-Token", token_bytes),
        ],
        "comma-list": [(b"X-Service-Token", token_bytes + b"," + token_bytes)],
        "authorization-only": [(b"Authorization", authorization)],
        "authorization-plus-token": [
            (b"X-Service-Token", token_bytes),
            (b"Authorization", authorization),
        ],
        "non-ascii": [(b"X-Service-Token", b"non-ascii-\xff")],
    }


@pytest.fixture
async def internal_auth_context(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SimpleNamespace]:
    """Use the actual application with a synthetic registry and counted DB seam."""
    registry = ownership_registry(ownership_principal())
    monkeypatch.setenv("INTERNAL_AI_AUTH_PRINCIPALS", registry)
    monkeypatch.setenv("INTERNAL_OWNERSHIP_AUTH_REQUIRED", "true")
    monkeypatch.setenv("INTERNAL_API_TOKEN", TOKEN_INTERNAL)
    monkeypatch.setenv("CHAT_S2S_TOKEN", TOKEN_CHAT)
    monkeypatch.setenv("MANAGED_AI_AUTH_PRINCIPALS", managed_registry())
    monkeypatch.setenv("BUSINESS_CONVERSATION_URL", "http://conversation-service:8000")
    monkeypatch.setenv("BUSINESS_CONVERSATION_TOKEN", TOKEN_BUSINESS)
    get_settings.cache_clear()

    calls = SimpleNamespace(
        db=0,
        ownership=AsyncMock(return_value=object()),
        count=AsyncMock(return_value=(2, 1)),
        deactivate=AsyncMock(return_value=1),
    )

    async def fake_db() -> AsyncIterator[object]:
        calls.db += 1
        yield object()

    previous_overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = fake_db
    monkeypatch.setattr(internal_agents.agent_service, "get_agent_by_id", calls.ownership)
    monkeypatch.setattr(internal_agents.agent_service, "count_agents", calls.count)
    monkeypatch.setattr(internal_agents.agent_service, "deactivate_all_agents", calls.deactivate)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            yield SimpleNamespace(client=client, calls=calls)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous_overrides)
        get_settings.cache_clear()


@pytest.mark.parametrize("caller", ["control-plane-service", "knowledge-base-service"])
def test_settings_accepts_supported_ownership_principals(caller: str) -> None:
    settings = settings_for_registry(
        ownership_registry(ownership_principal(caller=caller)),
    )

    assert settings.internal_ownership_auth_required is True
    assert len(settings.ownership_principals) == 1
    assert settings.ownership_principals[0].caller == caller
    assert settings.ownership_principals[0].purpose == "agent-ownership-check"
    assert TOKEN_OWNERSHIP not in repr(settings)


def test_settings_allows_same_grant_for_distinct_service_principals() -> None:
    settings = settings_for_registry(ownership_registry(
        ownership_principal(principal_id="cp", caller="control-plane-service"),
        ownership_principal(
            TOKEN_SECOND_PRINCIPAL,
            principal_id="kb",
            caller="knowledge-base-service",
        ),
    ))

    assert {principal.caller for principal in settings.ownership_principals} == {
        "control-plane-service",
        "knowledge-base-service",
    }


@pytest.mark.parametrize(
    "principal_changes",
    [
        {"caller": "cp-service"},
        {"purpose": "chat-completions"},
        {"all_tenants": True, "grants": [{"owner_id": str(OWNER_A), "agent_id": str(AGENT_A)}]},
        {"all_tenants": False, "grants": []},
        {
            "previous": [
                {"version": "v1", "token": TOKEN_OWNERSHIP_PREVIOUS, "revoked": False},
                {"version": "v0", "token": TOKEN_OWNERSHIP_REVOKED, "revoked": False},
            ]
        },
    ],
)
def test_settings_rejects_invalid_principal_scope_or_shape(
    principal_changes: dict[str, object],
) -> None:
    principal = ownership_principal()
    principal.update(principal_changes)

    with pytest.raises(RuntimeError):
        settings_for_registry(ownership_registry(principal))


def test_settings_rejects_invalid_tokens_and_duplicate_principal_or_version_ids() -> None:
    cases = [
        ownership_registry(ownership_principal(token="too-short")),
        ownership_registry(ownership_principal(token="ownership-non-ascii-" + "e" * 30 + "é")),
        ownership_registry(
            ownership_principal(principal_id="duplicate"),
            ownership_principal(
                TOKEN_SECOND_PRINCIPAL,
                principal_id="duplicate",
                grants=[{"owner_id": str(OWNER_B), "agent_id": str(AGENT_B)}],
            ),
        ),
        ownership_registry(ownership_principal(previous={
            "version": "v2", "token": TOKEN_OWNERSHIP_PREVIOUS, "revoked": False,
        })),
    ]

    for registry in cases:
        with pytest.raises(RuntimeError):
            settings_for_registry(registry)


@pytest.mark.parametrize(
    "collision",
    [TOKEN_INTERNAL, TOKEN_CHAT, TOKEN_MANAGED, TOKEN_BUSINESS],
)
def test_settings_rejects_ownership_token_collisions_with_other_purposes(collision: str) -> None:
    with pytest.raises(RuntimeError) as exc_info:
        settings_for_registry(ownership_registry(ownership_principal(token=collision)))

    assert collision not in str(exc_info.value)


@pytest.mark.parametrize(
    "duplicate_token",
    ["current", "previous"],
)
def test_settings_rejects_duplicate_tokens_even_on_revoked_versions(duplicate_token: str) -> None:
    first = ownership_principal(
        TOKEN_OWNERSHIP,
        principal_id="revoked-principal",
        revoked=True,
        current_revoked=True,
        previous={"version": "v1", "token": TOKEN_OWNERSHIP_REVOKED, "revoked": True},
    )
    if duplicate_token == "current":
        second = ownership_principal(
            TOKEN_OWNERSHIP,
            principal_id="active-principal",
            grants=[{"owner_id": str(OWNER_B), "agent_id": str(AGENT_B)}],
        )
    else:
        second = ownership_principal(
            TOKEN_SECOND_PRINCIPAL,
            principal_id="active-principal",
            grants=[{"owner_id": str(OWNER_B), "agent_id": str(AGENT_B)}],
            previous={"version": "old", "token": TOKEN_OWNERSHIP_REVOKED, "revoked": False},
        )

    with pytest.raises(RuntimeError):
        settings_for_registry(ownership_registry(first, second))


def test_settings_rejects_duplicate_json_keys() -> None:
    registry = (
        '[{"principal_id":"one","principal_id":"two",'
        '"caller":"control-plane-service","purpose":"agent-ownership-check",'
        '"current":{"version":"v2","token":"' + TOKEN_OWNERSHIP + '","revoked":false},'
        '"revoked":false,"all_tenants":false,"grants":[{"owner_id":"' + str(OWNER_A)
        + '","agent_id":"' + str(AGENT_A) + '"}]}]'
    )

    with pytest.raises(RuntimeError):
        settings_for_registry(registry)


def test_strict_settings_reject_empty_or_fully_revoked_registry() -> None:
    with pytest.raises(RuntimeError):
        settings_for_registry("[]")

    revoked = ownership_principal(revoked=True, current_revoked=True)
    with pytest.raises(RuntimeError):
        settings_for_registry(ownership_registry(revoked))


def test_invalid_ownership_registry_fails_even_when_strict_mode_is_disabled() -> None:
    with pytest.raises(RuntimeError):
        settings_for_registry("not-json", strict=False)


def test_ownership_registry_is_not_exported_to_mcp_child_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ownership_registry(ownership_principal())
    for key in (
        "INTERNAL_AI_AUTH_PRINCIPALS",
        "internal_ai_auth_principals",
        "Internal_AI_Auth_Principals",
    ):
        monkeypatch.setenv(key, registry)
    get_settings.cache_clear()
    settings = get_settings()

    try:
        env = MCPManager("bitrix", {"webhook_url": "synthetic-bitrix-credential"})._build_env()
        assert all(key.lower() != "internal_ai_auth_principals" for key in env)
        assert env["B24_WEBHOOK_URL"] == "synthetic-bitrix-credential"

        for config in (
            {
                "script_path": "none",
                "env_mapping": {"webhook_url": "internal_ai_auth_principals"},
            },
            {
                "script_path": "none",
                "env_mapping": {},
                "optional_env_mapping": {
                    "webhook_url": "InTeRnAl_Ai_Auth_Principals",
                },
            },
        ):
            monkeypatch.setattr(settings, "mcp_services", {"bitrix": config})
            with pytest.raises(
                RuntimeError,
                match="^MCP environment configuration is invalid$",
            ) as exc_info:
                MCPManager("bitrix", {"webhook_url": "synthetic-tool-value"})._build_env()
            assert registry not in str(exc_info.value)
            assert TOKEN_OWNERSHIP not in str(exc_info.value)
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize(
    "token",
    [TOKEN_OWNERSHIP, TOKEN_OWNERSHIP_PREVIOUS],
)
async def test_current_and_previous_ownership_versions_admit_exact_grant(
    internal_auth_context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    token: str,
) -> None:
    principal = ownership_principal(
        previous={"version": "v1", "token": TOKEN_OWNERSHIP_PREVIOUS, "revoked": False},
    )
    monkeypatch.setenv("INTERNAL_AI_AUTH_PRINCIPALS", ownership_registry(principal))
    get_settings.cache_clear()
    try:
        response = await internal_auth_context.client.post(
            OWNERSHIP_PATH,
            json=ownership_body(),
            headers=token_header(token),
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 204
    internal_auth_context.calls.ownership.assert_awaited_once_with(
        ANY,
        AGENT_A,
        user_id=OWNER_A,
    )
    assert internal_auth_context.calls.db == 1


async def test_removed_previous_ownership_version_is_denied_while_current_remains_valid(
    internal_auth_context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "INTERNAL_AI_AUTH_PRINCIPALS",
        ownership_registry(ownership_principal()),
    )
    get_settings.cache_clear()
    try:
        current = await internal_auth_context.client.post(
            OWNERSHIP_PATH,
            json=ownership_body(),
            headers=token_header(TOKEN_OWNERSHIP),
        )
        assert current.status_code == 204
        internal_auth_context.calls.db = 0
        internal_auth_context.calls.ownership.reset_mock()

        response = await internal_auth_context.client.post(
            OWNERSHIP_PATH,
            json=ownership_body(),
            headers=token_header(TOKEN_OWNERSHIP_PREVIOUS),
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 401
    assert internal_auth_context.calls.db == 0
    internal_auth_context.calls.ownership.assert_not_awaited()


@pytest.mark.parametrize(
    "token",
    ["missing", "wrong", "legacy", "chat", "managed", "business"],
)
async def test_strict_ownership_route_rejects_missing_or_other_purpose_tokens_before_db(
    internal_auth_context: SimpleNamespace,
    token: str,
) -> None:
    supplied = {
        "missing": None,
        "wrong": "synthetic-wrong-token-" + "x" * 32,
        "legacy": TOKEN_INTERNAL,
        "chat": TOKEN_CHAT,
        "managed": TOKEN_MANAGED,
        "business": TOKEN_BUSINESS,
    }[token]
    headers = {} if supplied is None else token_header(supplied)

    response = await internal_auth_context.client.post(
        OWNERSHIP_PATH,
        json=ownership_body(),
        headers=headers,
    )

    assert response.status_code == 401
    assert internal_auth_context.calls.db == 0
    internal_auth_context.calls.ownership.assert_not_awaited()


@pytest.mark.parametrize(
    "principal",
    [
        ownership_principal(revoked=True),
        ownership_principal(current_revoked=True),
        ownership_principal(previous={
            "version": "v1", "token": TOKEN_OWNERSHIP_PREVIOUS, "revoked": True,
        }),
    ],
)
async def test_revoked_principal_or_version_is_denied_before_db(
    internal_auth_context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    principal: dict[str, object],
) -> None:
    active = ownership_principal(
        TOKEN_SECOND_PRINCIPAL,
        principal_id="active-fallback",
        grants=[{"owner_id": str(OWNER_B), "agent_id": str(AGENT_B)}],
    )
    monkeypatch.setenv(
        "INTERNAL_AI_AUTH_PRINCIPALS",
        ownership_registry(principal, active),
    )
    get_settings.cache_clear()
    token = (
        TOKEN_OWNERSHIP_PREVIOUS
        if principal.get("previous")
        else TOKEN_OWNERSHIP
    )
    try:
        response = await internal_auth_context.client.post(
            OWNERSHIP_PATH,
            json=ownership_body(),
            headers=token_header(token),
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 401
    assert internal_auth_context.calls.db == 0
    internal_auth_context.calls.ownership.assert_not_awaited()


@pytest.mark.parametrize(
    "owner_id,agent_id",
    [(OWNER_B, AGENT_A), (OWNER_A, AGENT_B)],
)
async def test_grant_miss_is_indistinguishable_404_before_db(
    internal_auth_context: SimpleNamespace,
    owner_id: UUID,
    agent_id: UUID,
) -> None:
    response = await internal_auth_context.client.post(
        OWNERSHIP_PATH,
        json=ownership_body(owner_id, agent_id),
        headers={
            **token_header(TOKEN_OWNERSHIP),
            "X-User-Id": str(OWNER_B),
            "X-User-Role": "admin",
        },
    )

    assert response.status_code == 404
    assert response.json() == {"detail": "Agent not found"}
    assert internal_auth_context.calls.db == 0
    internal_auth_context.calls.ownership.assert_not_awaited()


async def test_all_tenants_principal_accepts_owner_pair_without_grants(
    internal_auth_context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = ownership_principal(
        all_tenants=True,
        grants=[],
    )
    monkeypatch.setenv("INTERNAL_AI_AUTH_PRINCIPALS", ownership_registry(principal))
    get_settings.cache_clear()
    try:
        response = await internal_auth_context.client.post(
            OWNERSHIP_PATH,
            json=ownership_body(OWNER_B, AGENT_B),
            headers=token_header(TOKEN_OWNERSHIP),
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 204
    internal_auth_context.calls.ownership.assert_awaited_once_with(
        ANY,
        AGENT_B,
        user_id=OWNER_B,
    )


async def test_two_exact_grants_do_not_create_cartesian_tenant_authority(
    internal_auth_context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = ownership_principal(
        grants=[
            {"owner_id": str(OWNER_A), "agent_id": str(AGENT_A)},
            {"owner_id": str(OWNER_B), "agent_id": str(AGENT_B)},
        ],
    )
    monkeypatch.setenv("INTERNAL_AI_AUTH_PRINCIPALS", ownership_registry(principal))
    get_settings.cache_clear()
    try:
        for owner_id, agent_id in ((OWNER_A, AGENT_A), (OWNER_B, AGENT_B)):
            allowed = await internal_auth_context.client.post(
                OWNERSHIP_PATH,
                json=ownership_body(owner_id, agent_id),
                headers=token_header(TOKEN_OWNERSHIP),
            )
            assert allowed.status_code == 204

        assert internal_auth_context.calls.db == 2
        internal_auth_context.calls.db = 0
        internal_auth_context.calls.ownership.reset_mock()

        for owner_id, agent_id in ((OWNER_A, AGENT_B), (OWNER_B, AGENT_A)):
            denied = await internal_auth_context.client.post(
                OWNERSHIP_PATH,
                json=ownership_body(owner_id, agent_id),
                headers=token_header(TOKEN_OWNERSHIP),
            )
            assert denied.status_code == 404
            assert denied.json() == {"detail": "Agent not found"}
    finally:
        get_settings.cache_clear()

    assert internal_auth_context.calls.db == 0
    internal_auth_context.calls.ownership.assert_not_awaited()


@pytest.mark.parametrize(
    "shape",
    list(ambiguous_service_headers(TOKEN_OWNERSHIP)),
)
async def test_ambiguous_or_malformed_auth_headers_are_rejected_before_db(
    internal_auth_context: SimpleNamespace,
    shape: str,
) -> None:
    response = await internal_auth_context.client.post(
        OWNERSHIP_PATH,
        json=ownership_body(),
        headers=ambiguous_service_headers(TOKEN_OWNERSHIP)[shape],
    )

    assert response.status_code == 401
    assert internal_auth_context.calls.db == 0
    internal_auth_context.calls.ownership.assert_not_awaited()


@pytest.mark.parametrize(
    "shape",
    list(ambiguous_service_headers(TOKEN_INTERNAL)),
)
async def test_management_routes_reject_ambiguous_or_malformed_auth_headers_before_db(
    internal_auth_context: SimpleNamespace,
    shape: str,
) -> None:
    response = await internal_auth_context.client.get(
        COUNT_PATH,
        params={"user_id": str(OWNER_A)},
        headers=ambiguous_service_headers(TOKEN_INTERNAL)[shape],
    )

    assert response.status_code == 401
    assert internal_auth_context.calls.db == 0
    internal_auth_context.calls.count.assert_not_awaited()


async def test_ownership_token_cannot_read_management_counts_or_deactivate_agents(
    internal_auth_context: SimpleNamespace,
) -> None:
    count = await internal_auth_context.client.get(
        COUNT_PATH,
        params={"user_id": str(OWNER_A)},
        headers=token_header(TOKEN_OWNERSHIP),
    )
    deactivate = await internal_auth_context.client.post(
        DEACTIVATE_PATH,
        json={"user_id": str(OWNER_A)},
        headers=token_header(TOKEN_OWNERSHIP),
    )

    assert count.status_code == 401
    assert deactivate.status_code == 401
    assert internal_auth_context.calls.db == 0
    internal_auth_context.calls.count.assert_not_awaited()
    internal_auth_context.calls.deactivate.assert_not_awaited()


async def test_management_token_still_authorizes_count_and_deactivate_routes(
    internal_auth_context: SimpleNamespace,
) -> None:
    count = await internal_auth_context.client.get(
        COUNT_PATH,
        params={"user_id": str(OWNER_A)},
        headers=token_header(TOKEN_INTERNAL),
    )
    deactivate = await internal_auth_context.client.post(
        DEACTIVATE_PATH,
        json={"user_id": str(OWNER_A)},
        headers=token_header(TOKEN_INTERNAL),
    )

    assert count.status_code == 200
    assert deactivate.status_code == 200
    internal_auth_context.calls.count.assert_awaited_once()
    internal_auth_context.calls.deactivate.assert_awaited_once()
    assert internal_auth_context.calls.db == 2


async def test_legacy_internal_token_remains_compatible_when_strict_mode_is_disabled(
    internal_auth_context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTERNAL_OWNERSHIP_AUTH_REQUIRED", "false")
    monkeypatch.setenv("INTERNAL_AI_AUTH_PRINCIPALS", "")
    get_settings.cache_clear()
    try:
        response = await internal_auth_context.client.post(
            OWNERSHIP_PATH,
            json=ownership_body(),
            headers=token_header(TOKEN_INTERNAL),
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 204
    internal_auth_context.calls.ownership.assert_awaited_once_with(
        ANY,
        AGENT_A,
        user_id=OWNER_A,
    )


@pytest.mark.parametrize(
    "token",
    [None, "synthetic-invalid-legacy-token-" + "z" * 32],
    ids=["missing", "invalid"],
)
async def test_legacy_transition_still_rejects_missing_or_invalid_tokens(
    internal_auth_context: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    token: str | None,
) -> None:
    monkeypatch.setenv("INTERNAL_OWNERSHIP_AUTH_REQUIRED", "false")
    monkeypatch.setenv("INTERNAL_AI_AUTH_PRINCIPALS", "")
    get_settings.cache_clear()
    try:
        response = await internal_auth_context.client.post(
            OWNERSHIP_PATH,
            json=ownership_body(),
            headers={} if token is None else token_header(token),
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 401
    assert internal_auth_context.calls.db == 0
    internal_auth_context.calls.ownership.assert_not_awaited()


@pytest.mark.integration
async def test_database_owner_filter_still_hides_foreign_agent_when_registry_grant_matches(
    api_client: AsyncClient,
    db_session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured grant cannot replace the SQL owner predicate."""
    from models.agent import AgentTemplate

    owner_a, owner_b = uuid4(), uuid4()
    agent = AgentTemplate(
        user_id=owner_b,
        name="Foreign agent",
        system_prompt="",
        llm_provider="openrouter",
        model_name="google/gemini-2.5-flash",
        temperature=0.5,
        is_active=True,
    )
    async with db_session() as session:
        session.add(agent)
        await session.commit()
        agent_id = agent.id

    registry = ownership_registry(
        ownership_principal(
            grants=[{"owner_id": str(owner_a), "agent_id": str(agent_id)}],
        )
    )
    monkeypatch.setenv("INTERNAL_AI_AUTH_PRINCIPALS", registry)
    monkeypatch.setenv("INTERNAL_OWNERSHIP_AUTH_REQUIRED", "true")
    monkeypatch.setenv("INTERNAL_API_TOKEN", TOKEN_INTERNAL)
    monkeypatch.setenv("CHAT_S2S_TOKEN", TOKEN_CHAT)
    monkeypatch.setenv("MANAGED_AI_AUTH_PRINCIPALS", managed_registry())
    monkeypatch.setenv("BUSINESS_CONVERSATION_URL", "http://conversation-service:8000")
    monkeypatch.setenv("BUSINESS_CONVERSATION_TOKEN", TOKEN_BUSINESS)
    get_settings.cache_clear()
    try:
        response = await api_client.post(
            OWNERSHIP_PATH,
            json={"owner_id": str(owner_a), "agent_id": str(agent_id)},
            headers=token_header(TOKEN_OWNERSHIP),
        )
    finally:
        get_settings.cache_clear()

    assert response.status_code == 404
    assert response.json() == {"detail": "Agent not found"}
