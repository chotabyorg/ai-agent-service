"""Real PostgreSQL lock-order coverage for the irreversible acquisition downgrade."""

from __future__ import annotations

import asyncio
import os
import queue
import threading
import time
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from schemas.business import BusinessSafetyError
from tests.business_effect_helpers import prepare_effect_permit
from tests.test_business_runtime import actions, setup_business


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HEAD = "g5_20261015_01"
CURRENT_HEAD = "handoff_20261015_01"
PREVIOUS = "luna_20261012_01"


def _set_postgres_env(url: str) -> dict[str, str | None]:
    parsed = make_url(url)
    names = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
    previous = {name: os.environ.get(name) for name in names}
    values = {
        "POSTGRES_HOST": parsed.host or "localhost",
        "POSTGRES_PORT": str(parsed.port or 5432),
        "POSTGRES_DB": parsed.database or "postgres",
        "POSTGRES_USER": parsed.username or "postgres",
        "POSTGRES_PASSWORD": parsed.password or "postgres",
    }
    os.environ.update(values)
    get_settings.cache_clear()
    return previous


def _restore_postgres_env(previous: dict[str, str | None]) -> None:
    for name, value in previous.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    get_settings.cache_clear()


def _config() -> Config:
    return Config(str(PROJECT_ROOT / "alembic.ini"))


@pytest.fixture
def isolated_utf8_migration_postgres(postgres_url):
    """A per-test UTF8 database, separate from the native runner/full-suite DB."""
    admin_url = make_url(postgres_url).set(drivername="postgresql+asyncpg")
    database = "goal5_migration_" + uuid4().hex

    async def manage(statement: str) -> None:
        engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                await connection.execute(text(statement))
        finally:
            await engine.dispose()

    asyncio.run(manage(
        f'CREATE DATABASE "{database}" ENCODING \'UTF8\' LC_COLLATE \'C\' '
        "LC_CTYPE 'C' TEMPLATE template0"
    ))
    try:
        yield admin_url.set(database=database).render_as_string(hide_password=False)
    finally:
        asyncio.run(manage(f'DROP DATABASE IF EXISTS "{database}"'))


def _migration_thread(target: str, errors: queue.Queue[BaseException | None]) -> threading.Thread:
    def run() -> None:
        try:
            command.downgrade(_config(), target)
        except BaseException as error:  # Captured for assertions in the test thread.
            errors.put(error)
        else:
            errors.put(None)

    thread = threading.Thread(target=run, name=f"goal5-alembic-{uuid4().hex[:8]}", daemon=True)
    thread.start()
    return thread


async def _wait_for_table_lock(url: str, mode: str, *, granted: bool, timeout: float = 10) -> None:
    parsed = make_url(url)
    connection = await asyncpg.connect(
        host=parsed.host,
        port=parsed.port,
        user=parsed.username,
        password=parsed.password,
        database=parsed.database,
        timeout=5,
    )
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = await connection.fetchval(
                "SELECT EXISTS ("
                " SELECT 1 FROM pg_locks l JOIN pg_class c ON c.oid=l.relation "
                " WHERE c.relname='business_operations' AND l.mode=$1 "
                " AND l.granted=$2 AND l.pid<>pg_backend_pid())",
                mode,
                granted,
            )
            if found:
                return
            await asyncio.sleep(0.01)
    finally:
        await connection.close()
    raise AssertionError(f"timed out waiting for business_operations {mode} granted={granted}")


async def _head_database(url: str):
    engine = create_async_engine(url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    return engine, factory


def _migrate_to_goal5_runtime(url: str) -> None:
    previous = _set_postgres_env(url)
    try:
        async def verify_database() -> None:
            parsed = make_url(url)
            connection = await asyncpg.connect(
                host=parsed.host,
                port=parsed.port,
                user=parsed.username,
                password=parsed.password,
                database=parsed.database,
                timeout=5,
            )
            try:
                version = int(await connection.fetchval("SHOW server_version_num"))
                encoding = await connection.fetchval("SHOW server_encoding")
                if not 160000 <= version < 170000 or encoding != "UTF8":
                    raise AssertionError("migration race tests require native UTF8 PostgreSQL 16")
            finally:
                await connection.close()

        asyncio.run(verify_database())
        # Keep this test's barrier choreography on the Goal5 migration under
        # test. The newer Handoff migration intentionally acquires a separate
        # Agent row lock first, which would make this test observe the wrong
        # lock queue and would test a different downgrade.
        command.upgrade(_config(), HEAD)

        async def install_current_agent_runtime_columns() -> None:
            engine = create_async_engine(url, poolclass=NullPool)
            try:
                async with engine.begin() as connection:
                    # The application ORM now includes Handoff lifecycle state.
                    # Add that inert runtime projection to this Goal5-only test
                    # schema without advancing Alembic, so current ORM reads
                    # work while the real Goal5 downgrade remains the first
                    # schema transition in the lock race.
                    await connection.execute(text(
                        "ALTER TABLE agent_templates "
                        "ADD COLUMN handoff_lifecycle_generation BIGINT NOT NULL DEFAULT 0, "
                        "ADD COLUMN handoff_lifecycle_state VARCHAR(32) NOT NULL DEFAULT 'STABLE', "
                        "ADD COLUMN handoff_lifecycle_source_generation BIGINT NULL, "
                        "ADD COLUMN handoff_lifecycle_scope_snapshot JSONB NULL"
                    ))
                    await connection.execute(text(
                        "UPDATE agent_templates SET handoff_lifecycle_generation=1 WHERE is_active IS TRUE"
                    ))
                    version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                    if version != HEAD:
                        raise AssertionError("Goal5 race fixture must remain at the Goal5 Alembic revision")
            finally:
                await engine.dispose()

        asyncio.run(install_current_agent_runtime_columns())
    finally:
        _restore_postgres_env(previous)


def _migrate_to_current_head(url: str) -> None:
    previous = _set_postgres_env(url)
    try:
        command.upgrade(_config(), "head")
    finally:
        _restore_postgres_env(previous)


def test_runtime_acquisition_commit_before_downgrade_lock_makes_downgrade_refuse(
    isolated_utf8_migration_postgres,
):
    """Pause after the real runtime UPDATE; Alembic must see it after the row commits."""
    url = isolated_utf8_migration_postgres
    _migrate_to_goal5_runtime(url)
    previous = _set_postgres_env(url)

    async def scenario() -> None:
        engine, factory = await _head_database(url)
        try:
            runtime, provider, _, _ = await setup_business(factory)
            plan = await runtime.freeze_plan(actions("12:00"))
            operation = plan.operations[0]
            writer_updated = threading.Event()
            release_writer = threading.Event()
            lock_observed = threading.Event()
            coordinator_errors: queue.Queue[BaseException] = queue.Queue()
            migration_errors: queue.Queue[BaseException | None] = queue.Queue()

            def pause_after_request_update(_connection, _cursor, statement, _parameters, _context, _many):
                normalized = " ".join(statement.lower().split())
                if (normalized.startswith("update business_operations set")
                        and "effect_request_turn_id" in normalized):
                    writer_updated.set()
                    if not release_writer.wait(timeout=15):
                        raise AssertionError("writer barrier timed out")

            def coordinate() -> None:
                if not writer_updated.wait(timeout=10):
                    coordinator_errors.put(AssertionError("runtime acquisition did not reach its evidence UPDATE"))
                    release_writer.set()
                    return
                migration = _migration_thread(PREVIOUS, migration_errors)
                try:
                    asyncio.run(_wait_for_table_lock(url, "AccessExclusiveLock", granted=False))
                    lock_observed.set()
                except BaseException as error:
                    coordinator_errors.put(error)
                finally:
                    release_writer.set()
                migration.join(timeout=15)
                if migration.is_alive():
                    coordinator_errors.put(AssertionError("Alembic downgrade did not finish after writer commit"))

            event.listen(engine.sync_engine, "after_cursor_execute", pause_after_request_update)
            coordinator = threading.Thread(target=coordinate, name="goal5-migration-coordinate", daemon=True)
            coordinator.start()
            try:
                acquisition = await runtime.repository.prepare_effect_acquisition(
                    runtime.request,
                    runtime.grant,
                    plan.id,
                    operation.id,
                    runtime.registration,
                    runtime.facts_resolver(actions("12:00")),
                )
            finally:
                release_writer.set()
                event.remove(engine.sync_engine, "after_cursor_execute", pause_after_request_update)
                coordinator.join(timeout=20)

            assert not coordinator.is_alive()
            assert coordinator_errors.empty(), list(coordinator_errors.queue)
            assert lock_observed.is_set()
            assert acquisition.reason == "REQUESTED"
            assert acquisition.request is not None
            assert acquisition.permit_id is None
            assert provider.posts == 0
            assert migration_errors.qsize() == 1
            error = migration_errors.get_nowait()
            assert isinstance(error, RuntimeError)
            assert "Refusing downgrade while business effect acquisition evidence exists" in str(error)

            async with engine.connect() as connection:
                version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                columns = set((await connection.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='business_operations'"
                ))).scalars())
                retained = (await connection.execute(text(
                    "SELECT effect_request_turn_id,effect_permit_id,status "
                    "FROM business_operations WHERE id=:operation_id"
                ), {"operation_id": operation.id})).one()
            assert version == HEAD
            assert {"effect_request_turn_id", "effect_permit_id"} <= columns
            assert retained.effect_request_turn_id == runtime.request.turn_id
            assert retained.effect_permit_id is None
            assert retained.status == "RESERVED"
        finally:
            await engine.dispose()

    try:
        asyncio.run(scenario())
    finally:
        _restore_postgres_env(previous)


def test_downgrade_lock_queued_before_runtime_prevents_post_drop_acquisition_commit(
    isolated_utf8_migration_postgres,
):
    """An actual Alembic lock queued first must precede a later runtime writer."""
    url = isolated_utf8_migration_postgres
    _migrate_to_goal5_runtime(url)
    previous = _set_postgres_env(url)

    async def scenario() -> None:
        engine, factory = await _head_database(url)
        holder = None
        holder_transaction = None
        try:
            runtime, provider, _, _ = await setup_business(factory)
            plan = await runtime.freeze_plan(actions("12:00"))
            operation = plan.operations[0]

            # ACCESS SHARE allows reads but blocks the downgrade's first
            # ACCESS EXCLUSIVE lock. It makes the PostgreSQL queue ordering
            # observable without sleeps or test-only changes to production SQL.
            holder = await engine.connect()
            holder_transaction = await holder.begin()
            await holder.execute(text("LOCK TABLE business_operations IN ACCESS SHARE MODE"))

            migration_errors: queue.Queue[BaseException | None] = queue.Queue()
            migration = _migration_thread(PREVIOUS, migration_errors)
            await _wait_for_table_lock(url, "AccessExclusiveLock", granted=False)

            acquisition_task = asyncio.create_task(runtime.repository.prepare_effect_acquisition(
                runtime.request,
                runtime.grant,
                plan.id,
                operation.id,
                runtime.registration,
                runtime.facts_resolver(actions("12:00")),
            ))
            try:
                await _wait_for_table_lock(url, "RowShareLock", granted=False, timeout=5)
            except AssertionError:
                # The production NOWAIT/lock-timeout path may fail closed
                # before it remains visible in pg_locks; either is safe, but
                # a successful writer is never accepted in this ordering.
                pass

            await holder_transaction.commit()
            await holder.close()
            holder = None
            holder_transaction = None
            await asyncio.to_thread(migration.join, 15)
            assert not migration.is_alive()
            assert migration_errors.qsize() == 1
            assert migration_errors.get_nowait() is None

            try:
                await asyncio.wait_for(acquisition_task, timeout=10)
            except BusinessSafetyError as error:
                assert error.code == "ADMISSION_CONTENDED"
            except Exception as error:
                assert isinstance(error, DBAPIError), type(error)
                assert "effect_request_turn_id" in str(error).lower()
            else:
                pytest.fail("runtime acquisition committed after the downgrade removed its evidence schema")

            # Once the real downgrade has committed, another actual runtime
            # acquisition must fail on the removed schema and cannot persist a
            # request into the predecessor revision.
            with pytest.raises(DBAPIError) as post_drop:
                await runtime.repository.prepare_effect_acquisition(
                    runtime.request,
                    runtime.grant,
                    plan.id,
                    operation.id,
                    runtime.registration,
                    runtime.facts_resolver(actions("12:00")),
                )
            assert "effect_request_turn_id" in str(post_drop.value).lower()
            assert provider.posts == 0

            async with engine.connect() as connection:
                version = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                columns = set((await connection.execute(text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='business_operations'"
                ))).scalars())
                retained = (await connection.execute(text(
                    "SELECT id,status,attempt_count FROM business_operations WHERE id=:operation_id"
                ), {"operation_id": operation.id})).one()
            assert version == PREVIOUS
            assert "effect_request_turn_id" not in columns
            assert "effect_permit_id" not in columns
            assert retained.id == operation.id
            assert retained.status == "RESERVED" and retained.attempt_count == 0
        finally:
            if holder_transaction is not None and holder_transaction.is_active:
                await holder_transaction.rollback()
            if holder is not None:
                await holder.close()
            await engine.dispose()

    try:
        asyncio.run(scenario())
    finally:
        _restore_postgres_env(previous)


@pytest.mark.parametrize("state", ["REQUESTED", "GRANTED"])
def test_populated_request_or_permit_refuses_downgrade(isolated_utf8_migration_postgres, state):
    """Both durable request-only and permit-granted evidence block schema loss."""
    url = isolated_utf8_migration_postgres
    _migrate_to_goal5_runtime(url)
    previous = _set_postgres_env(url)
    async def seed_and_check() -> None:
        engine, factory = await _head_database(url)
        try:
            runtime, _, _, _ = await setup_business(factory)
            plan = await runtime.freeze_plan(actions("12:00"))
            operation = plan.operations[0]
            if state == "REQUESTED":
                decision = await runtime.repository.prepare_effect_acquisition(
                    runtime.request,
                    runtime.grant,
                    plan.id,
                    operation.id,
                    runtime.registration,
                    runtime.facts_resolver(actions("12:00")),
                )
                assert decision.reason == "REQUESTED"
                assert decision.request is not None and decision.permit_id is None
            else:
                request = await prepare_effect_permit(runtime, plan, operation)
                assert request is not None
            retained = await runtime.repository.selected_plan(runtime.request, runtime.grant)
            assert retained is not None
            assert retained.operations[0].effect_request_turn_id == runtime.request.turn_id
            assert (retained.operations[0].effect_permit_id is not None) is (state == "GRANTED")
        finally:
            await engine.dispose()

    try:
        asyncio.run(seed_and_check())
        with pytest.raises(RuntimeError, match="Refusing downgrade while business effect acquisition evidence exists"):
            command.downgrade(_config(), PREVIOUS)
        async def verify() -> None:
            engine, _ = await _head_database(url)
            try:
                async with engine.connect() as connection:
                    assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == HEAD
                    assert await connection.scalar(text(
                        "SELECT count(*) FROM business_operations "
                        "WHERE effect_request_turn_id IS NOT NULL"
                    )) == 1
            finally:
                await engine.dispose()

        asyncio.run(verify())
    finally:
        _restore_postgres_env(previous)


@pytest.mark.parametrize("state", ["REQUESTED", "GRANTED"])
def test_current_head_downgrade_refusal_rolls_back_handoff_and_goal5_schema(
    isolated_utf8_migration_postgres, state,
):
    """A Goal5 refusal below Handoff must roll back every newer migration too."""
    url = isolated_utf8_migration_postgres
    _migrate_to_current_head(url)
    previous = _set_postgres_env(url)
    runtime_agent_id = None

    async def seed_and_check() -> None:
        nonlocal runtime_agent_id
        engine, factory = await _head_database(url)
        try:
            runtime, _, _, _ = await setup_business(factory)
            runtime_agent_id = runtime.request.agent_id
            plan = await runtime.freeze_plan(actions("12:00"))
            operation = plan.operations[0]
            if state == "REQUESTED":
                decision = await runtime.repository.prepare_effect_acquisition(
                    runtime.request,
                    runtime.grant,
                    plan.id,
                    operation.id,
                    runtime.registration,
                    runtime.facts_resolver(actions("12:00")),
                )
                assert decision.reason == "REQUESTED"
                assert decision.request is not None and decision.permit_id is None
            else:
                request = await prepare_effect_permit(runtime, plan, operation)
                assert request is not None

            retained = await runtime.repository.selected_plan(runtime.request, runtime.grant)
            assert retained is not None
            assert retained.operations[0].effect_request_turn_id == runtime.request.turn_id
            assert (retained.operations[0].effect_permit_id is not None) is (state == "GRANTED")
        finally:
            await engine.dispose()

    async def verify_rollback() -> None:
        engine, _ = await _head_database(url)
        try:
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == CURRENT_HEAD
                operation_columns = set((await connection.execute(text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name='business_operations'"
                ))).scalars())
                agent_columns = set((await connection.execute(text(
                    "SELECT column_name FROM information_schema.columns WHERE table_name='agent_templates'"
                ))).scalars())
                assert {"effect_request_turn_id", "effect_permit_id"} <= operation_columns
                assert {"handoff_lifecycle_generation", "handoff_lifecycle_state",
                        "handoff_lifecycle_scope_snapshot"} <= agent_columns
                assert await connection.scalar(text(
                    "SELECT to_regclass('public.managed_execution_receipts')"
                )) == "managed_execution_receipts"
                retained = (await connection.execute(text(
                    "SELECT effect_request_turn_id,effect_permit_id,status FROM business_operations"
                ))).one()
                lifecycle = (await connection.execute(text(
                    "SELECT handoff_lifecycle_generation,handoff_lifecycle_state,"
                    "handoff_lifecycle_source_generation,handoff_lifecycle_scope_snapshot "
                    "FROM agent_templates WHERE id=:agent_id"
                ), {"agent_id": runtime_agent_id})).one()
            assert retained.effect_request_turn_id is not None
            assert (retained.effect_permit_id is not None) is (state == "GRANTED")
            assert retained.status == "RESERVED"
            assert lifecycle == (0, "STABLE", None, None)
        finally:
            await engine.dispose()

    try:
        asyncio.run(seed_and_check())
        # The full downgrade first attempts to remove Handoff's newer schema,
        # then Goal5 refuses because the operation retains acquisition evidence.
        with pytest.raises(RuntimeError, match="Refusing downgrade while business effect acquisition evidence exists"):
            command.downgrade(_config(), PREVIOUS)
        asyncio.run(verify_rollback())
    finally:
        _restore_postgres_env(previous)
