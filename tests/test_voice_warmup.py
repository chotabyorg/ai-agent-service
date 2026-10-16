"""Native PostgreSQL warmup isolation, authority boundary and resource lifetime."""
import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from api.internal import managed_turns as routes
from core.config import get_settings
from models.session import ConversationSession
from services.business_runtime import BusinessRuntime
from services.llm_service import LLMService
from services.managed_runtime import ManagedRuntime, join_owner
from services.prompt_composer import PromptComposer
from services.voice_warmup import WarmupLimiter, warm_owned
from tests.fakes import build_llm_response, build_tool_call
from tests.test_managed_turns import TOKEN, execute, model, setup
from tests.test_voice_latency import profile

PATH = "/internal/v1/voice/warmup"


@pytest.mark.asyncio
async def test_real_business_schema_prefix_and_no_effects(api_client, db_session, monkeypatch, model):
    from uuid import UUID
    from models.agent import AgentTemplate
    from models.business import BusinessPlan, BusinessOperation
    from tests.test_chotacrm_managed_proposal_errors import install_managed_crm
    from tests.test_managed_turns import configure
    value, _, _, calls, _, _ = await install_managed_crm(api_client, db_session, monkeypatch, 200, {})
    import services.llm_service as llm_module
    monkeypatch.setattr(llm_module, "AsyncOpenAI", lambda **kwargs: model)
    value["channel"] = "VOICE"
    async with db_session() as db:
        agent = await db.get(AgentTemplate, UUID(value["agent_id"]))
        configure(monkeypatch, value, agent.user_id)
        selected = profile(value, agent.model_name, True).model_copy(update={"prompt_warmup": True})
    monkeypatch.setattr(get_settings(), "managed_voice_latency_profiles", [selected])
    monkeypatch.setattr(routes, "warmup_limiter", WarmupLimiter())
    body = {key: value[key] for key in ("owner_id", "integration_id", "agent_id", "channel")}
    body["call_id"] = str(uuid4())
    model.chat.completions.queue(build_llm_response(tool_calls=[build_tool_call(
        "discard", "booking_create", {})]), build_llm_response(content="Real answer"))
    assert (await warm(api_client, body)).json()["status"] == "WARMED"
    assert calls["lookup"] == calls["facts"] == calls["execute"] == 0
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0
    assert (await execute(api_client, value)).json()["status"] == "DRAFT_READY"
    before, actual = model.chat.completions.calls
    assert before["tools"] and before["tools"] == actual["tools"]
    assert before["parallel_tool_calls"] is actual["parallel_tool_calls"] is False


async def configured(api_client, db_session, monkeypatch):
    agent, value = await setup(api_client, db_session, monkeypatch, channel="VOICE")
    selected = profile(value, agent["model_name"], True).model_copy(update={"prompt_warmup": True})
    monkeypatch.setattr(get_settings(), "managed_voice_latency_profiles", [selected])
    monkeypatch.setattr(routes, "warmup_limiter", WarmupLimiter())
    body = {key: value[key] for key in ("owner_id", "integration_id", "agent_id", "channel")}
    body["call_id"] = str(uuid4())
    return body, value


async def warm(client, body, **kwargs):
    return await client.post(PATH, json=body, headers={"X-Service-Token": TOKEN}, **kwargs)


@pytest.mark.asyncio
async def test_discarded_output_no_business_history_or_rag(api_client, db_session, monkeypatch, model, fake_redis):
    body, value = await configured(api_client, db_session, monkeypatch)
    async def forbidden(*args, **kwargs):
        raise AssertionError("warmup invoked execution")
    monkeypatch.setattr(BusinessRuntime, "open", forbidden)
    monkeypatch.setattr(LLMService, "_build_knowledge_base_message", forbidden)
    monkeypatch.setattr(LLMService, "_execute_core", forbidden)
    keys = await fake_redis.keys("*")
    model.chat.completions.queue(build_llm_response(tool_calls=[build_tool_call(
        "ignored", "booking_create", {"private": "must never execute"})]))
    response = await warm(api_client, body)
    assert response.status_code == 200 and response.json()["status"] == "WARMED"
    assert response.headers["cache-control"] == "no-store"
    assert "private" not in response.text and "draft" not in response.text
    assert len(model.chat.completions.calls) == 1 and model.closed
    call = model.chat.completions.calls[0]
    assert call["max_tokens"] == 1 and call["model"] == "synthetic/voice-model"
    assert call["extra_body"]["provider"]["only"] == ["synthetic/bf16"]
    assert call["messages"][-1] == {"role": "user", "content": "."}
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(ConversationSession)) == 0
    assert await fake_redis.keys("*") == keys
    assert (await warm(api_client, body)).json()["status"] == "SKIPPED"
    assert len(model.chat.completions.calls) == 1


@pytest.mark.asyncio
async def test_warmup_matches_real_voice_prefix_but_not_user_text(api_client, db_session, monkeypatch, model):
    body, value = await configured(api_client, db_session, monkeypatch)
    monkeypatch.setattr(PromptComposer, "_channel_and_time", lambda *args: "fixed voice time")
    model.chat.completions.queue(build_llm_response(content="discard"), build_llm_response(content="Real answer"))
    assert (await warm(api_client, body)).json()["status"] == "WARMED"
    result = await execute(api_client, value)
    assert result.json()["status"] == "DRAFT_READY"
    before, actual = model.chat.completions.calls
    assert before["messages"][0] == actual["messages"][0]
    assert before["messages"][-1] != actual["messages"][-1]
    assert (before.get("tools") or []) == (actual.get("tools") or [])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["disabled", "model", "inactive", "owner", "integration", "agent", "channel", "extra"])
async def test_fail_closed_before_provider(api_client, db_session, monkeypatch, model, change):
    body, value = await configured(api_client, db_session, monkeypatch)
    if change in {"disabled", "model"}:
        current = get_settings().managed_voice_latency_profiles[0]
        update = {"prompt_warmup": False} if change == "disabled" else {"model": "other/model"}
        monkeypatch.setattr(get_settings(), "managed_voice_latency_profiles", [current.model_copy(update=update)])
    elif change == "inactive":
        from models.agent import AgentTemplate
        from uuid import UUID
        async with db_session() as db:
            agent = await db.get(AgentTemplate, UUID(body["agent_id"]))
            agent.is_active = False
            await db.commit()
    elif change == "channel":
        body["channel"] = "TELEGRAM"
    elif change == "extra":
        body["messages"] = [{"role": "user", "content": "never accepted"}]
    else:
        body[{"owner": "owner_id", "integration": "integration_id", "agent": "agent_id"}[change]] = str(uuid4())
    response = await warm(api_client, body)
    assert response.status_code in {200, 404, 422}
    if response.status_code == 200:
        assert response.json()["status"] == "SKIPPED"
    assert model.chat.completions.calls == []


@pytest.mark.asyncio
async def test_auth_body_and_route_bounds(api_client, db_session, monkeypatch, model):
    body, _ = await configured(api_client, db_session, monkeypatch)
    assert (await api_client.post(PATH, content=b"bad")).status_code == 401
    headers = {"X-Service-Token": TOKEN, "Content-Type": "application/json"}
    assert (await api_client.post(PATH, content=b"x"*4097, headers=headers)).status_code == 413
    assert (await api_client.post(PATH+"/", json=body, headers=headers)).status_code == 404
    assert (await api_client.get(PATH, headers=headers)).status_code == 405
    assert model.chat.completions.calls == []


@pytest.mark.asyncio
async def test_provider_failure_is_disposable_and_never_retried(api_client, db_session, monkeypatch, model):
    body, _ = await configured(api_client, db_session, monkeypatch)
    async def failed(**kwargs):
        raise RuntimeError("provider-secret")
    model.chat.completions.create = failed
    response = await warm(api_client, body)
    assert response.status_code == 200 and response.json()["status"] == "FAILED"
    assert "provider-secret" not in response.text and model.closed
    assert not routes.warmup_limiter.busy


def test_limiter_is_bounded_and_nonwaiting(monkeypatch):
    import services.voice_warmup as module
    clock = [0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    limiter = WarmupLimiter()
    assert limiter.acquire("one")
    assert not limiter.acquire("two")
    limiter.release()
    assert not limiter.acquire("one")
    clock[0] = 31
    assert limiter.acquire("one")
    limiter.release()
    assert limiter.acquire("two")


def test_routing_affinity_is_opted_in_private_and_scoped():
    from tests.test_managed_turns import payload
    selected = profile(payload(channel="VOICE"), "original/model", True)
    assert "session_id" not in selected.extra_body()
    selected = selected.model_copy(update={"prompt_warmup": True})
    session = selected.extra_body()["session_id"]
    assert session == selected.extra_body()["session_id"]
    assert selected.owner_id not in session and str(selected.agent_id) not in session
    for field in ("owner_id", "integration_id", "agent_id", "completion_model"):
        assert selected.model_copy(update={field: str(uuid4())}).extra_body()["session_id"] != session


@pytest.mark.asyncio
async def test_cancel_joins_provider_and_releases_limiter(api_client, db_session, monkeypatch, model):
    body, _ = await configured(api_client, db_session, monkeypatch)
    from schemas.voice_warmup import VoiceWarmupRequest
    from services.business_registry import EMPTY_BUSINESS_REGISTRY
    entered = asyncio.Event()
    async def blocked(**kwargs):
        entered.set()
        await asyncio.Event().wait()
    model.chat.completions.create = blocked
    async def provider():
        async with db_session() as db:
            yield db
    runtime = ManagedRuntime(2.5)
    limiter = WarmupLimiter()
    task = asyncio.create_task(warm_owned(VoiceWarmupRequest.model_validate(body),
        get_settings().managed_principals[0].grants[0], runtime, provider,
        EMPTY_BUSINESS_REGISTRY, limiter))
    await asyncio.wait_for(entered.wait(), 1)
    runtime.signal_cancel()
    with pytest.raises(asyncio.CancelledError):
        await join_owner(task, runtime)
    assert task.done() and model.closed and not limiter.busy
