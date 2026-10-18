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
        reads.append(True)
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime.registration.adapter, 'read', observe)
    async with held(db_session, AgentTemplate, AgentTemplate.id == runtime.request.agent_id):
        result = await asyncio.wait_for(runtime.call_tool('catalog_search', {}), 2)
    assert result.halt_status == 'FAILED' and result.error_code == 'TOOL_SETUP_FAILED'
    assert reads == [] and provider['posts'] == 0


@pytest.mark.asyncio
async def test_agent_revocation_during_wait_prevents_provider_read(db_session, crm_settings, monkeypatch):
    """Fresh authority after the wait must observe an agent revocation."""
    runtime, _, provider, _ = await setup(db_session, crm_settings)
    reads = []
    original = runtime.registration.adapter.read

    async def observe(*args, **kwargs):
        reads.append(True)
        return await original(*args, **kwargs)

    monkeypatch.setattr(runtime.registration.adapter, 'read', observe)
    async with held(db_session, AgentTemplate, AgentTemplate.id == runtime.request.agent_id) as (blocker, pid):
        read = asyncio.create_task(runtime.call_tool('catalog_search', {}))
        try:
            await observe_blocked(db_session, pid)
            await blocker.execute(update(AgentTemplate).where(AgentTemplate.id == runtime.request.agent_id).values(is_active=False))
            await blocker.commit()
        finally:
            await blocker.rollback()
            result = await asyncio.wait_for(read, 3)
    assert result.halt_status == 'FAILED' and reads == [] and provider['posts'] == 0


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
