"""Real checkout/row-lock waits remain separate, bounded and cancellation safe."""
import asyncio
from dataclasses import replace
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from core.business_sql_diagnostics import BusinessSQLTiming, BusinessSQLTrace, _trusted_business_sql_record
from core.managed_security import managed_logging
from models.agent import AgentTemplate
from services.business_repository import BusinessRepository
from services.managed_runtime import ManagedRuntime
from tests.test_business_runtime import make_request, setup_business
from tests.test_managed_wait_diagnostics import logging_boundary, SECRET  # noqa: F401


def records(caplog):
    return [r for r in caplog.records if getattr(r, 'event', None) == 'business_sql_observed']


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_pool_wait_is_checkout_and_cancel_releases_owner(migrated_postgres, caplog, cancel):
    engine = create_async_engine(migrated_postgres, pool_size=1, max_overflow=0, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    execution = ManagedRuntime(10, request=make_request(uuid4()))
    repository = BusinessRepository(factory, execution=execution)
    entered = asyncio.Event()
    async def query():
        async with repository._session() as session:
            entered.set()
            async with repository._transaction(session, 'CONFIRM'):
                assert await session.scalar(text('select 42')) == 42
    task = None
    try:
        async with engine.connect():
            task = asyncio.create_task(query())
            await entered.wait()
            await asyncio.sleep(0.12)
            assert not task.done()
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
        if not cancel:
            await asyncio.wait_for(task, 3)
        row, = records(caplog)
        assert row.checkout_ms >= 90 and row.lock_count == 0
        assert row.state == ('CANCELLED' if cancel else 'COMPLETED')
        assert row.finish_kind == ('ROLLBACK' if cancel else 'COMMIT')
        assert engine.pool.checkedout() == 0
        async with engine.connect() as connection:
            assert await connection.scalar(text('select 7')) == 7
        assert len(records(caplog)) == 1
        assert SECRET not in caplog.text
    finally:
        if task is not None and not task.done():
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_native_row_wait_is_lock_and_rollback_leaves_row_unchanged(db_session, caplog, cancel):
    original, _, _, _ = await setup_business(db_session)
    execution = ManagedRuntime(10, request=original.request)
    repository = BusinessRepository(db_session, execution=execution)
    entered = asyncio.Event()
    async def query():
        async with repository._session() as session:
            async with repository._transaction(session, 'DISPATCH'):
                entered.set()
                agent = await session.scalar(select(AgentTemplate).where(
                    AgentTemplate.id == original.request.agent_id).with_for_update())
                assert agent.is_active
    caplog.clear()
    task = None
    try:
        async with db_session() as holder:
            async with holder.begin():
                await holder.scalar(select(AgentTemplate).where(
                    AgentTemplate.id == original.request.agent_id).with_for_update())
                task = asyncio.create_task(query())
                await entered.wait()
                await asyncio.sleep(0.12)
                assert not task.done()
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 3)
        if not cancel:
            await asyncio.wait_for(task, 3)
        row, = records(caplog)
        assert row.lock_count == 1 and row.lock_ms >= 90
        assert row.state == ('CANCELLED' if cancel else 'COMPLETED')
        assert row.finish_kind == ('ROLLBACK' if cancel else 'COMMIT')
        async with db_session() as db:
            assert (await db.get(AgentTemplate, original.request.agent_id)).is_active
        assert SECRET not in caplog.text
    finally:
        if task is not None and not task.done():
            task.cancel(); await asyncio.gather(task, return_exceptions=True)


def test_closed_metadata_rejects_forgery_and_never_retains_content(caplog):
    import core.business_sql_diagnostics as diagnostics
    value = BusinessSQLTiming(uuid4(), 1, 'CONFIRM', 'COMPLETED', 'COMMIT', 12, 2, 3, 4, 1, 2, 1, 2)
    token = managed_logging.set(True)
    try:
        diagnostics.emit_business_sql(value)
        row, = records(caplog)
        assert row.transaction_ms == 12 and not hasattr(row, 'statement')
        forged = logging.LogRecord('uvicorn.error', logging.INFO, '', 0, SECRET, (), None)
        forged._business_sql_timing = value
        assert _trusted_business_sql_record(forged) is None
        for bad in (replace(value, step=21), replace(value, stage=SECRET), replace(value, lock_ms=True)):
            diagnostics.emit_business_sql(bad)
        assert len(records(caplog)) == 1 and SECRET not in caplog.text
    finally:
        managed_logging.reset(token)


def test_observer_bound_and_failure_cannot_change_the_body(monkeypatch, caplog):
    import core.business_sql_diagnostics as diagnostics
    runtime = SimpleNamespace(request=SimpleNamespace(turn_id=uuid4()))
    token = managed_logging.set(True)
    try:
        for _ in range(25):
            with BusinessSQLTrace(runtime, 'ADMISSION_PROBE') as trace:
                trace.finish_body('COMMIT')
        assert len(records(caplog)) == 20 and runtime._business_sql_step == 21
        def fail(*args, **kwargs):
            raise RuntimeError(SECRET)
        monkeypatch.setattr(diagnostics, 'emit_business_sql', fail)
        with BusinessSQLTrace(SimpleNamespace(request=runtime.request), 'CONFIRM') as trace:
            trace.finish_body('COMMIT')
        assert SECRET not in caplog.text
    finally:
        managed_logging.reset(token)
