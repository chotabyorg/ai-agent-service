"""One request-local resource owner and operation cancellation scopes."""

import asyncio
import json
from contextlib import AsyncExitStack
from uuid import UUID, uuid5

import anyio

from core.managed_security import EXECUTION_NAMESPACE
from services.managed_tools import ManagedExecutionError

PRIVACY_NAMESPACE = UUID("bc7459ee-8fe0-5c64-95f1-953ae385c7aa")
CHANNEL_PROMPT = {"TELEGRAM": "telegram", "INSTAGRAM": "instagram", "WEBSITE_WIDGET": "website",
                  "WHATSAPP": "whatsapp", "VOICE": "voice"}


def privacy_id(request):
    name = json.dumps([EXECUTION_NAMESPACE, request.owner_id, str(request.integration_id),
                       str(request.agent_id), request.channel, str(request.conversation_id)],
                      ensure_ascii=False, separators=(",", ":"))
    return uuid5(PRIVACY_NAMESPACE, name)


class ManagedRuntime:
    def __init__(self, seconds: float, *, request=None):
        # Stable trusted platform execution metadata is available to the shared
        # core/future receipt adapters; it is never a persistence/history owner.
        self.request = request
        self.execution_namespace = EXECUTION_NAMESPACE
        self.deadline = anyio.current_time() + seconds
        self.stack = AsyncExitStack()
        self.cancel_requested = False
        self.active_scope = None
        self.tool_dispatch_attempted = False
        self.business = None
        self.collector = None
        self.caller_phone = ""
        self.knowledge_task = None
        self.started_stdio_count = 0
        self.managers = []
        self._crm_http_clients = {}
        self._crm_http_closed = False

    def signal_cancel(self):
        self.cancel_requested = True
        if self.active_scope is not None:
            self.active_scope.cancel()

    def check(self):
        if self.cancel_requested:
            raise asyncio.CancelledError
        if anyio.current_time() >= self.deadline:
            raise ManagedExecutionError("EXECUTION_TIMEOUT")

    async def operation(self, awaitable):
        try:
            self.check()
        except BaseException:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise
        with anyio.CancelScope(deadline=self.deadline) as scope:
            previous_scope = self.active_scope
            self.active_scope = scope
            try:
                result = await awaitable
            finally:
                # Adapter READs can nest inside prompt/tool operations in this
                # same owner task. Restore their parent so later HTTP
                # cancellation still reaches the operation that is awaiting.
                self.active_scope = previous_scope
        self.check()
        if scope.cancel_called:
            raise ManagedExecutionError("EXECUTION_TIMEOUT")
        return result

    async def cleanup(self, lifecycle):
        # Do not admit a new pooled acquisition while existing callbacks join.
        self._crm_http_closed = True
        # Change the existing outer scope: no new scope may cross SDK scope exits.
        lifecycle.deadline = anyio.current_time() + 6 * self.started_stdio_count + 2
        try:
            await self.stack.aclose()
            if lifecycle.cancel_called:
                raise ManagedExecutionError("TOOL_CLEANUP_FAILED")
        except BaseException as exc:
            if isinstance(exc, asyncio.CancelledError) and self.cancel_requested:
                raise
            raise ManagedExecutionError("TOOL_CLEANUP_FAILED") from None
        finally:
            self._crm_http_clients.clear()
            for manager in self.managers:
                manager.stack = None
                manager.session = None
                manager._tools = []


async def join_owner(owner, runtime):
    """HTTP cancellation signals operations and joins; never task.cancel/aclose."""
    cancellation = None
    while True:
        try:
            result = await asyncio.shield(owner)
            break
        except asyncio.CancelledError as exc:
            if cancellation is None:
                cancellation = exc
            runtime.signal_cancel()
            if owner.done():
                # Retrieve its exception to avoid an unobserved failed task.
                try:
                    owner.result()
                except BaseException:
                    pass
                raise cancellation
    if cancellation is not None:
        raise cancellation
    return result
