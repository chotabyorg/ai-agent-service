"""Managed adapter: explicit canonical projection, no Session or Redis access."""

import asyncio
from contextlib import asynccontextmanager

import anyio
from openai import AsyncOpenAI

from core.managed_security import EXECUTION_NAMESPACE
from schemas.managed_turn import MAX_RESULT_BYTES, ManagedTurnResult
from services.llm_service import LLMService
from services.managed_http_cleanup import close_managed_helper
from services.managed_runtime import CHANNEL_PROMPT, privacy_id
from services.managed_tools import ManagedExecutionError
from services.prompt_builder import ChatContext


class ManagedAgentNotFound(Exception):
    pass


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


def _communication(message):
    if message.actor in {"AGENT", "HUMAN"}:
        return {"role": "assistant", "content": message.text}
    text = ("[System communication event]\n" + message.text
            if message.actor == "SYSTEM" else message.text)
    return {"role": "user", "content": text}


async def execute_managed_owned(request, grant, runtime, db_provider):
    """All DB/runtime/resource work and strict LIFO teardown run in this task."""
    outcome = dict(status="FAILED", draft_text=None, error_code="INTERNAL_ERROR", operation_refs=[])
    missing = False
    cancellation = None
    with anyio.CancelScope(shield=True) as lifecycle:
        try:
            runtime.check()
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
            try:
                key = service._get_agent_api_key(agent)
            except Exception:
                raise ManagedExecutionError("AGENT_CONFIGURATION_ERROR") from None
            if not key:
                raise ManagedExecutionError("AGENT_KEY_MISSING")
            query = "\n\n".join(message.text for message in request.messages[
                request.input_first_sequence - 1:request.input_last_sequence
            ])
            if len(query) > 32768 or len(query.encode("utf-8")) > 65536:
                raise ManagedExecutionError("CONTEXT_LIMIT")
            runtime.check()
            client = service._create_llm_client(key, privacy_id(request), managed=True)
            # The proxy creates/closes each owned HTTP request client in its helper.
            if hasattr(client, "close"):
                runtime.stack.push_async_callback(_close_client, client)
            credentials = await runtime.operation(service.agent_service.get_credentials(db, agent.id))
            answer = await service._execute_core(
                db, agent=agent, llm_client=client, credentials_records=credentials,
                communication=[_communication(message) for message in request.messages],
                chat_context=ChatContext(channel=CHANNEL_PROMPT[request.channel],
                                         external_client_id=f"{EXECUTION_NAMESPACE}:{request.conversation_id}"),
                query=query, runtime=runtime,
                selected_indices=list(range(request.input_first_sequence - 1, request.input_last_sequence)),
                simple_selection=(request.input_first_sequence == request.input_last_sequence ==
                                  request.context_last_sequence and request.messages[-1].actor == "CUSTOMER"),
            )
            try:
                ready = ManagedTurnResult.from_request(
                    request, status="DRAFT_READY", draft_text=answer, error_code=None, operation_refs=[],
                )
                if len(ready.model_dump_json().encode("utf-8")) > MAX_RESULT_BYTES:
                    raise ValueError
            except Exception:
                raise ManagedExecutionError("INVALID_RESULT") from None
            outcome = dict(status="DRAFT_READY", draft_text=answer, error_code=None, operation_refs=[])
        except ManagedAgentNotFound:
            missing = True
        except asyncio.CancelledError as exc:
            cancellation = exc
        except ManagedExecutionError as exc:
            outcome = dict(status="UNKNOWN" if runtime.tool_dispatch_attempted else "FAILED",
                           draft_text=None, error_code=exc.code, operation_refs=[])
        except Exception:
            outcome = dict(status="UNKNOWN" if runtime.tool_dispatch_attempted else "FAILED",
                           draft_text=None, error_code="INTERNAL_ERROR", operation_refs=[])
        finally:
            try:
                await runtime.cleanup(lifecycle)
            except BaseException:
                outcome = dict(status="UNKNOWN" if runtime.tool_dispatch_attempted else "FAILED",
                               draft_text=None, error_code="TOOL_CLEANUP_FAILED", operation_refs=[])
    if cancellation is not None or runtime.cancel_requested:
        raise cancellation or asyncio.CancelledError()
    if missing:
        raise ManagedAgentNotFound
    return ManagedTurnResult.from_request(request, **outcome)
