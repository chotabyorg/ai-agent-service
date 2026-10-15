"""Handoff migration refusal and rollback preservation on PostgreSQL."""

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from core.config import get_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HANDOFF_HEAD = "handoff_20261015_01"
GOAL5_HEAD = "g5_20261015_01"


def _set_postgres_env(url: str) -> dict[str, str | None]:
    parsed = make_url(url)
    values = {
        "POSTGRES_HOST": parsed.host or "localhost",
        "POSTGRES_PORT": str(parsed.port or 5432),
        "POSTGRES_DB": parsed.database or "postgres",
        "POSTGRES_USER": parsed.username or "postgres",
        "POSTGRES_PASSWORD": parsed.password or "postgres",
    }
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    get_settings.cache_clear()
    return previous


def _restore_postgres_env(previous: dict[str, str | None]) -> None:
    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


def _config() -> Config:
    return Config(str(PROJECT_ROOT / "alembic.ini"))


def _upgrade_current_head(url: str) -> None:
    previous = _set_postgres_env(url)
    try:
        command.upgrade(_config(), "head")
    finally:
        _restore_postgres_env(previous)


@pytest.mark.integration
def test_handoff_downgrade_refuses_retained_execution_receipt_and_rolls_back(
    isolated_migration_postgres,
):
    url = isolated_migration_postgres
    _upgrade_current_head(url)
    previous = _set_postgres_env(url)
    receipt_id, integration_id, agent_id, conversation_id, turn_id, dispatch_id = (
        uuid4() for _ in range(6)
    )

    async def seed_and_verify() -> None:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(text(
                    "INSERT INTO managed_execution_receipts "
                    "(id,owner_id,integration_id,channel,agent_id,conversation_id,turn_id,"
                    "input_revision,authority_revision,input_first_sequence,input_last_sequence,"
                    "context_last_sequence,original_dispatch_id,request_fingerprint,"
                    "managed_request_fingerprint,request_payload) "
                    "VALUES (:id,'opaque-owner',:integration,'TELEGRAM',:agent,:conversation,:turn,"
                    "1,0,1,1,1,:dispatch,:fingerprint,:sidecar,CAST(:payload AS jsonb))"
                ), {
                    "id": receipt_id,
                    "integration": integration_id,
                    "agent": agent_id,
                    "conversation": conversation_id,
                    "turn": turn_id,
                    "dispatch": dispatch_id,
                    "fingerprint": "a" * 64,
                    "sidecar": "b" * 64,
                    "payload": '{"fixture":true}',
                })
                assert await connection.scalar(text(
                    "SELECT count(*) FROM managed_execution_receipts WHERE id=:id"
                ), {"id": receipt_id}) == 1
        finally:
            await engine.dispose()

    async def verify_rollback() -> None:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == HANDOFF_HEAD
                assert await connection.scalar(text(
                    "SELECT count(*) FROM managed_execution_receipts WHERE id=:id"
                ), {"id": receipt_id}) == 1
                assert await connection.scalar(text(
                    "SELECT count(*) FROM information_schema.columns WHERE table_name='agent_templates' "
                    "AND column_name='handoff_lifecycle_generation'"
                )) == 1
        finally:
            await engine.dispose()

    try:
        asyncio.run(seed_and_verify())
        with pytest.raises(RuntimeError, match="Refusing downgrade while managed v3 execution evidence exists"):
            command.downgrade(_config(), GOAL5_HEAD)
        asyncio.run(verify_rollback())
    finally:
        _restore_postgres_env(previous)


@pytest.mark.integration
def test_handoff_downgrade_refuses_lifecycle_generation_and_rolls_back(
    isolated_migration_postgres,
):
    url = isolated_migration_postgres
    _upgrade_current_head(url)
    previous = _set_postgres_env(url)
    agent_id, owner_id = uuid4(), uuid4()

    async def seed_and_verify() -> None:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(text(
                    "INSERT INTO agent_templates "
                    "(id,user_id,name,system_prompt,llm_provider,model_name,temperature,is_active,"
                    "handoff_lifecycle_generation,handoff_lifecycle_state) "
                    "VALUES (:id,:owner,'Synthetic','prompt','openai','test-model',0.5,false,2,'STABLE')"
                ), {"id": agent_id, "owner": owner_id})
                assert await connection.scalar(text(
                    "SELECT handoff_lifecycle_generation FROM agent_templates WHERE id=:id"
                ), {"id": agent_id}) == 2
        finally:
            await engine.dispose()

    async def verify_rollback() -> None:
        engine = create_async_engine(url, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                assert await connection.scalar(text("SELECT version_num FROM alembic_version")) == HANDOFF_HEAD
                assert await connection.scalar(text(
                    "SELECT handoff_lifecycle_generation FROM agent_templates WHERE id=:id"
                ), {"id": agent_id}) == 2
                assert await connection.scalar(text(
                    "SELECT count(*) FROM information_schema.columns WHERE table_name='agent_templates' "
                    "AND column_name='handoff_lifecycle_scope_snapshot'"
                )) == 1
                assert await connection.scalar(text(
                    "SELECT to_regclass('public.managed_execution_receipts')"
                )) == "managed_execution_receipts"
        finally:
            await engine.dispose()

    try:
        asyncio.run(seed_and_verify())
        with pytest.raises(RuntimeError, match="Refusing downgrade while AI Handoff lifecycle evidence exists"):
            command.downgrade(_config(), GOAL5_HEAD)
        asyncio.run(verify_rollback())
    finally:
        _restore_postgres_env(previous)
