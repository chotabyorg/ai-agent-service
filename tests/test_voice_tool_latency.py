"""Voice-only signing reuse and bounded phase timings; native test database."""
import asyncio
import base64
import json
import logging
from uuid import uuid4

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from pydantic import SecretStr

from business_capabilities.chotacrm_client import ChotaCRMClient
from business_capabilities.context import BusinessRuntimeError
from core.config import Settings, get_settings
from core.managed_security import managed_logging, install_managed_log_boundary
from core import voice_latency_diagnostics as diagnostic
from services.voice_signing import voice_signing_scope, _current
from tests.fakes import build_llm_response, build_tool_call
from tests.test_managed_business_fakes import install_business, plan_arguments
from tests.test_managed_turns import setup, execute, model  # noqa: F401
from tests.test_voice_latency import profile


@pytest.fixture(autouse=True)
def capture_voice_logger(monkeypatch, request):
    if request.node.get_closest_marker("integration"):
        request.getfixturevalue("migrated_postgres")
    install_managed_log_boundary()
    logger = logging.getLogger("uvicorn.error")
    monkeypatch.setattr(logger, "disabled", False)
    monkeypatch.setattr(logger, "propagate", True)
    monkeypatch.setattr(logger, "level", logging.INFO)


@pytest.fixture(scope="module")
def keys():
    return [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(2)]


def pem(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def settings(key):
    return Settings(_env_file=None, business_capabilities_enabled=True,
        business_crm_key_id="synthetic", business_crm_private_key=pem(key),
        business_crm_url="http://synthetic.invalid")


def unpack(token, key):
    header, body, signature = token.split(".")
    key.public_key().verify(base64.urlsafe_b64decode(signature + "=="),
        (header + "." + body).encode(), padding.PKCS1v15(), hashes.SHA256())
    return json.loads(base64.urlsafe_b64decode(body + "=="))


def count_loads(monkeypatch):
    original = ChotaCRMClient._validated_key
    counts = []
    def load(value):
        counts.append(1)
        return original(value)
    monkeypatch.setattr(ChotaCRMClient, "_validated_key", staticmethod(load))
    return counts


@pytest.mark.asyncio
async def test_reuse_key_but_send_fresh_authorization_for_every_request(keys, monkeypatch):
    counts = count_loads(monkeypatch)
    authorizations = []
    def handle(request):
        authorizations.append(request.headers["Authorization"][7:])
        return httpx.Response(200, json={"status": "OK"})
    client = ChotaCRMClient(settings(keys[0]), transport=httpx.MockTransport(handle))
    owner = object()
    with voice_signing_scope(owner):
        for scope in ("context", "catalog.search", "context", "resource.search"):
            await client.request("GET", scope, scope=scope, claims={"owner_id": "synthetic"}, execution=owner)
    claims = [unpack(token, keys[0]) for token in authorizations]
    assert len(counts) == 1 and len(authorizations) == 4
    assert len({value["jti"] for value in claims}) == 4
    assert [value["scope"] for value in claims] == ["context", "catalog.search", "context", "resource.search"]
    assert all(value["exp"] - value["iat"] == 45 for value in claims)
    assert _current.get() is None


def test_other_runtime_and_no_owner_cannot_reuse_and_scope_clears(keys, monkeypatch):
    counts = count_loads(monkeypatch)
    client = ChotaCRMClient(settings(keys[0]))
    owner, other = object(), object()
    with voice_signing_scope(owner):
        entry = _current.get()
        client.assertion("context", {}, execution=owner)
        client.assertion("context", {}, execution=owner)
        client.assertion("context", {}, execution=other)
        client.assertion("context", {})
    assert entry == {} and _current.get() is None and len(counts) == 3
    client.assertion("context", {}, execution=owner)
    assert len(counts) == 4


def test_rotation_revalidates_and_invalid_key_never_reuses_old_material(keys, monkeypatch):
    counts = count_loads(monkeypatch)
    config = settings(keys[0]); client = ChotaCRMClient(config); owner = object()
    with voice_signing_scope(owner):
        unpack(client.assertion("context", {}, execution=owner), keys[0])
        config.business_crm_private_key = SecretStr(pem(keys[1]))
        unpack(client.assertion("context", {}, execution=owner), keys[1])
        config.business_crm_private_key = SecretStr("invalid-secret-material")
        for _ in range(2):
            with pytest.raises(BusinessRuntimeError):
                client.assertion("context", {}, execution=owner)
        assert _current.get()["key"] is None and _current.get()["pem"] is None
    assert len(counts) == 4


def test_nested_and_exceptional_scope_restores_outer_without_retaining_inner(keys):
    client = ChotaCRMClient(settings(keys[0])); outer, inner = object(), object()
    with voice_signing_scope(outer):
        original = _current.get()
        with pytest.raises(RuntimeError):
            with voice_signing_scope(inner):
                inner_entry = _current.get()
                client.assertion("context", {}, execution=inner)
                raise RuntimeError("synthetic")
        assert inner_entry == {} and _current.get() is original
    assert original == {} and _current.get() is None


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("channel,enabled,expected", [("VOICE", True, 1), ("VOICE", False, 2), ("TELEGRAM", True, 2)])
async def test_managed_activation_is_exact_voice_only_and_cleanup_owned(
        api_client, db_session, monkeypatch, model, keys, channel, enabled, expected, caplog):
    agent, value = await setup(api_client, db_session, monkeypatch, channel=channel)
    selected = profile(value, agent["model_name"], True).model_copy(update={"reuse_signing_key": enabled})
    monkeypatch.setattr(get_settings(), "managed_voice_latency_profiles", [selected])
    await install_business(monkeypatch, value, db_session, confirmation=False)
    from services.business_runtime import BusinessRuntime
    original = BusinessRuntime.call_tool
    counts = count_loads(monkeypatch); client = ChotaCRMClient(settings(keys[0])); entries = []
    async def call_tool(self, name, arguments):
        entries.append(_current.get())
        client.assertion("context", {}, execution=self.execution)
        client.assertion("catalog.search", {}, execution=self.execution)
        return await original(self, name, arguments)
    monkeypatch.setattr(BusinessRuntime, "call_tool", call_tool)
    model.chat.completions.queue(
        build_llm_response(tool_calls=[build_tool_call("tool", "business_execute_plan", plan_arguments())]),
        build_llm_response(content="Synthetic answer"))
    with caplog.at_level(logging.INFO):
        result = (await execute(api_client, value)).json()
    assert result["status"] == "DRAFT_READY" and model.closed
    assert len(counts) == expected and _current.get() is None
    assert all(entry is None or entry == {} for entry in entries)
    timings = [r for r in caplog.records if getattr(r, "event", None) == "voice_phase_timing"]
    assert [r.phase for r in timings] == (
        ["SETUP", "BUSINESS", "CONTEXT", "KNOWLEDGE", "PREPARE", "MODEL", "TOOL", "MODEL"]
        if channel == "VOICE" else [])
    elapsed = [r.elapsed_ms for r in timings[:5]]
    assert elapsed == sorted(elapsed)
    text_timings = [r for r in caplog.records if getattr(r, 'event', None) == 'managed_text_phase_timing']
    assert [r.phase for r in text_timings] == (
        ['SETUP', 'BUSINESS', 'CONTEXT', 'KNOWLEDGE', 'PREPARE', 'TOOL']
        if channel != 'VOICE' else [])
    assert [r.elapsed_ms for r in text_timings[:5]] == sorted(r.elapsed_ms for r in text_timings[:5])


@pytest.mark.asyncio
async def test_model_connection_warmup_reuses_one_owned_pool_connection():
    from openai import AsyncOpenAI
    from services.managed_turn_service import _join_warmup, _warm_model_connection
    seen = []
    def handle(request):
        seen.append((request.method, request.url.path, request.headers["Authorization"]))
        return httpx.Response(200, json={"data": {}})
    client = AsyncOpenAI(base_url="https://synthetic.invalid/api/v1", api_key="synthetic-key",
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    task = asyncio.create_task(_warm_model_connection(client))
    await asyncio.wait({task}, timeout=1)
    await _join_warmup(task)
    assert seen == [("GET", "/api/v1/key", "Bearer synthetic-key")]

    def fail(request):
        raise httpx.ConnectError("synthetic")
    broken = AsyncOpenAI(base_url="https://synthetic.invalid/api/v1", api_key="synthetic-key",
                         http_client=httpx.AsyncClient(transport=httpx.MockTransport(fail)))
    await _warm_model_connection(broken)

    async def hang(request):
        await asyncio.sleep(30)
    stuck = AsyncOpenAI(base_url="https://synthetic.invalid/api/v1", api_key="synthetic-key",
                        http_client=httpx.AsyncClient(transport=httpx.MockTransport(hang)))
    pending = asyncio.create_task(_warm_model_connection(stuck))
    await asyncio.sleep(0)
    await asyncio.wait_for(_join_warmup(pending), 1)
    assert pending.done()


@pytest.mark.parametrize("changes", [
    {"phase": "secret"}, {"turn_id": "secret"}, {"step": True}, {"step": 21},
    {"elapsed_ms": -1}, {"elapsed_ms": True}, {"tool": "secret"}, {"phase": "TOOL"},
])
@pytest.mark.parametrize('emitter', ['emit_voice_timing', 'emit_text_timing'])
def test_invalid_timing_cannot_escape_logging_boundary(caplog, changes, emitter):
    value = diagnostic.VoiceTiming(**{"turn_id": uuid4(), "step": 1, "phase": "MODEL", "elapsed_ms": 10, **changes})
    mark = managed_logging.set(True)
    try:
        with caplog.at_level(logging.INFO):
            getattr(diagnostic, emitter)(value)
    finally:
        managed_logging.reset(mark)
    assert caplog.records[-1].getMessage() == "Managed execution diagnostic"
    assert "secret" not in caplog.text
