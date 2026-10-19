"""Actual PostgreSQL lock waits before CRM reads; no provider writes."""
import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update

from models.agent import AgentTemplate
from models.business import BusinessBinding, BusinessOperation, BusinessPlan
from models.business_connection import BusinessConnection
from tests.business_effect_helpers import open_business_runtime
from tests.test_business_concurrency import held, observe_blocked
from tests.test_business_runtime import make_request
from tests.test_chotacrm_adapter import crm_settings, setup  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('entity', [AgentTemplate, BusinessBinding, BusinessConnection])
async def test_another_config_reader_does_not_block_catalog_read(db_session, crm_settings, entity):
    """Concurrent readers retain fresh authority without serializing on config."""
    runtime, _, provider, _ = await setup(db_session, crm_settings)
    predicate = entity.id == runtime.request.agent_id if entity is AgentTemplate else entity.agent_id == runtime.request.agent_id
    async with db_session() as reader:
        async with reader.begin():
            await reader.scalar(select(entity).where(predicate).with_for_update(read=True))
            result = await asyncio.wait_for(runtime.call_tool('catalog_search', {}), 2)
    assert result.halt_status is None and result.tool_payload['status'] == 'OK'
    assert provider['posts'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('entity', [AgentTemplate, BusinessBinding, BusinessConnection])
async def test_short_config_lock_waits_then_catalog_read_succeeds(db_session, crm_settings, entity):
    """An ordinary short lock on shared config must not fail an independent read."""
    runtime, _, provider, _ = await setup(db_session, crm_settings)
    predicate = entity.id == runtime.request.agent_id if entity is AgentTemplate else entity.agent_id == runtime.request.agent_id
    async with held(db_session, entity, predicate) as (blocker, pid):
        read = asyncio.create_task(runtime.call_tool('catalog_search', {}))
        try:
            await observe_blocked(db_session, pid)
        finally:
            await blocker.rollback()
            result = await asyncio.wait_for(read, 3)
    assert result.halt_status is None and result.tool_payload['status'] == 'OK'
    assert provider['posts'] == 0


@pytest.mark.asyncio
async def test_long_config_lock_fails_closed_before_provider_read(db_session, crm_settings, monkeypatch):
    """A held lock has a bounded wait and never becomes permission to call CRM."""
    runtime, _, provider, _ = await setup(db_session, crm_settings)
    reads = []
    original = runtime.registration.adapter.read

    async def observe(*args, **kwargs):
        """Record reads to prove a held writer prevents provider I/O."""
        """Record attempted CRM reads without changing their behavior."""
        reads.append(True)
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime.registration.adapter, 'read', observe)
    async with held(db_session, AgentTemplate, AgentTemplate.id == runtime.request.agent_id):
        result = await asyncio.wait_for(runtime.call_tool('catalog_search', {}), 2)
    assert result.halt_status == 'FAILED' and result.error_code == 'TOOL_SETUP_FAILED'
    assert reads == [] and provider['posts'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('entity', [AgentTemplate, BusinessBinding, BusinessConnection])
async def test_config_revocation_during_wait_prevents_provider_read(db_session, crm_settings, monkeypatch, entity):
    """Fresh authority after waiting must observe each config revocation."""
    runtime, _, provider, _ = await setup(db_session, crm_settings)
    reads = []
    refusals = []
    original = runtime.registration.adapter.read

    async def observe(*args, **kwargs):
        """Record attempted CRM reads without changing their behavior."""
        reads.append(True)
        return await original(*args, **kwargs)

    def diagnostic(**fields):
        """Inspect the raw reason before diagnostic projection groups it."""
        if fields.get('stage') == 'TOOL_GUARD':
            refusals.append(fields.get('refusal_category'))

    monkeypatch.setattr(runtime.registration.adapter, 'read', observe)
    monkeypatch.setattr(runtime, 'observe_business_diagnostic', diagnostic)
    predicate = entity.id == runtime.request.agent_id if entity is AgentTemplate else entity.agent_id == runtime.request.agent_id
    revoked = {'is_active': False} if entity is AgentTemplate else {'active': False}
    async with held(db_session, entity, predicate) as (blocker, pid):
        read = asyncio.create_task(runtime.call_tool('catalog_search', {}))
        try:
            await observe_blocked(db_session, pid)
            await blocker.execute(update(entity).where(predicate).values(**revoked))
            await blocker.commit()
        finally:
            await blocker.rollback()
            result = await asyncio.wait_for(read, 3)
    assert result.halt_status == 'FAILED' and reads == [] and provider['posts'] == 0
    assert refusals == ['AUTHORITY_DENIED' if entity is AgentTemplate else 'BINDING_UNAVAILABLE']


@pytest.mark.asyncio
async def test_config_reader_locks_are_released_before_provider_io(db_session, crm_settings, monkeypatch):
    """A provider round trip must not extend the config authority transaction."""
    runtime, _, provider, _ = await setup(db_session, crm_settings)
    original = runtime.registration.adapter.read
    reads = []

    async def observe(*args, **kwargs):
        """Acquire writer locks at the provider boundary after guards close."""
        async with db_session() as writer:
            async with writer.begin():
                for entity in (AgentTemplate, BusinessConnection, BusinessBinding):
                    predicate = entity.id == runtime.request.agent_id if entity is AgentTemplate else entity.agent_id == runtime.request.agent_id
                    await writer.scalar(select(entity).where(predicate).with_for_update(nowait=True))
        reads.append(True)
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime.registration.adapter, 'read', observe)
    result = await runtime.call_tool('catalog_search', {})
    assert result.halt_status is None and reads == [True] and provider['posts'] == 0


@pytest.mark.asyncio
async def test_five_conversations_share_agent_without_read_failures_or_operations(db_session, crm_settings):
    """Repeated simultaneous reads use fresh independent scopes and create no plans."""
    first, registry, provider, _ = await setup(db_session, crm_settings)
    runtimes = [first]
    for _ in range(4):
        request = make_request(first.request.agent_id, conversation_id=uuid4(), integration_id=first.request.integration_id)
        runtime = await open_business_runtime(request, first.grant, db_session, registry)
        await runtime.prompt_context()
        runtimes.append(runtime)
    for _ in range(3):
        results = await asyncio.wait_for(asyncio.gather(*(runtime.call_tool('catalog_search', {}) for runtime in runtimes)), 5)
        assert all(result.halt_status is None and result.tool_payload['status'] == 'OK' for result in results)
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0
    assert provider['posts'] == 0
