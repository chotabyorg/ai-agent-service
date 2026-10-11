import os
import asyncio
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

# Ensure a valid Fernet key is available BEFORE importing the app.
# LLMService (and its EncryptionService) is instantiated at module level
# during `import main`, so the key must already be in os.environ.
from cryptography.fernet import Fernet

os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import core.redis as redis_module
import main
from api.dependencies import get_db
from services.bitrix_webhook_validator import (
    BitrixCredentialsValidationResult,
    BitrixCredentialsValidationStatus,
)
from services.google_workspace_validator import (
    GoogleCredentialsValidationResult,
    GoogleCredentialsValidationStatus,
)
from services.subscription_client import SubscriptionClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _set_postgres_env(url: str) -> dict[str, str | None]:
    parsed = make_url(url)
    previous = {
        "POSTGRES_HOST": os.environ.get("POSTGRES_HOST"),
        "POSTGRES_PORT": os.environ.get("POSTGRES_PORT"),
        "POSTGRES_DB": os.environ.get("POSTGRES_DB"),
        "POSTGRES_USER": os.environ.get("POSTGRES_USER"),
        "POSTGRES_PASSWORD": os.environ.get("POSTGRES_PASSWORD"),
    }
    os.environ["POSTGRES_HOST"] = parsed.host or "localhost"
    os.environ["POSTGRES_PORT"] = str(parsed.port or 5432)
    os.environ["POSTGRES_DB"] = parsed.database or "postgres"
    os.environ["POSTGRES_USER"] = parsed.username or "postgres"
    os.environ["POSTGRES_PASSWORD"] = parsed.password or "postgres"
    return previous


def _restore_env(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    testcontainers = pytest.importorskip("testcontainers.postgres")
    previous_ryuk = os.environ.get("TESTCONTAINERS_RYUK_DISABLED")
    os.environ["TESTCONTAINERS_RYUK_DISABLED"] = "true"
    container = testcontainers.PostgresContainer("postgres:16-alpine")
    container.start()
    try:
        yield container.get_connection_url()
    finally:
        container.stop()
        if previous_ryuk is None:
            os.environ.pop("TESTCONTAINERS_RYUK_DISABLED", None)
        else:
            os.environ["TESTCONTAINERS_RYUK_DISABLED"] = previous_ryuk


@pytest.fixture(scope="session")
def migrated_postgres(postgres_url: str) -> str:
    from core.config import get_settings

    previous = _set_postgres_env(postgres_url)
    get_settings.cache_clear()
    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    try:
        command.upgrade(alembic_config, "head")
    finally:
        _restore_env(previous)
        get_settings.cache_clear()
    return postgres_url.replace("psycopg2", "asyncpg")


@pytest.fixture
def isolated_migration_postgres(postgres_url: str) -> Iterator[str]:
    """Keep historical/irreversible migration exercises off the shared test DB."""
    admin_url = make_url(postgres_url).set(drivername="postgresql+asyncpg")
    database = "migration_" + uuid4().hex

    async def manage(action: str) -> None:
        engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                # The identifier is generated locally from a UUID, never user input.
                await connection.execute(text(f'{action} DATABASE "{database}"'))
        finally:
            await engine.dispose()

    asyncio.run(manage("CREATE"))
    try:
        yield admin_url.set(database=database).render_as_string(hide_password=False)
    finally:
        asyncio.run(manage("DROP"))


@pytest_asyncio.fixture
async def db_engine(migrated_postgres: str):
    engine = create_async_engine(
        migrated_postgres,
        future=True,
        poolclass=NullPool,
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def db_session_factory(db_engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@pytest_asyncio.fixture
async def clean_database(db_engine) -> AsyncIterator[None]:
    async with db_engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE TABLE business_operations, business_turn_plan_links, business_plans, "
                "business_turn_contexts, business_scopes, business_bindings, business_connections, "
                "conversation_sessions, agent_credentials, agent_templates "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture(autouse=True)
def disable_lifespan_redis(monkeypatch: pytest.MonkeyPatch) -> None:
    async def noop() -> None:
        return None

    monkeypatch.setattr(main, "init_redis", noop)
    monkeypatch.setattr(main, "close_redis", noop)


@pytest.fixture(autouse=True)
def mock_bitrix_webhook_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_validate_bitrix_credentials(credentials: dict[str, object]) -> BitrixCredentialsValidationResult:
        normalized_credentials: dict[str, str] = {}
        portal_url = credentials.get("portal_url")
        webhook_url = credentials.get("webhook_url")
        if isinstance(portal_url, str):
            normalized_credentials["portal_url"] = portal_url
        if isinstance(webhook_url, str):
            normalized_credentials["webhook_url"] = webhook_url

        return BitrixCredentialsValidationResult(
            status=BitrixCredentialsValidationStatus.VALID,
            normalized_credentials=normalized_credentials,
        )

    monkeypatch.setattr(
        "api.v1.agents.validate_bitrix_credentials",
        fake_validate_bitrix_credentials,
    )


@pytest.fixture(autouse=True)
def mock_google_workspace_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_validate_google_workspace_credentials(
        credentials: dict[str, object],
    ) -> GoogleCredentialsValidationResult:
        normalized_credentials: dict[str, object] = {
            key: credentials[key]
            for key in credentials
            if isinstance(credentials.get(key), (str, int, float, bool))
        }
        return GoogleCredentialsValidationResult(
            status=GoogleCredentialsValidationStatus.VALID,
            normalized_credentials=normalized_credentials,
        )

    monkeypatch.setattr(
        "api.v1.agents.validate_google_workspace_credentials",
        fake_validate_google_workspace_credentials,
    )


@pytest.fixture
def override_db_session(
    db_session_factory: async_sessionmaker[AsyncSession],
) -> Iterator[async_sessionmaker[AsyncSession]]:
    async def _override_get_db() -> AsyncIterator[AsyncSession]:
        async with db_session_factory() as session:
            yield session

    from services.business_registry import EMPTY_BUSINESS_REGISTRY

    absent = object()
    old_factory = getattr(main.app.state, "business_session_factory", absent)
    old_registry = getattr(main.app.state, "business_registry", absent)
    main.app.state.business_session_factory = db_session_factory
    main.app.state.business_registry = EMPTY_BUSINESS_REGISTRY
    main.app.dependency_overrides[get_db] = _override_get_db
    try:
        yield db_session_factory
    finally:
        main.app.dependency_overrides.clear()
        for name, previous in (("business_session_factory", old_factory), ("business_registry", old_registry)):
            if previous is absent:
                delattr(main.app.state, name)
            else:
                setattr(main.app.state, name, previous)


@pytest.fixture
def db_session(
    clean_database,
    override_db_session: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    return override_db_session


@pytest_asyncio.fixture
async def fake_redis(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[object]:
    fakeredis = pytest.importorskip("fakeredis.aioredis")
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_module, "_redis_client", client)
    monkeypatch.setattr(redis_module, "_redis_pool", None)
    try:
        yield client
    finally:
        await client.flushall()
        if hasattr(client, "aclose"):
            await client.aclose()
        else:
            await client.close()
        monkeypatch.setattr(redis_module, "_redis_client", None)
        monkeypatch.setattr(redis_module, "_redis_pool", None)


@pytest_asyncio.fixture
async def api_client(
    clean_database,
    override_db_session: async_sessionmaker[AsyncSession],
    fake_redis: object,
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=True)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest_asyncio.fixture
async def api_client_no_raise(
    clean_database,
    override_db_session: async_sessionmaker[AsyncSession],
    fake_redis: object,
) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=main.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


@pytest.fixture
def mock_subscription_client() -> AsyncMock:
    """A mock SubscriptionClient that defaults to ``{"allowed": True}``."""

    client = AsyncMock(spec=SubscriptionClient)
    client.check.return_value = {"allowed": True}
    return client


@pytest.fixture
def enable_subscription_check(mock_subscription_client: AsyncMock) -> Iterator[AsyncMock]:
    """Inject *mock_subscription_client* into ``app.state`` for the test."""

    main.app.state.subscription_client = mock_subscription_client
    try:
        yield mock_subscription_client
    finally:
        main.app.state.subscription_client = None
