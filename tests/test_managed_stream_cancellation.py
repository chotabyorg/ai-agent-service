"""SSE disconnect joins request-local cleanup before returning to ASGI."""
import asyncio
from contextlib import suppress
from types import SimpleNamespace

import anyio
import pytest

from api.internal import managed_turns
from services.managed_runtime import ManagedRuntime


@pytest.mark.parametrize('mode', ['task_cancel', 'receive_disconnect', 'send_disconnect', 'send_error'])
async def test_stream_disconnect_joins_owner_without_task_cancel(monkeypatch, mode):
    runtime = ManagedRuntime(30)
    entered, cleanup_entered, release_cleanup = (asyncio.Event() for _ in range(3))
    owners, cleaned = [], []
    disconnect, sending = asyncio.Event(), asyncio.Event()

    async def execute(*args, **kwargs):
        owner = asyncio.current_task()
        owners.append(owner)
        with anyio.CancelScope(shield=True) as lifecycle:
            async def close():
                cleanup_entered.set()
                await release_cleanup.wait()
                cleaned.append(owner)
            runtime.stack.push_async_callback(close)
            try:
                entered.set()
                if mode in {'send_disconnect', 'send_error'}:
                    await kwargs['partial_hook']('Здравствуйте! Следующее предложение.')
                await runtime.operation(asyncio.Event().wait())
            finally:
                await runtime.cleanup(lifecycle)

    monkeypatch.setattr(managed_turns, 'execute_managed_owned', execute)
    value = SimpleNamespace(max_partial_chars=500, stream_partials=True,
                            owner_id='owner', integration_id='integration',
                            channel='TELEGRAM', agent_id='agent')
    principal = SimpleNamespace(grants=[SimpleNamespace(scope=lambda: (
        value.owner_id, value.integration_id, value.channel, value.agent_id))])
    request = SimpleNamespace(scope={'managed_turn_request': value, 'managed_principal': principal},
                              app=SimpleNamespace(dependency_overrides={}), headers={})
    monkeypatch.setattr(managed_turns, 'ManagedRuntime', lambda *args, **kwargs: runtime)
    monkeypatch.setattr(managed_turns, '_business_dependency', lambda *args: None)
    response = await managed_turns.execute_managed_turn(request)
    stream = response.body_iterator
    async def receive():
        await disconnect.wait()
        return {'type': 'http.disconnect'}

    async def send(message):
        if message['type'] == 'http.response.body' and message.get('body'):
            sending.set()
            if mode == 'send_error':
                raise OSError('synthetic disconnected transport')
            await asyncio.Event().wait()

    async def consume():
        if mode == 'task_cancel':
            return await stream.__anext__()
        from starlette.requests import ClientDisconnect
        with suppress(ClientDisconnect):
            await response({'type': 'http', 'asgi': {
                'spec_version': '2.4' if mode == 'send_error' else '2.0'}}, receive, send)

    reader = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        if mode == 'task_cancel':
            reader.cancel()
        elif mode == 'receive_disconnect':
            disconnect.set()
        else:
            await asyncio.wait_for(sending.wait(), 1)
            disconnect.set()
        await asyncio.wait_for(cleanup_entered.wait(), 1)
        await asyncio.sleep(0)
        waited_for_cleanup = not reader.done()
        direct_cancels = owners[0].cancelling()
    finally:
        runtime.signal_cancel()
        release_cleanup.set()
        with suppress(asyncio.CancelledError, StopAsyncIteration):
            await reader
        with suppress(asyncio.CancelledError):
            await owners[0]
        await stream.aclose()
    assert waited_for_cleanup, 'SSE response returned while owned cleanup was still running'
    assert direct_cancels == 0, 'Owner must be cancelled through its active operation scope'
    assert cleaned == owners
    assert runtime.cancel_requested and runtime.active_scope is None
