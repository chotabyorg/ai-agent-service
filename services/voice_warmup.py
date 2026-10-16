"""Bounded, disposable prefill. No BusinessRuntime.open, history, RAG or tool dispatch."""
import asyncio
import json
import time

import anyio
from openai import AsyncOpenAI
from sqlalchemy import select, text

from models.business_connection import BusinessConnection
from schemas.voice_warmup import VoiceWarmupResult
from services.business_runtime import BusinessRuntime
from services.llm_service import LLMService
from services.managed_http_cleanup import own_managed_http
from services.managed_turn_service import _OwnedDBContext, _close_client
from services.prompt_builder import ChatContext


class WarmupLimiter:
    """Per-process bounded best-effort admission; never delays a real completion."""
    def __init__(self):
        self.busy = False
        self.attempts = {}

    def acquire(self, scope):
        now = time.monotonic()
        self.attempts = {key: at for key, at in self.attempts.items() if now - at < 30}
        if self.busy or scope in self.attempts or len(self.attempts) >= 64:
            return False
        self.attempts[scope] = now
        self.busy = True
        return True

    def release(self):
        self.busy = False


def preview_tools(connection, registry):
    """Render code-owned schemas only; this view has no repository, request or effects port.

    A real turn rebuilds all permissions and schemas independently. Even a stale
    preview can only miss the provider cache, never enable an operation.
    """
    registration = registry.get("chotacrm")
    if (connection is None or not connection.active or connection.pending_action
            or registration is None):
        return []
    view = BusinessRuntime()
    view.registration, view.binding = registration, connection
    view.recovery_mode = False
    view.effect_eligible = True  # Schema preview only; no execution methods are invoked.
    view._stale_split_plan_id = None
    return view.openai_tools()


async def warm_owned(value, grant, runtime, db_provider, registry, limiter):
    started = time.monotonic()
    acquired = False
    result = VoiceWarmupResult(status="FAILED")
    with anyio.CancelScope(shield=True) as lifecycle:
        try:
            service = LLMService()
            # The normal ManagedTurnRequest matcher deliberately cannot match this DTO.
            profile = next((p for p in service.settings.managed_voice_latency_profiles
                if p.prompt_warmup and p.completion_model and
                (p.owner_id, p.integration_id, p.agent_id) ==
                (value.owner_id, value.integration_id, value.agent_id)), None)
            if profile is None:
                return VoiceWarmupResult(status="SKIPPED")
            acquired = limiter.acquire((value.owner_id, value.integration_id, value.agent_id))
            if not acquired:
                return VoiceWarmupResult(status="SKIPPED")
            db = await runtime.stack.enter_async_context(_OwnedDBContext(db_provider))
            await runtime.operation(db.execute(text("SET TRANSACTION READ ONLY")))
            agent = await runtime.operation(service.agent_service.get_agent_by_id(
                db, value.agent_id, user_id=grant.agent_owner_id))
            if (agent is None or not agent.is_active
                    or (agent.model_name or service.settings.llm_default_model) != profile.model):
                return VoiceWarmupResult(status="SKIPPED")
            key = service._get_agent_api_key(agent)
            if not key:
                return VoiceWarmupResult(status="SKIPPED")
            connection = await runtime.operation(db.scalar(select(BusinessConnection).where(
                BusinessConnection.agent_id == value.agent_id,
                BusinessConnection.owner_id == grant.agent_owner_id,
                BusinessConnection.active.is_(True))))
            tools = preview_tools(connection, registry)
            prompt = service.prompt_composer.compose(agent, ChatContext("voice", ""))
            messages = [{"role": "system", "content": prompt}, {"role": "user", "content": "."}]
            envelope = dict(model=profile.completion_model, temperature=agent.temperature,
                messages=messages, max_tokens=1, extra_body=profile.extra_body())
            if tools:
                envelope.update(tools=tools, parallel_tool_calls=False)
            if len(json.dumps(envelope).encode("utf-8")) > service.settings.managed_ai_context_bytes:
                return VoiceWarmupResult(status="SKIPPED")
            await runtime.operation(db.rollback())
            client = service._create_llm_client(key, value.call_id, managed=True)
            runtime.stack.push_async_callback(_close_client, client)
            if isinstance(client, AsyncOpenAI):
                own_managed_http(client._client)
            # Identical tool schemas are necessary for the provider prefix cache.
            # No output/tool-call processing exists here, even if the model emits one.
            response = await runtime.operation(client.chat.completions.create(
                **envelope, extra_headers=service._build_extra_headers()))
            details = getattr(getattr(response, "usage", None), "prompt_tokens_details", None)
            cached = getattr(details, "cached_tokens", 0)
            result = VoiceWarmupResult(status="WARMED", elapsed_ms=min(30000,
                round((time.monotonic() - started) * 1000)),
                cached_tokens=cached if type(cached) is int and 0 <= cached <= 1048576 else 0)
        except asyncio.CancelledError:
            raise
        except Exception:
            result = VoiceWarmupResult(status="FAILED")
        finally:
            try:
                await runtime.cleanup(lifecycle)
            finally:
                if acquired:
                    limiter.release()
    return result
