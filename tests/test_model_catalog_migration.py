"""Real PostgreSQL data migration and next-invocation model selection."""
import asyncio
import importlib
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.config import Settings, get_settings
from services.encryption_service import EncryptionService
from services.llm_service import LLMService
from tests.fakes import ScriptedLLMClient, build_llm_response
from tests.test_business_migrations import _restore_env, _set_postgres_env

REVISION = "luna_20261012_01"
PREVIOUS = "bv2_20261008_01"


@pytest.mark.integration
def test_model_migration_preserves_everything_except_model_and_next_call_uses_luna(isolated_migration_postgres, monkeypatch):
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    assert ScriptDirectory.from_config(config).get_heads() == [REVISION]
    previous_env = _set_postgres_env(isolated_migration_postgres)
    get_settings.cache_clear()
    ids = [uuid4() for _ in range(3)]
    owner = uuid4()
    encrypted_key = EncryptionService().encrypt("synthetic-local-key")

    async def seed():
        engine = create_async_engine(isolated_migration_postgres, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                for index, old_model in enumerate(["google/gemini-2.5-flash", "xiaomi/mimo-v2.5", "unexpected/legacy-value"]):
                    await connection.execute(text(
                        "INSERT INTO agent_templates(id,user_id,name,system_prompt,llm_provider,model_name,temperature,is_active,timezone,prompt_config,encrypted_openrouter_key) "
                        "VALUES (:id,:owner,:name,'Synthetic prompt','openrouter',:model,:temperature,:active,'Europe/Minsk',:prompt,:key)"),
                        {"id": ids[index], "owner": owner, "name": f"Synthetic {index}", "model": old_model,
                         "temperature": 0.2 + index / 10, "active": index != 2,
                         "prompt": None if index == 0 else '{"synthetic":"preserve"}', "key": encrypted_key})
                await connection.execute(text("INSERT INTO agent_credentials(agent_id,service_name,credentials,enabled_tools) VALUES (:id,'synthetic',:credentials,'[\"read\"]')"), {"id": ids[1], "credentials": '{"fixture":true}'})
                await connection.execute(text("INSERT INTO conversation_sessions(id,agent_id,external_client_id,channel) VALUES (:sid,:id,'synthetic-retained','website')"), {"sid": uuid4(), "id": ids[0]})
        finally:
            await engine.dispose()

    async def snapshot():
        engine = create_async_engine(isolated_migration_postgres, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                agents = (await connection.execute(text("SELECT to_jsonb(a) - 'model_name' FROM agent_templates a ORDER BY id"))).scalars().all()
                credentials = (await connection.execute(text("SELECT to_jsonb(c) FROM agent_credentials c ORDER BY agent_id"))).scalars().all()
                sessions = (await connection.execute(text("SELECT to_jsonb(s) FROM conversation_sessions s ORDER BY id"))).scalars().all()
                models = (await connection.execute(text("SELECT model_name FROM agent_templates ORDER BY id"))).scalars().all()
                revision = await connection.scalar(text("SELECT version_num FROM alembic_version"))
                return (agents, credentials, sessions), models, revision
        finally:
            await engine.dispose()

    async def repeat_data_operation():
        migration = importlib.import_module("migrations.versions.luna_20261012_01_agent_models")
        engine = create_async_engine(isolated_migration_postgres, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                def run(sync):
                    with Operations.context(MigrationContext.configure(sync)):
                        migration.upgrade()
                await connection.run_sync(run)
        finally:
            await engine.dispose()

    async def invoke_from_fresh_session():
        engine = create_async_engine(isolated_migration_postgres, poolclass=NullPool)
        try:
            service = LLMService()
            service.settings = Settings(_env_file=None, llm_default_model="google/gemini-2.5-flash")
            client = ScriptedLLMClient()
            client.chat.completions.queue(build_llm_response(content="Synthetic answer"))
            monkeypatch.setattr(service, "_create_llm_client", lambda *a, **kw: client)
            monkeypatch.setattr(service.history_service, "load_history", AsyncMock(return_value=[]))
            monkeypatch.setattr(service.history_service, "save_history", AsyncMock())
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                result = await service.process_chat_message(session, ids[0], "synthetic-retained", "website", "Hello", user_id=owner)
            assert result.answer == "Synthetic answer"
            assert len(client.chat.completions.calls) == 1
            assert client.chat.completions.calls[0]["model"] == "openai/gpt-6-luna"
        finally:
            await engine.dispose()

    try:
        command.upgrade(config, PREVIOUS)
        asyncio.run(seed())
        before, legacy, _ = asyncio.run(snapshot())
        assert "unexpected/legacy-value" in legacy
        command.upgrade(config, "head")
        after, models, revision = asyncio.run(snapshot())
        assert before == after
        assert models == ["openai/gpt-6-luna"] * 3
        assert revision == REVISION
        asyncio.run(repeat_data_operation())
        assert asyncio.run(snapshot()) == (after, models, revision)
        command.check(config)
        with pytest.raises(RuntimeError, match="Cannot restore previous per-agent model names"):
            command.downgrade(config, PREVIOUS)
        assert asyncio.run(snapshot()) == (after, models, revision)
        asyncio.run(invoke_from_fresh_session())
    finally:
        _restore_env(previous_env)
        get_settings.cache_clear()
