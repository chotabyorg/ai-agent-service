"""Real disposable PostgreSQL empty/populated migration and drift checks."""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings
from db.database import Base


def _set_postgres_env(url):
    """Local pure helper: importing conftest would regenerate its fixture key."""
    parsed = make_url(url)
    values = {"POSTGRES_HOST": parsed.host or "localhost", "POSTGRES_PORT": str(parsed.port or 5432),
              "POSTGRES_DB": parsed.database or "postgres", "POSTGRES_USER": parsed.username or "postgres",
              "POSTGRES_PASSWORD": parsed.password or "postgres"}
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    return previous


def _restore_env(previous):
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_business_migration_preserves_current_main_data_and_refuses_populated_downgrade(isolated_migration_postgres):
    project = Path(__file__).resolve().parents[1]
    config = Config(str(project / "alembic.ini"))
    # Exercise this historical schema boundary independently of later irreversible
    # data migrations. The model migration tests assert the current single head.
    assert ScriptDirectory.from_config(config).get_revision("bv2_20261008_01") is not None
    previous = _set_postgres_env(isolated_migration_postgres)
    get_settings.cache_clear()
    async_url = isolated_migration_postgres
    agent_id, owner_id, session_id = uuid4(), uuid4(), uuid4()

    async def query(mode):
        engine = create_async_engine(async_url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                if mode == "drift":
                    # command.check requires the newest revision even when a later
                    # data-only migration leaves this historical schema unchanged.
                    differences = await connection.run_sync(lambda sync: compare_metadata(
                        MigrationContext.configure(sync, opts={"compare_type": True}), Base.metadata))
                    assert differences == []
                elif mode == "clear":
                    await connection.execute(text("TRUNCATE business_operations,business_turn_plan_links,business_plans,"
                        "business_turn_contexts,business_scopes,business_bindings,conversation_sessions,agent_credentials,agent_templates CASCADE"))
                elif mode == "seed":
                    await connection.execute(text("INSERT INTO agent_templates(id,user_id,name,system_prompt,llm_provider,model_name,temperature,is_active,timezone,encrypted_openrouter_key) "
                        "VALUES (:id,:owner,'preserved','prompt','openai','fake',0.5,true,'Europe/Minsk','encrypted-test-value')"), {"id": agent_id, "owner": owner_id})
                    await connection.execute(text("INSERT INTO agent_credentials(agent_id,service_name,credentials,enabled_tools) VALUES (:id,'fake','{}','[\"read\"]')"), {"id": agent_id})
                    await connection.execute(text("INSERT INTO conversation_sessions(id,agent_id,external_client_id,channel) VALUES (:sid,:id,'legacy-preserved','telegram')"), {"sid": session_id, "id": agent_id})
                elif mode == "verify":
                    row = (await connection.execute(text("SELECT is_active,encrypted_openrouter_key FROM agent_templates WHERE id=:id"), {"id": agent_id})).one()
                    assert row == (True, "encrypted-test-value")
                    assert await connection.scalar(text("SELECT count(*) FROM agent_credentials WHERE agent_id=:id"), {"id": agent_id}) == 1
                    assert await connection.scalar(text("SELECT count(*) FROM conversation_sessions WHERE id=:id"), {"id": session_id}) == 1
                    assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == "bv2_20261008_01"
                    await connection.execute(text("INSERT INTO business_bindings(id,owner_id,agent_owner_id,agent_id,integration_id,channel,adapter_id,connection_id,authorization_generation,context_generation,catalog_revision,enabled_capabilities,active) "
                        "VALUES (:binding,'retained',:owner,:id,:integration,'TELEGRAM','fake',:connection,1,1,:hash,'[]',false)"),
                        {"binding": uuid4(), "owner": owner_id, "id": agent_id, "integration": uuid4(), "connection": uuid4(), "hash": "a" * 64})
        finally:
            await engine.dispose()

    try:
        command.upgrade(config, "bv2_20261008_01")
        asyncio.run(query("clear"))
        command.downgrade(config, "c4d5e6f7g8h9")
        asyncio.run(query("seed"))
        command.upgrade(config, "bv2_20261008_01")
        command.current(config)
        asyncio.run(query("drift"))
        asyncio.run(query("verify"))
        with pytest.raises(RuntimeError, match="Refusing downgrade"):
            command.downgrade(config, "c4d5e6f7g8h9")
        asyncio.run(query("clear"))
        command.downgrade(config, "c4d5e6f7g8h9")
        command.upgrade(config, "bv2_20261008_01")
        asyncio.run(query("drift"))
    finally:
        _restore_env(previous)
        get_settings.cache_clear()
