"""Owner recovery and tenant boundaries on real PostgreSQL, mocked S2S only."""
from uuid import uuid4
from types import SimpleNamespace
import pytest
from sqlalchemy import select
from models.agent import AgentTemplate
from models.business_connection import BusinessConnection
from business_capabilities.context import BusinessRuntimeError
from business_capabilities.results import Grant
from business_capabilities.registry import DEFAULTS
from services.business_connection_service import BusinessConnectionService


@pytest.mark.parametrize('origin,allowed', [
    ('https://agents.chota.by', True), ('https://chota.by', True),
    ('https://evil.example', False), ('https://agents.chota.by.evil.example', False),
    ('null', False), ('', False),
])
def test_default_owner_mutation_origins(monkeypatch, origin, allowed):
    from fastapi import HTTPException
    from starlette.requests import Request
    from api.v1 import business_tools
    from core.config import Settings

    # Inspect the actual shipped default independently of developer .env files.
    settings = SimpleNamespace(business_config_origins=Settings.model_fields['business_config_origins'].default_factory())
    monkeypatch.setattr(business_tools, 'get_settings', lambda: settings)
    request = Request({'type': 'http', 'headers': [
        (b'cookie', b'access_token=synthetic'), (b'origin', origin.encode()),
    ]})
    if allowed:
        assert business_tools.browser(request, mutation=True) == 'access_token=synthetic'
    else:
        with pytest.raises(HTTPException) as error:
            business_tools.browser(request, mutation=True)
        assert error.value.status_code == 403


class Provider:
    def __init__(self, factory):
        self.factory = factory
        self.workspace = uuid4()
        self.grants = {}
        self.lose = False
        self.membership = True

    async def workspaces(self, owner, cookie):
        return {'items': [dict(id=str(self.workspace), name='Synthetic salon', timezone='Europe/Minsk')] if self.membership else []}

    async def configure(self, owner, agent, connection, workspace, enabled, cookie):
        # An independent NOWAIT lock proves the remote call isn't made while
        # owner configuration holds an agent/connection transaction open.
        async with self.factory() as db:
            await db.execute(select(AgentTemplate).where(AgentTemplate.id == agent).with_for_update(nowait=True))
            await db.execute(select(BusinessConnection).where(BusinessConnection.id == connection).with_for_update(nowait=True))
        grant = self.grants.get(connection)
        if grant is None or set(grant.capabilities) != set(enabled):
            grant = Grant(grant_id=grant.grant_id if grant else uuid4(), revision=grant.revision + 1 if grant else 1,
                connection_id=connection, workspace_id=workspace, agent_id=agent, capabilities=list(enabled))
            self.grants[connection] = grant
        if self.lose:
            self.lose = False
            raise BusinessRuntimeError('PROVIDER_UNAVAILABLE')
        return grant

    async def revoke(self, owner, agent, connection, cookie):
        self.grants.pop(connection, None)
        if self.lose:
            self.lose = False
            raise BusinessRuntimeError('PROVIDER_UNAVAILABLE')


async def setup(factory):
    owner, agent = uuid4(), uuid4()
    async with factory() as db:
        db.add(AgentTemplate(id=agent, user_id=owner, name='Synthetic', system_prompt='test',
            llm_provider='openai', model_name='test', temperature=0, is_active=True))
        await db.commit()
    provider = Provider(factory)
    return owner, agent, provider, BusinessConnectionService(SimpleNamespace(), adapter=provider)


@pytest.mark.asyncio
async def test_lost_connect_reply_keeps_one_remote_identity_and_retryable_intent(db_session):
    owner, agent, provider, service = await setup(db_session)
    provider.lose = True
    async with db_session() as db:
        with pytest.raises(BusinessRuntimeError):
            await service.configure(db, agent, owner, 'test', workspace_id=provider.workspace)
    async with db_session() as db:
        state = await service.state(db, agent, await provider.workspaces(owner, 'test'))
        assert not state['connected'] and state['pending_connection_action'] == 'CONNECT'
    async with db_session() as db:
        state = await service.configure(db, agent, owner, 'test', workspace_id=provider.workspace)
    assert state['connected'] and state['pending_connection_action'] is None
    assert len(provider.grants) == 1
    assert {c['id'] for c in state['capabilities'] if c['enabled']} == set(DEFAULTS)
    assert not next(c for c in state['capabilities'] if c['id'] == 'deal.create')['enabled']


@pytest.mark.asyncio
async def test_membership_loss_still_allows_repeat_safe_disconnect(db_session):
    owner, agent, provider, service = await setup(db_session)
    async with db_session() as db:
        await service.configure(db, agent, owner, 'test', workspace_id=provider.workspace)
    provider.membership, provider.lose = False, True
    async with db_session() as db:
        with pytest.raises(BusinessRuntimeError):
            await service.disconnect(db, agent, owner, 'test')
    async with db_session() as db:
        state = await service.state(db, agent, {'items': []})
        assert state['pending_connection_action'] == 'DISCONNECT'
        assert not any(c['enabled'] for c in state['capabilities'])
    async with db_session() as db:
        await service.disconnect(db, agent, owner, 'test')
    assert not provider.grants


@pytest.mark.asyncio
async def test_foreign_owner_and_second_workspace_denied(db_session):
    owner, agent, provider, service = await setup(db_session)
    async with db_session() as db:
        with pytest.raises(BusinessRuntimeError, match='NOT_FOUND'):
            await service.configure(db, agent, uuid4(), 'test', workspace_id=provider.workspace)
    assert not provider.grants
    async with db_session() as db:
        await service.configure(db, agent, owner, 'test', workspace_id=provider.workspace)
    async with db_session() as db:
        with pytest.raises(BusinessRuntimeError, match='CONNECTION_CONFLICT'):
            await service.configure(db, agent, owner, 'test', workspace_id=uuid4())


@pytest.mark.asyncio
async def test_capability_patch_increments_generation_and_rejects_unknown(db_session):
    owner, agent, provider, service = await setup(db_session)
    async with db_session() as db:
        await service.configure(db, agent, owner, 'test', workspace_id=provider.workspace)
        row = await service.connection(db, agent)
        before = row.generation
    async with db_session() as db:
        state = await service.configure(db, agent, owner, 'test', enabled=['catalog.search', 'deal.create'])
        assert {c['id'] for c in state['capabilities'] if c['enabled']} == {'catalog.search', 'deal.create'}
        assert (await service.connection(db, agent)).generation > before
    async with db_session() as db:
        with pytest.raises(BusinessRuntimeError, match='VALIDATION_ERROR'):
            await service.configure(db, agent, owner, 'test', enabled=['crm.admin'])
