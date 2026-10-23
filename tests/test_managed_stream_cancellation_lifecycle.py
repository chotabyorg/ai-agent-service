"""Independent offline SSE ownership adversarial checks."""

import asyncio
import json
from contextlib import suppress
from types import SimpleNamespace
from uuid import uuid4

import anyio
import pytest

from api.internal import managed_turns as mod
from services.managed_runtime import ManagedRuntime


def value():
    return SimpleNamespace(
        max_partial_chars=500,
        contract_version=2,
        conversation_id=uuid4(),
        turn_id=uuid4(),
        agent_id=uuid4(),
        input_revision=1,
        authority_revision=1,
        input_first_sequence=1,
        input_last_sequence=1,
        context_last_sequence=1,
    )


@pytest.mark.parametrize("kind", ["normal", "error", "cleanup_error"])
async def test_terminal_after_partial_and_owner_done(monkeypatch, kind):
    request, runtime = value(), ManagedRuntime(30)

    async def execute(*args, **kwargs):
        await kwargs["partial_hook"]("First sentence. Second sentence.")
        if kind == "error":
            raise ValueError("synthetic internal error")
        result = {k: v for k, v in vars(request).items() if k != "max_partial_chars"}
        return mod.ManagedTurnResult(
            **result,
            status="FAILED" if kind == "cleanup_error" else "DRAFT_READY",
            draft_text=None if kind == "cleanup_error" else "Final authoritative text.",
            error_code="TOOL_CLEANUP_FAILED" if kind == "cleanup_error" else None,
        )

    monkeypatch.setattr(mod, "execute_managed_owned", execute)
    frames = [
        json.loads(chunk.decode().removeprefix("data: "))
        async for chunk in mod._stream_managed_turn(
            request, None, runtime, None, None, None
        )
    ]
    assert frames[0] == {"partial_text": "First sentence. ", "partial_final": False}
    finals = [frame for frame in frames if frame["partial_final"]]
    assert len(finals) == 1 and frames[-1] == finals[0]
    assert (
        finals[0]["status"]
        == {"normal": "DRAFT_READY", "error": "UNKNOWN", "cleanup_error": "FAILED"}[
            kind
        ]
    )
    assert (
        finals[0]["error_code"]
        == {
            "normal": None,
            "error": "INTERNAL_ERROR",
            "cleanup_error": "TOOL_CLEANUP_FAILED",
        }[kind]
    )
    assert not runtime.cancel_requested


async def test_close_observes_owner_error_when_already_done(monkeypatch):
    request, runtime = value(), ManagedRuntime(30)
    release, failed = asyncio.Event(), asyncio.Event()
    owners = []

    async def execute(*args, **kwargs):
        owners.append(asyncio.current_task())
        await kwargs["partial_hook"]("First sentence. Second sentence.")
        await release.wait()
        failed.set()
        raise mod.ManagedAgentNotFound()

    monkeypatch.setattr(mod, "execute_managed_owned", execute)
    stream = mod._stream_managed_turn(request, None, runtime, None, None, None)
    await anext(stream)
    release.set()
    await failed.wait()
    assert owners[0].done()
    try:
        await stream.aclose()
        # CPython flag is used only to deterministically detect unobserved-task warnings.
        assert not owners[0]._log_traceback
    finally:
        with suppress(BaseException):
            owners[0].result()


@pytest.mark.parametrize("cleanup_error", [False, True])
async def test_repeated_parent_cancel_waits_for_same_owner_cleanup(
    monkeypatch, cleanup_error
):
    runtime = ManagedRuntime(30)
    started, closing, release = (asyncio.Event() for _ in range(3))
    owners, cleaned = [], []

    async def execute(*args, **kwargs):
        owner = asyncio.current_task()
        owners.append(owner)
        with anyio.CancelScope(shield=True) as lifecycle:

            async def close():
                closing.set()
                await release.wait()
                cleaned.append(asyncio.current_task())
                if cleanup_error:
                    raise ValueError("synthetic cleanup failure")

            runtime.stack.push_async_callback(close)
            try:
                started.set()
                await runtime.operation(asyncio.Event().wait())
            finally:
                await runtime.cleanup(lifecycle)

    monkeypatch.setattr(mod, "execute_managed_owned", execute)
    stream = mod._stream_managed_turn(value(), None, runtime, None, None, None)
    reader = asyncio.create_task(anext(stream))
    await started.wait()
    reader.cancel()
    await closing.wait()
    reader.cancel()
    await asyncio.sleep(0)
    waited, cancel_count = not reader.done(), owners[0].cancelling()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await reader
    with suppress(BaseException):
        await owners[0]
    assert waited and cancel_count == 0
    assert owners[0].done() and cleaned == owners
    assert runtime.active_scope is None
    await stream.aclose()


@pytest.mark.parametrize("cleanup_mode", ["release", "deadline"])
async def test_real_owned_runtime_joins_and_cleanup_deadline_stays_bounded(
    monkeypatch, cleanup_mode
):
    from services.business_repository import BusinessRepository

    request = value()
    runtime = ManagedRuntime(30, request=request)
    started, closing, release = (asyncio.Event() for _ in range(3))
    owners, cleanup_tasks, cleanup_exited = [], [], []

    async def gate(self, request, grant):
        owners.append(asyncio.current_task())

        async def close():
            cleanup_tasks.append(asyncio.current_task())
            closing.set()
            try:
                await release.wait()
            finally:
                cleanup_exited.append(True)

        runtime.stack.push_async_callback(close)
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(BusinessRepository, "result_gate", gate)
    stream = mod._stream_managed_turn(request, None, runtime, None, None, None)
    reader = asyncio.create_task(anext(stream))
    await asyncio.wait_for(started.wait(), 5)
    reader.cancel()
    await asyncio.wait_for(closing.wait(), 5)
    waited, cancel_count = not reader.done(), owners[0].cancelling()
    if cleanup_mode == "release":
        release.set()
    try:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(reader, 5)
        # Existing runtime cleanup deadline (2 seconds with no stdio owners)
        # terminates blocked fake close; no hard wall-clock performance assertion.
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(owners[0], 5)
        assert waited and cancel_count == 0
        assert cleanup_tasks == owners and cleanup_exited == [True]
        assert owners[0].done() and runtime.active_scope is None
    finally:
        release.set()
        with suppress(BaseException):
            await owners[0]
        await stream.aclose()


async def test_new_parent_cancel_during_aclose_is_not_swallowed(monkeypatch):
    runtime = ManagedRuntime(30)
    closing, release = asyncio.Event(), asyncio.Event()
    owners = []

    async def execute(*args, **kwargs):
        owners.append(asyncio.current_task())
        with anyio.CancelScope(shield=True) as lifecycle:

            async def close():
                closing.set()
                await release.wait()

            runtime.stack.push_async_callback(close)
            try:
                await kwargs["partial_hook"]("First sentence. Second sentence.")
                await runtime.operation(asyncio.Event().wait())
            finally:
                await runtime.cleanup(lifecycle)

    monkeypatch.setattr(mod, "execute_managed_owned", execute)
    stream = mod._stream_managed_turn(value(), None, runtime, None, None, None)
    await anext(stream)
    closer = asyncio.create_task(stream.aclose())
    try:
        await asyncio.wait_for(closing.wait(), 5)
        closer.cancel()
        await asyncio.sleep(0)
        assert not closer.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await closer
        assert owners[0].done() and owners[0].cancelling() == 0
    finally:
        release.set()
        runtime.signal_cancel()
        with suppress(BaseException):
            await owners[0]
        with suppress(BaseException):
            await closer
