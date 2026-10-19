"""HTTP cancellation still reaches the owner after a nested adapter operation."""
import asyncio
from contextlib import suppress

import anyio
import pytest

from services.managed_runtime import ManagedRuntime, join_owner
from services.managed_tools import ManagedExecutionError
from tests.test_chotacrm_adapter import crm_settings  # noqa: F401


@pytest.mark.parametrize('inner_outcome', ['success', 'error', 'local_timeout'])
async def test_cancel_joined_owner_after_nested_operation(inner_outcome):
    runtime = ManagedRuntime(30)
    deadline = runtime.deadline
    entered = asyncio.Event()
    release = asyncio.Event()
    cleaned = []

    async def inner():
        if inner_outcome == 'error':
            raise ValueError('synthetic read failure')
        if inner_outcome == 'local_timeout':
            await asyncio.Event().wait()
        return 'synthetic read result'

    async def work():
        try:
            with anyio.move_on_after(.01):
                await runtime.operation(inner())
        except ValueError:
            pass
        entered.set()
        await release.wait()

    async def owned():
        # This is the real ownership boundary used by managed turns. Cancelling
        # the HTTP task must signal the operation and join this shielded owner.
        with anyio.CancelScope(shield=True):
            try:
                await runtime.operation(work())
            finally:
                cleaned.append(asyncio.current_task())

    owner = asyncio.create_task(owned())
    joined = asyncio.create_task(join_owner(owner, runtime))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        joined.cancel()
        done, _ = await asyncio.wait({joined}, timeout=.25)
        cancelled_without_releasing_work = joined in done
    finally:
        # Even a failing regression releases its synthetic operation; no task
        # survives this test or waits out the 30-second production deadline.
        release.set()
        with suppress(asyncio.CancelledError):
            await joined
        with suppress(asyncio.CancelledError):
            await owner

    assert cancelled_without_releasing_work
    assert joined.cancelled() and owner.cancelled()
    assert cleaned == [owner]
    assert runtime.deadline == deadline
    assert runtime.active_scope is None


async def test_cancellation_inside_nested_operation_unwinds_in_same_owner():
    runtime = ManagedRuntime(30)
    entered = asyncio.Event()
    cleanup = []

    async def inner():
        try:
            entered.set()
            await asyncio.Event().wait()
        finally:
            cleanup.append(('inner', asyncio.current_task()))

    async def outer():
        try:
            await runtime.operation(inner())
        finally:
            cleanup.append(('outer', asyncio.current_task()))

    async def owned():
        with anyio.CancelScope(shield=True):
            await runtime.operation(outer())

    owner = asyncio.create_task(owned())
    joined = asyncio.create_task(join_owner(owner, runtime))
    await asyncio.wait_for(entered.wait(), 1)
    joined.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(joined, 1)
    assert cleanup == [('inner', owner), ('outer', owner)]
    assert owner.cancelled() and runtime.active_scope is None


async def test_outer_deadline_still_applies_after_nested_operation():
    runtime = ManagedRuntime(.03)
    deadline = runtime.deadline

    async def outer():
        assert await runtime.operation(asyncio.sleep(0, 'result')) == 'result'
        await asyncio.Event().wait()

    with pytest.raises(ManagedExecutionError, match='EXECUTION_TIMEOUT'):
        await runtime.operation(outer())
    assert runtime.deadline == deadline and runtime.active_scope is None


@pytest.mark.integration
async def test_cancel_after_real_adapter_read_interrupts_following_guard(
        db_session, crm_settings, monkeypatch):
    from tests.test_preparation_schema_correction import local_runtime

    business, runtime, provider, _ = await local_runtime(db_session, crm_settings, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    reads = []
    adapter = business.registration.adapter
    original_read, original_guard = adapter.read, business.execution_guard

    async def read(*args, **kwargs):
        result = await original_read(*args, **kwargs)
        reads.append(True)
        return result

    async def guard(*args, **kwargs):
        if reads:
            entered.set()
            await release.wait()
        return await original_guard(*args, **kwargs)

    monkeypatch.setattr(adapter, 'read', read)
    monkeypatch.setattr(business, 'execution_guard', guard)

    async def owned():
        with anyio.CancelScope(shield=True) as lifecycle:
            try:
                await runtime.operation(business.call_tool('catalog_search', {}))
            finally:
                await runtime.cleanup(lifecycle)

    owner = asyncio.create_task(owned())
    joined = asyncio.create_task(join_owner(owner, runtime))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        joined.cancel()
        done, _ = await asyncio.wait({joined}, timeout=1)
        cancelled_without_releasing_guard = joined in done
    finally:
        release.set()
        with suppress(asyncio.CancelledError):
            await joined
        with suppress(asyncio.CancelledError):
            await owner

    assert cancelled_without_releasing_guard and owner.cancelled()
    assert reads == [True] and provider['posts'] == 0
    assert runtime.active_scope is None
    assert await business.repository.selected_plan(business.request, business.grant) is None
