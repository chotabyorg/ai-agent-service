"""Independent scopes share config reads; scope and effect exclusion remain."""
import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from models.agent import AgentTemplate
from models.business import BusinessBinding, BusinessOperation, BusinessPlan
from models.business_connection import BusinessConnection
from tests.business_effect_helpers import open_business_runtime
from tests.test_business_runtime import later, make_request
from tests.test_chotacrm_adapter import crm_settings, setup  # noqa: F401


async def independent(first, registry, factory):
    """Open another real ledger scope under the same agent configuration."""
    request=make_request(first.request.agent_id, conversation_id=uuid4(),
                         integration_id=first.request.integration_id)
    runtime=await open_business_runtime(request,first.grant,factory,registry)
    await runtime.prompt_context()
    return runtime


@pytest.mark.asyncio
@pytest.mark.parametrize('concurrent_open', [False, True])
async def test_five_proposals_and_consents_have_one_effect_each(db_session, crm_settings, concurrent_open):
    """Exercise admission, consent, permit, dispatch and replay across five scopes."""
    first,registry,provider,arguments=await setup(db_session,crm_settings)
    transport=first.registration.adapter.client.transport

    async def unique_receipt(request):
        """Return distinct synthetic receipts while retaining original validation."""
        response=await transport.handle_async_request(request)
        if request.url.path.endswith('/execute/booking.create'):
            body=json.loads(request.content)
            value=json.loads(response.content)
            value['operation_ref']='op_'+body['operation']['id']
            value['data']['ref']='booking_'+body['operation']['id']
            provider['receipts'][body['operation']['id']]=value
            return httpx.Response(200,json=value)
        return response

    first.registration.adapter.client.transport=httpx.MockTransport(unique_receipt)
    async def open_and_propose():
        """Include new-turn setup overlap when requested by this test case."""
        runtime=await independent(first,registry,db_session)
        return runtime,await runtime.call_tool('booking_create',arguments)

    if concurrent_open:
        pairs=await asyncio.wait_for(asyncio.gather(*(open_and_propose() for _ in range(5))),15)
    else:
        runtimes=[first]
        for _ in range(4):runtimes.append(await independent(first,registry,db_session))
        proposals=await asyncio.wait_for(asyncio.gather(*(r.call_tool('booking_create',arguments) for r in runtimes)),15)
        pairs=list(zip(runtimes,proposals,strict=True))
    assert all(p.halt_status is None and p.tool_payload.get('proposal_id') for _,p in pairs)
    assert provider['posts']==0
    async with db_session() as db:
        operations=(await db.scalars(select(BusinessOperation))).all()
        assert len(operations)==5 and all(o.status=='RESERVED' and o.attempt_count==0 for o in operations)
    confirmations=[]
    for runtime,proposal in pairs:
        confirming=await open_business_runtime(later(runtime.request,'да'),runtime.grant,db_session,registry)
        context=await confirming.prompt_context()
        assert context['pending']['proposal_id']==proposal.tool_payload['proposal_id']
        confirmations.append((confirming,dict(proposal_id=proposal.tool_payload['proposal_id'],intent='CONFIRM')))
    results=await asyncio.wait_for(asyncio.gather(*(r.call_tool('business_decide_proposal',a) for r,a in confirmations)),15)
    assert all(r.halt_status is None for r in results) and provider['posts']==5
    for runtime,arguments in confirmations:
        replay=await runtime.call_tool('business_decide_proposal',arguments)
        assert replay.halt_status is None
    async with db_session() as db:
        operations=(await db.scalars(select(BusinessOperation))).all()
        assert len(operations)==5 and all(o.status=='SUCCEEDED' and o.attempt_count==1 for o in operations)
        assert len({o.object_id for o in operations})==5
    assert provider['posts']==5


@pytest.mark.asyncio
@pytest.mark.parametrize('entity',[AgentTemplate,BusinessConnection,BusinessBinding])
async def test_admission_shared_config_still_excludes_config_writers(db_session, crm_settings, monkeypatch, entity):
    """A decisive admission excludes configuration mutation until commit."""
    runtime,_,provider,arguments=await setup(db_session,crm_settings)
    acquired,release=asyncio.Event(),asyncio.Event()
    original=runtime.repository._lock_scope

    async def gate(session,scope_id):
        """Pause only after decisive authority has all config and scope locks."""
        scope=await original(session,scope_id)
        if session.info.get('business_nowait'):
            acquired.set()
            await release.wait()
        return scope

    monkeypatch.setattr(runtime.repository,'_lock_scope',gate)
    task=asyncio.create_task(runtime.call_tool('booking_create',arguments))
    try:
        await asyncio.wait_for(acquired.wait(),5)
        async with db_session() as writer:
            with pytest.raises(DBAPIError) as raised:
                predicate=entity.id==runtime.request.agent_id if entity is AgentTemplate else entity.agent_id==runtime.request.agent_id
                await writer.scalar(select(entity).where(predicate).with_for_update(nowait=True))
            assert runtime.repository._contention(raised.value)
            await writer.rollback()
    finally:
        release.set()
        result=await asyncio.wait_for(task,5)
    assert result.halt_status is None and provider['posts']==0


@pytest.mark.asyncio
async def test_revocation_between_probe_and_decisive_admission_still_denies(db_session, crm_settings, monkeypatch):
    """Compatible config locks never substitute for a fresh authority check."""
    runtime,_,provider,arguments=await setup(db_session,crm_settings)
    original=runtime.repository._reserve_plan

    async def revoke(*args,**kwargs):
        """Revoke after preparation and probe, before final admission authority."""
        async with db_session() as db:
            await db.execute(update(AgentTemplate).where(AgentTemplate.id==runtime.request.agent_id).values(is_active=False))
            await db.commit()
        return await original(*args,**kwargs)

    monkeypatch.setattr(runtime.repository,'_reserve_plan',revoke)
    result=await runtime.call_tool('booking_create',arguments)
    assert result.halt_status=='FAILED' and provider['posts']==0
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan))==0
