"""Receiver admission with real routes, synthetic identities and explicit seams."""

import json
import secrets
import traceback
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from api.dependencies import get_db
from api.gateway_owner import require_owner_gateway
from core.config import Settings, get_settings
from core.managed_security import MANAGED_SECRET_ENV_KEYS
from main import app
from mcp_clients.manager import MCPManager

try:
    # FastAPI >=0.137 keeps included routers rather than cloning their routes.
    from fastapi.routing import iter_route_contexts
except ImportError:
    iter_route_contexts = None


def effective_api_routes():
    """Include inherited dependencies/prefixes, hidden and duplicate routes."""
    if iter_route_contexts is None:
        return [route for route in app.routes if isinstance(route, APIRoute)]
    return [context for context in iter_route_contexts(app.routes)
            if isinstance(context.original_route, APIRoute)]


OWNER = UUID(int=1101)
OTHER_OWNER = UUID(int=1102)
AGENT = UUID(int=1201)
TOKEN = "ai-gateway-current-" + "a" * 32
PREVIOUS = "ai-gateway-previous-" + "b" * 32
INTERNAL = "ai-internal-management-" + "c" * 32
CHAT = "ai-chat-service-token-" + "d" * 32
MANAGED = "ai-managed-principal-" + "e" * 32
OWNERSHIP = "ai-ownership-principal-" + "f" * 32
BUSINESS = "ai-business-authority-" + "g" * 32
PATH = f"/v1/agents/{AGENT}"
OWNER_ROUTES = [
    ("POST", "/v1/agents/"), ("GET", "/v1/agents/"),
    ("GET", PATH), ("PUT", PATH), ("DELETE", PATH),
    ("GET", PATH + "/prompt-config"), ("PUT", PATH + "/prompt-config"),
    ("GET", PATH + "/prompt-preview"),
    ("GET", PATH + "/credentials"), ("PUT", PATH + "/credentials"),
    ("DELETE", PATH + "/credentials/bitrix"),
    ("GET", PATH + "/tools/bitrix"), ("PUT", PATH + "/tools/bitrix"),
    ("PUT", PATH + "/openrouter-key"), ("GET", PATH + "/openrouter-key"),
    ("DELETE", PATH + "/openrouter-key"),
    ("GET", PATH + "/business-tools/workspaces"),
    ("GET", PATH + "/business-tools"), ("PUT", PATH + "/business-tools"),
    ("PATCH", PATH + "/business-tools"), ("DELETE", PATH + "/business-tools"),
]


def principal(**changes):
    return {
        "principal_id": "gateway-ai-main", "caller": "chota-gateway",
        "purpose": "gateway-owner-api", "audience": "ai-agent-service",
        "current": {"version": "v2", "token": TOKEN},
        "previous": {"version": "v1", "token": PREVIOUS},
        **changes,
    }


def configured(**changes):
    return Settings(_env_file=None, **{
        "gateway_ai_auth_principals": json.dumps([principal()]),
        "gateway_owner_auth_mode": "required",
        "internal_api_token": INTERNAL, "chat_s2s_token": CHAT,
        "managed_ai_auth_principals": json.dumps([{
            "principal_id": "conversation-test", "token": MANAGED,
            "caller": "conversation-service",
            "execution_namespace": "main-chota.conversation.v1",
            "grants": [{"owner_id": str(OWNER), "agent_owner_id": str(OWNER),
                        "agent_id": str(AGENT), "integration_id": str(UUID(int=1301)),
                        "channel": "TELEGRAM"}],
        }]),
        "internal_ai_auth_principals": json.dumps([{
            "principal_id": "kb-test", "caller": "knowledge-base-service",
            "purpose": "agent-ownership-check",
            "current": {"version": "v1", "token": OWNERSHIP},
            "all_tenants": True,
        }]),
        "business_conversation_url": "http://conversation-service:8000",
        "business_conversation_token": BUSINESS,
        **changes,
    })


def headers(token=TOKEN, owner=OWNER, role="member"):
    result = {"X-User-Id": str(owner)}
    if token is not None:
        result["X-Gateway-Token"] = token
    if role is not None:
        result["X-User-Role"] = role
    return result


@pytest.fixture
async def admission(monkeypatch):
    settings = configured()
    monkeypatch.setattr("api.gateway_owner.get_settings", lambda: settings)
    calls = SimpleNamespace(db=0)

    async def database_boundary():
        calls.db += 1
        raise HTTPException(418, detail="Synthetic database boundary reached")
        yield  # This must stay a yield dependency like the real DB provider.

    previous = app.dependency_overrides.copy()
    app.dependency_overrides[get_db] = database_boundary
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
            yield SimpleNamespace(client=client, calls=calls, settings=settings)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)


@pytest.mark.parametrize("method,path", OWNER_ROUTES)
@pytest.mark.parametrize("token", [None, INTERNAL, CHAT, MANAGED, OWNERSHIP, BUSINESS, "wrong"])
async def test_owner_cohort_rejects_before_database_or_service(admission, method, path, token):
    response = await admission.client.request(method, path, headers=headers(token), json={})
    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid gateway authentication"}
    assert admission.calls.db == 0


@pytest.mark.parametrize("method,path", OWNER_ROUTES)
@pytest.mark.parametrize("token", [TOKEN, PREVIOUS])
async def test_current_and_previous_admit_every_owner_operation(admission, method, path, token):
    response = await admission.client.request(method, path, headers=headers(token), json={})
    assert response.status_code == 418
    assert admission.calls.db == 1


@pytest.mark.parametrize("role", [None, "", "member", "admin", "custom-role"])
async def test_optional_role_preserves_existing_semantics(admission, role):
    response = await admission.client.get(PATH, headers=headers(role=role))
    assert response.status_code == 418


def malformed_headers():
    base = [(b"X-Gateway-Token", TOKEN.encode()), (b"X-User-Id", str(OWNER).encode())]
    return [
        [*base, (b"x-gateway-token", TOKEN.encode())],
        [*base, (b"x-user-id", str(OWNER).encode())],
        [*base, (b"X-USER-ID", str(OTHER_OWNER).encode())],
        [*base, (b"x-user-role", b"admin"), (b"X-User-Role", b"member")],
        [*base, (b"x-user-role", b"admin,member")],
        [*base, (b"x-user-role", b" admin")],
        [*base, (b"x-user-role", b"admin\xff")],
        [*base, (b"Authorization", b"Bearer synthetic")],
        [*base, (b"X-Service-Token", INTERNAL.encode())],
        [(b"X-Gateway-Token", TOKEN.encode() + b"," + TOKEN.encode()), base[1]],
        [(b"X-Gateway-Token", b" " + TOKEN.encode()), base[1]],
        [(b"X-Gateway-Token", TOKEN.encode() + b"\n"), base[1]],
        [(b"X-Gateway-Token", b"non-ascii-\xff"), base[1]],
        [(b"X-Gateway-Token", b""), base[1]],
        [base[0]],
        [base[0], (b"X-User-Id", b"")],
        [base[0], (b"X-User-Id", b"not-a-uuid")],
        [base[0], (b"X-User-Id", str(OWNER).encode() + b"," + str(OTHER_OWNER).encode())],
        [base[0], (b"X-User-Id", b" " + str(OWNER).encode())],
        [base[0], (b"X-User-Id", b"\xff")],
    ]


@pytest.mark.parametrize("raw_headers", malformed_headers())
@pytest.mark.parametrize("mode", ["legacy", "required"])
async def test_ambiguous_or_malformed_assertions_never_reach_database(admission, raw_headers, mode):
    admission.settings.gateway_owner_auth_mode = mode
    response = await admission.client.get(PATH, headers=raw_headers)
    assert response.status_code == 401
    assert admission.calls.db == 0


@pytest.mark.parametrize("token", [TOKEN, PREVIOUS, "invalid"])
async def test_every_configured_version_is_compared_without_early_exit(admission, monkeypatch, token):
    compare = secrets.compare_digest
    comparisons = []

    def counted(left, right):
        comparisons.append(right)
        return compare(left, right)

    monkeypatch.setattr("api.gateway_owner.secrets.compare_digest", counted)
    response = await admission.client.get(PATH, headers=headers(token))
    assert response.status_code == (401 if token == "invalid" else 418)
    assert comparisons == [TOKEN.encode(), PREVIOUS.encode()]


@pytest.mark.parametrize("method,path,payload", [
    ("GET", "/internal/v1/agents/count?user_id=" + str(OWNER), None),
    ("POST", "/internal/v1/agents/deactivate-all", {"user_id": str(OWNER)}),
    ("POST", "/internal/v1/agents/ownership-check", {"owner_id": str(OWNER), "agent_id": str(AGENT)}),
    ("POST", "/v1/chat/completions", {}),
])
async def test_gateway_credential_does_not_authorize_other_token_purposes(admission, monkeypatch, method, path, payload):
    monkeypatch.setattr("api.dependencies.get_settings", lambda: admission.settings)
    monkeypatch.setattr("api.v1.chat.get_settings", lambda: admission.settings)
    response = await admission.client.request(method, path, json=payload, headers={
        "X-Service-Token": TOKEN, "X-User-Id": str(OWNER),
    })
    assert response.status_code == 401
    assert admission.calls.db == 0


@pytest.mark.parametrize("token", [TOKEN, PREVIOUS])
@pytest.mark.parametrize("revoke", ["principal", "version"])
async def test_revoked_versions_do_not_fall_back_in_legacy_mode(admission, token, revoke, monkeypatch):
    data = principal()
    if revoke == "principal":
        data["revoked"] = True
    else:
        data["current" if token == TOKEN else "previous"]["revoked"] = True
    monkeypatch.setattr("api.gateway_owner.get_settings", lambda: configured(
        gateway_owner_auth_mode="legacy", gateway_ai_auth_principals=json.dumps([data]),
    ))
    response = await admission.client.get(PATH, headers=headers(token))
    assert response.status_code == 401
    assert admission.calls.db == 0


async def test_legacy_default_only_bypasses_absent_gateway_credential(admission, monkeypatch):
    settings = Settings(_env_file=None)
    assert settings.gateway_owner_auth_mode == "legacy"
    monkeypatch.setattr("api.gateway_owner.get_settings", lambda: settings)
    accepted = await admission.client.get(PATH, headers=headers(None))
    rejected = await admission.client.get(PATH, headers=headers("unexpected"))
    assert accepted.status_code == 418
    assert rejected.status_code == 401
    assert admission.calls.db == 1


@pytest.mark.parametrize("raw_headers", [
    [(b"X-User-Id", str(OWNER).encode()), (b"x-user-id", str(OWNER).encode())],
    [(b"X-User-Id", str(OWNER).encode()), (b"x-user-id", str(OTHER_OWNER).encode())],
    [(b"X-User-Id", str(OWNER).encode()), (b"x-user-role", b"admin"), (b"X-User-Role", b"member")],
])
async def test_legacy_absent_gateway_token_still_rejects_duplicate_identity(admission, raw_headers):
    admission.settings.gateway_owner_auth_mode = "legacy"
    response = await admission.client.get(PATH, headers=raw_headers)
    assert response.status_code == 401
    assert admission.calls.db == 0


async def test_public_and_separate_auth_contracts_are_not_owner_gated(admission):
    for path in ("/health", "/v1/models", "/v1/mcp-services", "/openapi.json", "/docs"):
        response = await admission.client.get(path)
        assert response.status_code == 200
    separate = set()
    for route in effective_api_routes():
        if (
            route.path.startswith("/internal/") or route.path == "/v1/chat/completions"
        ):
            assert all(dep.call is not require_owner_gateway for dep in route.dependant.dependencies)
            separate.update((method, route.path) for method in route.methods)
    assert separate == {
        ("GET", "/internal/v1/agents/count"),
        ("POST", "/internal/v1/agents/deactivate-all"),
        ("POST", "/internal/v1/agents/ownership-check"),
        ("POST", "/internal/v1/managed-turns/execute"),
        ("POST", "/internal/v1/managed-turns/evidence"),
        ("POST", "/v1/chat/completions"),
    }
    assert admission.calls.db == 0


def test_guarded_route_inventory_is_exact():
    guarded = {
        (method, route.path.replace("{agent_id}", str(AGENT))
         .replace("{service_name}", "bitrix"))
        for route in effective_api_routes()
        if any(dep.call is require_owner_gateway for dep in route.dependant.dependencies)
        for method in route.methods
    }
    assert guarded == set(OWNER_ROUTES)
    assert len(guarded) == 21


@pytest.mark.parametrize("other", [INTERNAL, CHAT, MANAGED, OWNERSHIP, BUSINESS])
@pytest.mark.parametrize("version", ["current", "previous"])
def test_configuration_rejects_cross_purpose_token_reuse(other, version):
    data = principal()
    data[version]["token"] = other
    data[version]["revoked"] = True
    with pytest.raises(RuntimeError, match="Invalid gateway owner authentication configuration"):
        configured(gateway_ai_auth_principals=json.dumps([data]))


@pytest.mark.parametrize("change", [
    {"caller": "knowledge-base-service"}, {"purpose": "agent-ownership-check"},
    {"audience": "control-plane-service"}, {"unknown": "field"},
    {"revoked": "false"}, {"current": {"version": "v2", "token": "short"}},
    {"previous": {"version": "v2", "token": PREVIOUS}},
    {"previous": {"version": "v1", "token": TOKEN}},
])
def test_configuration_rejects_malformed_or_wrong_purpose_registry(change):
    with pytest.raises(RuntimeError, match="Invalid gateway owner authentication configuration"):
        configured(gateway_ai_auth_principals=json.dumps([principal(**change)]))


@pytest.mark.parametrize("raw", ["", "[]", "{}", "null", "NaN", "not-json", '[{"principal_id":"one","principal_id":"two"}]'])
def test_required_configuration_fails_closed_without_active_registry(raw):
    with pytest.raises(RuntimeError, match="Invalid gateway owner authentication configuration"):
        configured(gateway_ai_auth_principals=raw)


def test_required_configuration_rejects_all_revoked_but_accepts_active_previous():
    data = principal(revoked=True)
    with pytest.raises(RuntimeError):
        configured(gateway_ai_auth_principals=json.dumps([data]))
    data = principal(current={"version": "v2", "token": TOKEN, "revoked": True})
    assert configured(gateway_ai_auth_principals=json.dumps([data])).gateway_principals
    data["previous"]["revoked"] = True
    with pytest.raises(RuntimeError):
        configured(gateway_ai_auth_principals=json.dumps([data]))


def test_duplicate_principal_or_cross_principal_tokens_fail_startup():
    for second in (principal(), principal(principal_id="other")):
        with pytest.raises(RuntimeError):
            configured(gateway_ai_auth_principals=json.dumps([principal(), second]))


@pytest.mark.parametrize("mode", ["off", "REQUIRED", "", "true"])
def test_invalid_mode_does_not_select_legacy(mode):
    with pytest.raises(RuntimeError):
        configured(gateway_owner_auth_mode=mode)


def test_registry_and_startup_errors_do_not_render_secrets():
    assert TOKEN not in repr(configured())
    assert PREVIOUS not in repr(configured())
    raw = json.dumps([principal(current={"version": "v2", "token": "invalid," + TOKEN})])
    with pytest.raises(RuntimeError) as caught:
        configured(gateway_ai_auth_principals=raw)
    rendered = "".join(traceback.format_exception(caught.value))
    assert TOKEN not in rendered
    assert raw not in rendered
    assert "input_value" not in rendered


@pytest.mark.parametrize("key", ["GATEWAY_AI_AUTH_PRINCIPALS", "Gateway_AI_Auth_Principals",
                                 "GATEWAY_AI_OWNER_TOKEN", "OWNER_AI_TOKEN", "OWNER_CP_TOKEN"])
@pytest.mark.parametrize("mapping", ["inherit", "env_mapping", "optional_env_mapping"])
def test_gateway_secrets_cannot_enter_any_mcp_subprocess(monkeypatch, key, mapping):
    manager = MCPManager("bitrix", {"webhook_url": "synthetic-tool-credential"})
    monkeypatch.setenv(key, TOKEN)
    if mapping != "inherit":
        config = {"script_path": "none", "env_mapping": {}}
        config[mapping] = {"webhook_url": key}
        monkeypatch.setattr(manager.settings, "mcp_services", {"bitrix": config})
        with pytest.raises(RuntimeError, match="MCP environment configuration is invalid"):
            manager._build_server_params()
    else:
        env = manager._build_server_params().env
        assert key not in env
        assert all(name.lower() not in MANAGED_SECRET_ENV_KEYS for name in env)
        assert env["B24_WEBHOOK_URL"] == "synthetic-tool-credential"


@pytest.mark.integration
async def test_authenticated_owner_crud_keeps_tenant_and_admin_rules(api_client, monkeypatch):
    monkeypatch.setattr("api.gateway_owner.get_settings", lambda: configured())
    create = await api_client.post("/v1/agents/", headers=headers(), json={
        "name": "Synthetic owner agent", "system_prompt": "test", "is_active": False,
        "model_name": "google/gemini-2.5-flash",
    })
    assert create.status_code == 201
    path = "/v1/agents/" + create.json()["id"]
    own = await api_client.get(path, headers=headers())
    foreign = await api_client.get(path, headers=headers(owner=OTHER_OWNER))
    update = await api_client.put(path, headers=headers(owner=OTHER_OWNER), json={"name": "foreign"})
    delete = await api_client.delete(path, headers=headers(owner=OTHER_OWNER, role="custom-role"))
    spoof = await api_client.delete(path, headers=headers(None, owner=OTHER_OWNER, role="admin"))
    admin = await api_client.delete(path, headers=headers(owner=OTHER_OWNER, role="admin"))
    assert own.status_code == 200
    assert foreign.status_code == update.status_code == 404
    assert delete.status_code == 403
    assert spoof.status_code == 401
    assert admin.status_code == 204
