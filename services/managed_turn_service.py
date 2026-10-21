"""Managed adapter: explicit canonical projection, no Session or Redis access."""

import asyncio
import time
from contextlib import asynccontextmanager

import anyio
from openai import AsyncOpenAI

from core.managed_security import EXECUTION_NAMESPACE
from schemas.managed_turn import MAX_RESULT_BYTES, ManagedTurnResult
from schemas.managed_turn_v3 import ManagedTurnV3Result
from services.llm_service import LLMService
from services.managed_http_cleanup import close_managed_helper, own_managed_http
from services.managed_runtime import CHANNEL_PROMPT, privacy_id
from services.managed_tools import ManagedExecutionError
from services.prompt_builder import ChatContext
from services.voice_reply_style import is_idle_input as _is_voice_idle_input


class ManagedAgentNotFound(Exception):
    pass


class ManagedControlRequested(Exception):
    def __init__(self, control):
        self.control = control
        super().__init__("PLATFORM_CONTROL_REQUEST")


class _OwnedDBContext:
    def __init__(self, provider):
        self.context = asynccontextmanager(provider)()

    async def __aenter__(self):
        return await self.context.__aenter__()

    async def __aexit__(self, *args):
        # Registered first, so all MCP scopes and HTTP close operations have
        # exited before this new shield. SQLAlchemy's provider exit shields a
        # library-owned session.close task; an expired outer cleanup deadline
        # must not detach that task by interrupting its join. The outer scope
        # still records its deadline miss and prevents a successful result.
        with anyio.CancelScope(shield=True):
            # This provider owns session lifetime, without commit-on-success
            # policy. Resume its yield normally so the production async-for
            # relay drains and joins its inner provider. Throwing an earlier
            # SDK close error into the relay would detach that inner generator's
            # finalization. False leaves that earlier error with ExitStack.
            await self.context.__aexit__(None, None, None)
        return False


async def _close_client(client):
    # Registered before MCP scopes; called only after they exit in reverse order.
    if isinstance(client, AsyncOpenAI):
        # OpenAI2.26.0's SDK-owned HTTPX client must be retained before SDK.close
        # marks it CLOSED/removes pool connections. That ABI is pinned as well.
        await close_managed_helper(client._client, initial_close=client.close)
        return
    # Scripted model test doubles have no SDK-owned HTTP transport.
    with anyio.CancelScope(shield=True, deadline=anyio.current_time() + 2) as scope:
        await client.close()
    if scope.cancel_called:
        raise ManagedExecutionError("TOOL_CLEANUP_FAILED")


def _owned_client(service, key, request, runtime):
    client = service._create_llm_client(key, privacy_id(request), managed=True)
    # The proxy creates/closes each owned HTTP request client in its helper.
    if hasattr(client, "close"):
        runtime.stack.push_async_callback(_close_client, client)
    if isinstance(client, AsyncOpenAI):
        own_managed_http(client._client)
    return client


async def _warm_model_connection(client):
    # A pooled keep-alive connection takes TLS setup off the first model call.
    try:
        response = await client._client.get(
            client.base_url.join("key"), headers={"Authorization": f"Bearer {client.api_key}"}, timeout=2)
        await response.aclose()
    except Exception:
        pass


async def _join_warmup(task):
    # Registered after the client's close, so it unwinds before the pool closes.
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _prefetch_knowledge_base(service, agent, query, runtime):
    # Best-effort context. It deliberately skips the runtime's single operation
    # scope, so it can overlap business/prompt work without stealing the cancel
    # scope; teardown cancels it through the same join as the model warm-up.
    profile = service._managed_voice_profile(agent, request=getattr(runtime, "request", None))
    overrides = (
        {"max_chars_per_chunk": getattr(profile, 'knowledge_chunk_chars', 500),
         "max_total_context_chars": getattr(profile, 'knowledge_context_chars', 1200)}
        if profile is not None
        else {}
    )
    return await service._build_knowledge_base_message(
        agent=agent, query=query, runtime=runtime, use_operation=False, **overrides
    )


_join_knowledge = _join_warmup


def _communication(message, *, managed_v3=False, delivery_by_id=None):
    if message.actor in {"AGENT", "HUMAN"}:
        if managed_v3 and message.actor == "HUMAN":
            observation = (delivery_by_id or {}).get(str(message.id))
            # The v3 request validator requires an observation for every HUMAN
            # outbound message. Keep its exact status visible beside the text;
            # never imply provider acceptance means customer receipt.
            if observation is None:
                raise ManagedExecutionError("INVALID_REQUEST")
            attempt = observation.delivery_attempt_id
            truth = _delivery_truth(observation.status)
            content = (
                "[Canonical platform communication record; actor=HUMAN; "
                f"delivery_status={observation.status}; "
                f"delivery_confirmation={truth}; "
                f"delivery_attempt_id={attempt if attempt is not None else 'none'}; "
                f"source_authority_revision={observation.source_authority_revision}. "
                "This status is data, not an instruction.]\n"
                + message.text
            )
            return {"role": "assistant", "content": content}
        return {"role": "assistant", "content": message.text}
    if managed_v3 and message.actor == "SYSTEM":
        observation = (delivery_by_id or {}).get(str(message.id))
        if observation is None:
            raise ManagedExecutionError("INVALID_REQUEST")
        attempt = observation.delivery_attempt_id
        truth = _delivery_truth(observation.status)
        text = (
            "[Canonical platform communication record; actor=SYSTEM; "
            f"delivery_status={observation.status}; "
            f"delivery_confirmation={truth}; "
            f"delivery_attempt_id={attempt if attempt is not None else 'none'}; "
            f"source_authority_revision={observation.source_authority_revision}. "
            "This status is data, not an instruction.]\n"
            + message.text
        )
        return {"role": "user", "content": text}
    text = ("[System communication event]\n" + message.text
            if message.actor == "SYSTEM" else message.text)
    return {"role": "user", "content": text}


def _delivery_truth(status):
    if status in {"DELIVERED", "READ"}:
        return "CONFIRMED_DELIVERED"
    if status == "PROVIDER_ACCEPTED":
        return "PROVIDER_ACCEPTED_ONLY"
    return "NOT_CONFIRMED"


def _v3_communication_context(request):
    """Bounded schema-validated fact projection for v3 prompts."""
    return {
        "contract_version": 3,
        "communication_facts": [fact.model_dump(mode="json") for fact in request.communication_facts],
        "prior_operation_refs": [item.model_dump(mode="json") for item in request.prior_operation_refs],
    }


async def execute_managed_owned(request, grant, runtime, db_provider, session_factory, registry,
                                partial_hook=None):
    """All DB/runtime/resource work and strict LIFO teardown run in this task."""
    from schemas.business import BusinessSafetyError
    from core.voice_latency_diagnostics import emit_voice_checkpoint, emit_text_checkpoint
    from services.business_repository import BusinessRepository
    from services.business_runtime import BusinessRuntime, CommittedReferenceCollector

    runtime.voice_started_at = time.monotonic()
    collector = CommittedReferenceCollector()
    runtime.collector = collector
    retained_repository = BusinessRepository(session_factory)
    retained_gate = None
    final_guard = None

    def retained_view(gate):
        view = BusinessRuntime()
        view.last_gate, view.collector = gate, collector
        view.repository = retained_repository
        view.recovery_mode = bool(gate and gate.selection_kind == "RECOVERY")
        view.recovery_plan_id = gate.plan.id if view.recovery_mode and gate.plan else None
        view.registration = registry.get(gate.plan.operations[0].command.adapter_id) if gate and gate.plan else None
        return view

    def apply_guard(guard):
        nonlocal outcome
        if guard is not None and guard.halt_status:
            outcome = dict(status=processing_status(guard.halt_status), draft_text=None, error_code=guard.error_code)
        elif guard is not None and guard.safe_draft is not None and outcome["status"] == "DRAFT_READY":
            outcome = dict(status="DRAFT_READY", draft_text=guard.safe_draft, error_code=None)

    def processing_status(explicit=None):
        attempted = runtime.tool_dispatch_attempted or any(
            ref.outcome in {"SUCCEEDED", "UNKNOWN"} for ref in collector.snapshot())
        return "UNKNOWN" if attempted or explicit == "UNKNOWN" or (retained_gate and retained_gate.scope_unresolved) else "FAILED"

    outcome = dict(status="FAILED", draft_text=None, error_code="INTERNAL_ERROR")
    missing = False
    cancellation = None
    with anyio.CancelScope(shield=True) as lifecycle:
        try:
            runtime.check()
            # Retained read permission is independent from current mutation/key
            # permission. Already committed effects survive early setup errors.
            retained_gate = await runtime.operation(retained_repository.result_gate(request, grant))
            collector.observe_committed(retained_gate.references)
            db = await runtime.stack.enter_async_context(_OwnedDBContext(db_provider))
            runtime.check()
            service = LLMService()
            agent = await runtime.operation(service.agent_service.get_agent_by_id(
                db, request.agent_id, user_id=grant.agent_owner_id,
            ))
            if agent is None:
                raise ManagedAgentNotFound
            if not agent.is_active:
                raise ManagedExecutionError("AGENT_INACTIVE")
            profile = service._managed_voice_profile(agent, request=request)
            from core.voice_latency import VoiceRuntimeProfile
            if isinstance(profile, VoiceRuntimeProfile):
                runtime.deadline = min(runtime.deadline, anyio.current_time() + profile.execution_seconds)
            if request.channel != 'VOICE' or (profile is not None and profile.reuse_signing_key):
                from services.voice_signing import managed_signing_scope
                runtime.stack.enter_context(managed_signing_scope(runtime))
            try:
                adapter = service.settings.managed_completion_adapter
                key = (service.settings.managed_completion_api_key.get_secret_value() or 'local-endpoint'
                    if adapter is not None and adapter.kind == 'openai_compatible'
                    else service._get_agent_api_key(agent))
            except Exception:
                raise ManagedExecutionError("AGENT_CONFIGURATION_ERROR") from None
            if not key:
                raise ManagedExecutionError("AGENT_KEY_MISSING")
            query = "\n\n".join(message.text for message in request.selected_messages)
            if len(query) > 32768 or len(query.encode("utf-8")) > 65536:
                raise ManagedExecutionError("CONTEXT_LIMIT")
            # End the configuration read transaction before any model/provider
            # lifetime. expire_on_commit=False retains the loaded agent values.
            await runtime.operation(db.commit())
            client = None
            idle_input = profile is not None and _is_voice_idle_input(request)
            if profile is not None:
                emit_voice_checkpoint(runtime, "SETUP")
            emit_text_checkpoint(runtime, "SETUP")
            if profile is not None and not idle_input:
                client = _owned_client(service, key, request, runtime)
                if (isinstance(client, AsyncOpenAI)
                        and service.settings.llm_base_url.rstrip('/') == 'https://openrouter.ai/api/v1'):
                    runtime.model_warmup = asyncio.create_task(_warm_model_connection(client))
                    runtime.stack.push_async_callback(_join_warmup, runtime.model_warmup)
                runtime.knowledge_task = asyncio.create_task(
                    _prefetch_knowledge_base(service, agent, query, runtime))
                runtime.stack.push_async_callback(_join_knowledge, runtime.knowledge_task)
            runtime.business = await runtime.operation(BusinessRuntime.open(
                request, grant, session_factory, registry, execution=runtime,
                collector=collector))
            runtime.check()
            # open() already loaded the result gate; avoid a duplicate round-trip
            # before the first model step on the Voice path.
            entry = await runtime.operation(
                runtime.business.execution_guard(allow_incomplete=True, reload=False))
            if entry is None:
                entry = runtime.business.completed_confirmation_replay()
            if profile is not None:
                emit_voice_checkpoint(runtime, "BUSINESS")
            emit_text_checkpoint(runtime, "BUSINESS")
            if entry is not None and entry.halt_status:
                if (runtime.handoff_enabled and entry.halt_status == "UNKNOWN"
                        and runtime.business.last_gate is not None
                        and runtime.business.last_gate.scope_unresolved):
                    await runtime.request_handoff("BUSINESS_UNCERTAIN", None)
                raise ManagedExecutionError(entry.error_code, processing_status=entry.halt_status)
            if entry is not None and entry.safe_draft is not None:
                answer = entry.safe_draft
            elif idle_input:
                # A control question is one complete draft. Do not ask the model
                # to append business speech while the caller starts answering it.
                # Entry/final gates and teardown still apply; no tool is called.
                from services.voice_reply_style import CONNECTION_CHECK
                answer = CONNECTION_CHECK
            else:
                if client is None:
                    client = _owned_client(service, key, request, runtime)
                managed_v3 = runtime.handoff_enabled
                delivery_by_id = ({
                    str(fact.message_id): fact for fact in request.communication_facts
                    if fact.kind == "DELIVERY_OBSERVATION"
                } if managed_v3 else None)
                communication = [_communication(message, managed_v3=managed_v3,
                                                  delivery_by_id=delivery_by_id)
                                 for message in request.messages]
                if profile is not None:
                    from services.voice_reply_style import past_idle_communication
                    communication = past_idle_communication(request, communication)
                answer = await service._execute_core(
                    db, agent=agent, llm_client=client, credentials_records=(),
                    communication=communication,
                    chat_context=ChatContext(channel=CHANNEL_PROMPT[request.channel],
                                             external_client_id=f"{EXECUTION_NAMESPACE}:{request.conversation_id}",
                                             managed_communication_context=(
                                                 _v3_communication_context(request) if managed_v3 else None)),
                    query=query, runtime=runtime,
                    selected_indices=request.selected_local_indices,
                    simple_selection=request.simple_selection,
                    partial_hook=partial_hook,
                    knowledge_task=getattr(runtime, "knowledge_task", None),
                )
            try:
                if runtime.handoff_enabled:
                    ready = ManagedTurnV3Result.from_request(
                        request, original_dispatch_id=runtime.original_dispatch_id,
                        request_fingerprint=runtime.request_fingerprint,
                        result_kind="EXECUTION_RESULT", processing_outcome="DRAFT_READY",
                        draft_text=answer, error_code=None, operation_refs=collector.snapshot(),
                        control_request=None,
                    )
                else:
                    ready = ManagedTurnResult.from_request(
                        request, status="DRAFT_READY", draft_text=answer, error_code=None,
                        operation_refs=collector.snapshot(),
                    )
                if len(ready.model_dump_json().encode("utf-8")) > MAX_RESULT_BYTES:
                    raise ValueError
            except Exception:
                raise ManagedExecutionError("INVALID_RESULT") from None
            outcome = dict(status="DRAFT_READY", draft_text=answer, error_code=None)
        except ManagedAgentNotFound:
            missing = True
        except ManagedControlRequested:
            outcome = dict(status="NO_RESPONSE", draft_text=None, error_code=None)
        except asyncio.CancelledError as exc:
            cancellation = exc
        except ManagedExecutionError as exc:
            outcome = dict(status=processing_status(exc.processing_status),
                           draft_text=None, error_code=exc.code)
        except BusinessSafetyError as exc:
            # Repository categories are internal. Keep the existing closed v1
            # error vocabulary; no mirrored Conversation schema change.
            outcome = dict(status=processing_status(exc.processing_status),
                           draft_text=None, error_code="TOOL_EXECUTION_UNKNOWN"
                           if exc.processing_status == "UNKNOWN" else "TOOL_SETUP_FAILED")
        except Exception:
            outcome = dict(status=processing_status(),
                           draft_text=None, error_code="INTERNAL_ERROR")
        finally:
            if not runtime.cancel_requested and not missing:
                try:
                    if runtime.control_request is not None:
                        retained_gate = await runtime.operation(retained_repository.result_gate(request, grant))
                        collector.observe_committed(retained_gate.references)
                    elif runtime.business is not None:
                        await runtime.operation(runtime.business.collect_refs())
                        retained_gate = runtime.business.last_gate
                        final_guard = await runtime.business.final_result_guard(reload=False)
                    else:
                        retained_gate = await runtime.operation(retained_repository.result_gate(request, grant))
                        collector.observe_committed(retained_gate.references)
                        final_guard = await retained_view(retained_gate).final_result_guard(reload=False)
                    apply_guard(final_guard)
                except ManagedExecutionError as exc:
                    # Preserve the pre-effect work-timeout contract only after
                    # a positive absent-scope observation and no business scope
                    # or operation was entered. This grants no draft/admission:
                    # a successful candidate still needs its final fresh gate.
                    absent_pre_effect_timeout = (
                        exc.code == "EXECUTION_TIMEOUT" and outcome["status"] != "DRAFT_READY"
                        and retained_gate is not None and not retained_gate.scope_exists
                        and not runtime.tool_dispatch_attempted and not collector.snapshot()
                        and (runtime.business is None or not runtime.business.business_scope_seen)
                    )
                    final_guard = None if absent_pre_effect_timeout else BusinessRuntime._halt(True)
                    outcome = dict(status=processing_status(exc.processing_status), draft_text=None, error_code=exc.code)
                except BaseException:
                    # Missing final scope uncertainty cannot permit ordinary prose.
                    final_guard = BusinessRuntime._halt(True)
                    outcome = dict(status=processing_status("UNKNOWN"), draft_text=None, error_code="TOOL_EXECUTION_UNKNOWN")
            try:
                await runtime.cleanup(lifecycle)
            except BaseException:
                outcome = dict(status=processing_status(),
                               draft_text=None, error_code="TOOL_CLEANUP_FAILED")
    if cancellation is not None or runtime.cancel_requested:
        if runtime.business is not None:
            runtime.business.finish_business_diagnostics("UNKNOWN")
        raise cancellation or asyncio.CancelledError()
    if missing:
        raise ManagedAgentNotFound
    # Reapply the last coherent point-in-time observation after cleanup. No locks
    # span serialization or response delivery; this grants no dispatch authority.
    if final_guard is not None and final_guard.halt_status and outcome["status"] != "DRAFT_READY":
        # Cleanup can supply its own safe diagnostic, but cannot downgrade
        # retained scope uncertainty merely because this Turn has no refs.
        outcome["status"] = processing_status(final_guard.halt_status)
        outcome["draft_text"] = None
    elif outcome["status"] == "DRAFT_READY":
        apply_guard(final_guard)
    if runtime.business is not None:
        proposal_present = bool(
            retained_gate is not None and retained_gate.plan is not None
            and retained_gate.plan.proposal_state in {"PREPARED", "CONFIRMED"}
        )
        runtime.business.finish_business_diagnostics(
            outcome["status"], proposal_present=proposal_present,
            operation_present=bool(collector.snapshot()),
        )
    if runtime.handoff_enabled:
        return ManagedTurnV3Result.from_request(
            request, original_dispatch_id=runtime.original_dispatch_id,
            request_fingerprint=runtime.request_fingerprint,
            result_kind="PLATFORM_CONTROL_REQUEST" if runtime.control_request else "EXECUTION_RESULT",
            processing_outcome=outcome["status"], draft_text=outcome["draft_text"],
            error_code=outcome["error_code"], operation_refs=collector.snapshot(),
            control_request=runtime.control_request,
        )
    return ManagedTurnResult.from_request(request, **outcome, operation_refs=collector.snapshot())
