"""Real DB regression for committed remote grant with lost local acknowledgement."""
from types import SimpleNamespace
from uuid import uuid4
import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from business_capabilities.context import BusinessRuntimeError
from core.config import Settings
from models.agent import AgentTemplate
from models.business import IntegrationConnection, CapabilityBinding
from services.business_connection_service import BusinessConnectionService
from tests.test_business_runtime import setup_business


class GrantProvider:
    def __init__(self, owner):
        self.owner=owner
        self.workspace=uuid4()
        self.grant=uuid4()
        self.active=None
        self.attempts=[]
        self.revokes=[]
        self.lose_connect=True
        self.lose_revoke=False
        self.member=True

    async def workspaces(self, owner, cookie):
        if owner != self.owner or cookie != 'access_token=synthetic' or not self.member:
            raise BusinessRuntimeError('PERMISSION_DENIED')
        return {'items':[{'id':str(self.workspace),'name':'Synthetic salon','timezone':'Europe/Minsk'}]}

    async def configure(self, owner, agent, connection, workspace, capabilities, cookie):
        await self.workspaces(owner,cookie)
        assert workspace==self.workspace
        self.attempts.append(connection)
        if self.active and self.active != connection:
            raise BusinessRuntimeError('PROVIDER_UNAVAILABLE')
        self.active=connection
        if self.lose_connect:
            self.lose_connect=False
            raise BusinessRuntimeError('PROVIDER_UNAVAILABLE')
        return SimpleNamespace(grant_id=self.grant,revision=1,capabilities=capabilities)

    async def revoke(self, owner, agent, connection, cookie):
        if owner != self.owner or cookie != 'access_token=synthetic':
            raise BusinessRuntimeError('PERMISSION_DENIED')
        self.revokes.append(connection)
        if self.active==connection:
            self.active=None
        if self.lose_revoke:
            self.lose_revoke=False
            raise BusinessRuntimeError('PROVIDER_UNAVAILABLE')


@pytest_asyncio.fixture(loop_scope='function')
async def recovery_setup(db_session):
    agent_id,owner=uuid4(),uuid4()
    async with db_session() as db:
        db.add(AgentTemplate(id=agent_id,user_id=owner,name='Synthetic agent',system_prompt='Preserve',
            llm_provider='test',model_name='test',temperature=.1,is_active=True))
        await db.commit()
    return db_session,agent_id,owner,Settings(_env_file=None,business_capabilities_enabled=True),GrantProvider(owner)


@pytest.mark.asyncio
async def test_lost_connect_response_recovers_same_grant_after_restart(recovery_setup):
    factory,agent,owner,settings,provider=recovery_setup
    async with factory() as db:
        with pytest.raises(BusinessRuntimeError,match='PROVIDER_UNAVAILABLE'):
            await BusinessConnectionService(settings,provider).configure(db,agent,owner,'access_token=synthetic',workspace_id=provider.workspace)
        await db.rollback()
        assert await db.scalar(select(func.count()).select_from(IntegrationConnection))==0
    # A fresh session and service stand in for process restart; no in-memory intent survives.
    async with factory() as db:
        state=await BusinessConnectionService(settings,provider).configure(db,agent,owner,'access_token=synthetic',workspace_id=provider.workspace)
        assert state['connected'] and len(set(provider.attempts))==1
        assert await db.scalar(select(func.count()).select_from(IntegrationConnection))==1


@pytest.mark.asyncio
async def test_pending_cancel_revokes_committed_grant_after_lost_response(recovery_setup):
    factory,agent,owner,settings,provider=recovery_setup
    async with factory() as db:
        with pytest.raises(BusinessRuntimeError):
            await BusinessConnectionService(settings,provider).configure(db,agent,owner,'access_token=synthetic',workspace_id=provider.workspace)
        await db.rollback()
    async with factory() as db:
        await BusinessConnectionService(settings,provider).disconnect(db,agent,owner,'access_token=synthetic')
        assert provider.active is None and provider.revokes==provider.attempts
        assert await db.scalar(select(func.count()).select_from(CapabilityBinding))==0


@pytest.mark.asyncio
@pytest.mark.parametrize('commit_before_failure',[False,True])
async def test_initial_local_commit_failure_resumes_stable_identity(recovery_setup,monkeypatch,commit_before_failure):
    factory,agent,owner,settings,provider=recovery_setup
    provider.lose_connect=False
    async with factory() as db:
        original=db.commit
        commits=0
        async def failing_commit():
            nonlocal commits
            commits+=1
            if commits==2:
                if commit_before_failure:
                    await original()
                raise RuntimeError('synthetic local commit loss')
            await original()
        monkeypatch.setattr(db,'commit',failing_commit)
        with pytest.raises(RuntimeError,match='synthetic local commit loss'):
            await BusinessConnectionService(settings,provider).configure(db,agent,owner,'access_token=synthetic',workspace_id=provider.workspace)
        await db.rollback()
    async with factory() as db:
        result=await BusinessConnectionService(settings,provider).configure(db,agent,owner,'access_token=synthetic',workspace_id=provider.workspace)
        assert result['connected'] and result['pending_connection_action'] is None
        assert len(set(provider.attempts))==1
        assert await db.scalar(select(func.count()).select_from(IntegrationConnection))==1


@pytest.mark.asyncio
async def test_pending_intent_revalidates_owner_cookie_workspace_and_membership(recovery_setup):
    from business_capabilities.runtime import CapabilityRuntime
    factory,agent,owner,settings,provider=recovery_setup
    async with factory() as db:
        svc=BusinessConnectionService(settings,provider)
        with pytest.raises(BusinessRuntimeError):
            await svc.configure(db,agent,owner,'access_token=synthetic',workspace_id=provider.workspace)
        await db.rollback()
        for requested_owner,cookie,workspace,code in [
            (uuid4(),'access_token=synthetic',provider.workspace,'NOT_FOUND'),
            (owner,'expired-cookie',provider.workspace,'PERMISSION_DENIED'),
            (owner,'access_token=synthetic',uuid4(),'CONNECTION_CONFLICT')]:
            with pytest.raises(BusinessRuntimeError,match=code):
                await svc.configure(db,agent,requested_owner,cookie,workspace_id=workspace)
            await db.rollback()
        provider.member=False
        with pytest.raises(BusinessRuntimeError,match='PERMISSION_DENIED'):
            await svc.configure(db,agent,owner,'access_token=synthetic',workspace_id=provider.workspace)
        await db.rollback()
        assert len(provider.attempts)==1
        pending_state=await svc.state(db,agent,{'items':[]})
        assert pending_state['pending_connection_action']=='CONNECT' and not pending_state['connected']
        assert pending_state['workspace']['id']==str(provider.workspace)
        assert not any(c['enabled'] for c in pending_state['capabilities'])
        source=await db.get(AgentTemplate,agent)
        with pytest.raises(BusinessRuntimeError,match='CONNECTION_PENDING'):
            await CapabilityRuntime.open(db,settings,source,'client','website','trusted',owner,adapter=provider)
        await db.rollback()
        # Revocation requires current identity/ownership, not lost workspace membership.
        await svc.disconnect(db,agent,owner,'access_token=synthetic')
        assert provider.active is None


@pytest.mark.asyncio
async def test_uncertain_pending_cancel_stays_recoverable_and_never_reconnects(recovery_setup):
    factory,agent,owner,settings,provider=recovery_setup
    async with factory() as db:
        svc=BusinessConnectionService(settings,provider)
        with pytest.raises(BusinessRuntimeError):
            await svc.configure(db,agent,owner,'access_token=synthetic',workspace_id=provider.workspace)
        await db.rollback()
        provider.lose_revoke=True
        with pytest.raises(BusinessRuntimeError):
            await svc.disconnect(db,agent,owner,'access_token=synthetic')
        await db.rollback()
    async with factory() as db:
        svc=BusinessConnectionService(settings,provider)
        state=await svc.state(db,agent,{'items':[]})
        assert state['pending_connection_action']=='DISCONNECT'
        with pytest.raises(BusinessRuntimeError,match='CONNECTION_CONFLICT'):
            await svc.configure(db,agent,owner,'access_token=synthetic',workspace_id=provider.workspace)
        await db.rollback()
        await svc.disconnect(db,agent,owner,'access_token=synthetic')
        state=await svc.state(db,agent,{'items':[]})
        assert state['pending_connection_action'] is None and not state['connected']
        assert state['runtime_mode']=='CANONICAL'
        assert len(provider.revokes)==2 and len(set(provider.revokes))==1


@pytest.mark.asyncio
async def test_concurrent_connects_recover_one_grant(recovery_setup):
    factory,agent,owner,settings,provider=recovery_setup
    entered,release=asyncio.Event(),asyncio.Event()
    original=provider.configure
    first=True
    async def paused(*args):
        nonlocal first
        if first:
            first=False
            entered.set()
            await release.wait()
        return await original(*args)
    provider.configure=paused
    async def connect():
        async with factory() as db:
            try:
                return await BusinessConnectionService(settings,provider).configure(db,agent,owner,'access_token=synthetic',workspace_id=provider.workspace)
            except BusinessRuntimeError as exc:
                await db.rollback()
                return exc.code
    one=asyncio.create_task(connect())
    await entered.wait()
    two=asyncio.create_task(connect())
    await asyncio.sleep(.05)
    release.set()
    outcomes=await asyncio.gather(one,two)
    assert outcomes[0]=='PROVIDER_UNAVAILABLE' and outcomes[1]['connected']
    assert len(provider.attempts)==2 and len(set(provider.attempts))==1


@pytest.mark.asyncio
async def test_uncertain_active_disconnect_blocks_history_even_before_remote_revoke(setup_business,monkeypatch):
    from business_capabilities.runtime import CapabilityRuntime
    db,agent,settings,provider,svc=setup_business
    runtime=await CapabilityRuntime.open(db,settings,agent,'client','website','trusted',agent.user_id,adapter=provider)
    original=provider.revoke
    async def failed_before_dispatch(*args):
        raise BusinessRuntimeError('PROVIDER_UNAVAILABLE')
    monkeypatch.setattr(provider,'revoke',failed_before_dispatch)
    with pytest.raises(BusinessRuntimeError):
        await svc.disconnect(db,agent.id,agent.user_id,'access_token=synthetic')
    await db.rollback()
    assert provider.valid
    with pytest.raises(BusinessRuntimeError,match='CONNECTION_PENDING'):
        await runtime.authorize()
    state=await svc.state(db,runtime.context.agent_id,{'items':[]})
    assert state['connected'] and state['pending_connection_action']=='DISCONNECT'
    assert not any(c['enabled'] for c in state['capabilities'])
    monkeypatch.setattr(provider,'revoke',original)
    await svc.disconnect(db,runtime.context.agent_id,runtime.context.owner_id,'access_token=synthetic')
