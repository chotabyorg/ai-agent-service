"""Real PostgreSQL session/proposal/operation tests with deterministic provider and LLM."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import json

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy import select, func

from models.agent import AgentTemplate, AgentCredentials
from models.business import BusinessConfiguration, IntegrationConnection, CapabilityBinding, PendingBusinessAction, BusinessOperation, BusinessTurn
from core.config import Settings
from business_capabilities.registry import REGISTRY, DEFAULTS, normalize_arguments
from business_capabilities.context import BusinessRuntimeError, failure
from business_capabilities.runtime import CapabilityRuntime, digest, conversation_lock
from services.business_connection_service import BusinessConnectionService
from services.encryption_service import EncryptionService


class FakeAdapter:
    supported_capabilities = frozenset(REGISTRY)
    def __init__(self):
        self.workspace_id=uuid4()
        self.grant_id=uuid4()
        self.revision=1
        self.capabilities=list(DEFAULTS)
        self.valid=True
        self.executions=[]
        self.preparations=[]
        self.receipts={}
        self.lose_response=False
        self.receipt_available=True
        self.context_calls=0

    async def context(self, context):
        self.context_calls+=1
        if not self.valid or context.grant_revision != self.revision:
            raise BusinessRuntimeError('PERMISSION_DENIED')
        return SimpleNamespace(capabilities=self.capabilities,timezone='Europe/Minsk')

    async def workspaces(self, owner, cookie):
        assert cookie
        if not self.valid:
            raise BusinessRuntimeError('PERMISSION_DENIED')
        return {'items':[{'id':str(self.workspace_id),'name':'Synthetic Salon','timezone':'Europe/Minsk'}]}

    async def configure(self, owner, agent, connection, workspace, capabilities, cookie):
        if self.capabilities != list(capabilities):
            self.revision+=1
        self.capabilities=list(capabilities)
        return SimpleNamespace(grant_id=self.grant_id,revision=self.revision,capabilities=self.capabilities)

    async def revoke(self,*args):
        self.valid=False

    async def prepare(self, ctx, capability, arguments, turn_id):
        self.preparations.append((capability, arguments, turn_id))
        summary={'customer':'Влад','service':'Стрижка','resource':'Анна','start_at':arguments.get('start_at','2026-10-12T15:00:00Z'),
            'end_at':'2026-10-12T16:00:00Z','timezone':'Europe/Minsk','price':'40.00','currency':'BYN'}
        if capability=='deal.create':
            summary={'customer':'Влад','title':arguments['title'],'currency':'BYN','items':[{'name':'Стрижка','unit_price':'40.00','quantity':'1'}]}
        return {'status':'OK','data':{'proposal_ref':'p_'+uuid4().hex,'arguments':arguments,'payload_hash':digest(arguments),
            'summary':summary,'expires_at':(datetime.now(timezone.utc)+timedelta(minutes=10)).isoformat()}}

    async def execute(self,ctx,capability,arguments,operation=None):
        if operation:
            if operation['id'] in self.receipts:
                return self.receipts[operation['id']]
            self.executions.append((capability,arguments,operation))
        if capability=='catalog.search':
            return {'status':'OK','data':{'items':[{'ref':'o','name':'Стрижка','type':'SERVICE','price':'40.00','currency':'BYN','duration_minutes':60,'active':True}]}}
        if capability=='customer.create':
            data={'ref':'c','display_name':arguments['display_name'],'archived':False,'identities':[]}
        elif capability=='booking.create':
            data={'ref':'b','customer_ref':'c','offering_ref':'o','resource_ref':'r','start_at':arguments['start_at'],'end_at':'2026-10-12T16:00:00Z','status':'CONFIRMED','revision':'1','timezone':'Europe/Minsk'}
        else:
            data={'ref':'synthetic'}
        result={'status':'OK','data':data,'effect':'APPLIED','operation_ref':'op_'+uuid4().hex}
        if operation:
            self.receipts[operation['id']]=result
            if self.lose_response:
                return failure('UNKNOWN_WRITE_RESULT','RECONCILE')
        return result

    async def reconcile(self,ctx,capability,operation_id):
        if not self.receipt_available:
            return failure('UNKNOWN_WRITE_RESULT','RECONCILE')
        return self.receipts.get(str(operation_id),failure('UNKNOWN_WRITE_RESULT','RECONCILE'))


@pytest_asyncio.fixture(loop_scope="function")
async def setup_business(db_session):
    adapter=FakeAdapter()
    settings=Settings(_env_file=None,business_capabilities_enabled=True)
    async with db_session() as db:
        agent=AgentTemplate(id=uuid4(),user_id=uuid4(),name='Salon',system_prompt='Коротко и естественно. Собирай имя и телефон постепенно.',
            llm_provider='openrouter',model_name='test-model',temperature=0.1,is_active=True,
            encrypted_openrouter_key=EncryptionService().encrypt('synthetic-model-key'))
        db.add(agent)
        await db.commit()
        svc=BusinessConnectionService(settings,adapter)
        await svc.configure(db,agent.id,agent.user_id,'access_token=synthetic',workspace_id=adapter.workspace_id)
        yield db,agent,settings,adapter,svc


async def turn(env, message='Хочу записаться', client='client-a', event_id=None):
    db,agent,settings,adapter,_=env
    return await CapabilityRuntime.open(db,settings,agent,client,'website','website-gateway',agent.user_id,
        user_message=message,event_id=event_id,adapter=adapter)


BOOKING={'customer_ref':'c','offering_ref':'o','resource_ref':'r','start_at':'2026-10-12T15:00:00+00:00'}


@pytest.mark.asyncio
async def test_defaults_disabled_forged_authority_and_safe_write(setup_business):
    runtime=await turn(setup_business)
    assert set(runtime.enabled)==set(DEFAULTS)
    assert (await runtime.call('deal_create',{}))['error']['code']=='PERMISSION_DENIED'
    assert (await runtime.call('catalog_search',{'workspace_id':str(uuid4())}))['error']['code']=='VALIDATION_ERROR'
    result=await runtime.call('customer_create',{'display_name':'Влад','identities':[{'kind':'PHONE','value':'+375291234567'}]})
    assert result['status']=='OK'
    assert len(setup_business[3].executions)==1
    assert await setup_business[0].scalar(select(func.count()).select_from(PendingBusinessAction))==0


@pytest.mark.asyncio
@pytest.mark.parametrize('message',['+','ага','ок','👍','давайте'])
async def test_semantic_confirmation_later_turn_no_keyword_parser(setup_business,message):
    runtime=await turn(setup_business)
    proposed=await runtime.call('booking_create',BOOKING)
    ref=proposed['pending_action']['ref']
    assert (await runtime.call('business_confirm',{'pending_action_ref':ref}))['error']['code']=='CONFIRMATION_REQUIRED'
    assert setup_business[3].executions==[]
    runtime=await turn(setup_business,message)
    assert (await runtime.prompt_context()).pending_action.confirmable
    result=await runtime.call('business_confirm',{'pending_action_ref':ref})
    assert result['status']=='OK'
    assert len(setup_business[3].executions)==1
    # Repeated confirm and future turns replay the same durable effect.
    assert (await runtime.call('business_confirm',{'pending_action_ref':ref}))['status']=='OK'
    assert len(setup_business[3].executions)==1


@pytest.mark.asyncio
async def test_changed_time_replaces_old_and_cannot_mutate_confirmation(setup_business):
    first=await turn(setup_business)
    old=(await first.call('booking_create',BOOKING))['pending_action']['ref']
    second=await turn(setup_business,'Да, только лучше в 19')
    changed={**BOOKING,'start_at':'2026-10-12T16:00:00+00:00'}
    new=(await second.call('booking_create',changed))['pending_action']['ref']
    assert new!=old
    assert (await second.call('business_confirm',{'pending_action_ref':old}))['error']['code']=='STALE_PROPOSAL'
    assert (await second.call('business_confirm',{'pending_action_ref':new,'arguments':changed}))['error']['code']=='VALIDATION_ERROR'
    assert (await second.call('business_confirm',{'pending_action_ref':new}))['error']['code']=='CONFIRMATION_REQUIRED'
    assert not setup_business[3].executions


@pytest.mark.asyncio
async def test_decline_unrelated_expiry_cross_session(setup_business):
    r=await turn(setup_business)
    ref=(await r.call('booking_create',BOOKING))['pending_action']['ref']
    unrelated=await turn(setup_business,'А где парковка?')
    assert not setup_business[3].executions
    other=await turn(setup_business,'+',client='different-client')
    assert (await other.call('business_confirm',{'pending_action_ref':ref}))['error']['code']=='NOT_FOUND'
    declining=await turn(setup_business,'нет')
    assert (await declining.call('business_cancel',{'pending_action_ref':ref}))['status']=='OK'
    assert not setup_business[3].executions
    ref=(await declining.call('booking_create',BOOKING))['pending_action']['ref']
    row=await setup_business[0].get(PendingBusinessAction, __import__('uuid').UUID(ref))
    row.expires_at=datetime.now(timezone.utc)-timedelta(seconds=1)
    await setup_business[0].commit()
    expired=await turn(setup_business,'давайте')
    assert (await expired.call('business_confirm',{'pending_action_ref':ref}))['error']['code']=='STALE_PROPOSAL'


@pytest.mark.asyncio
async def test_unknown_blocks_changed_write_and_reconciles_without_retry(setup_business):
    r=await turn(setup_business)
    adapter=setup_business[3]
    adapter.lose_response=True
    adapter.receipt_available=False
    args={'display_name':'Влад','identities':[{'kind':'PHONE','value':'+375291234567'}]}
    assert (await r.call('customer_create',args))['error']['code']=='UNKNOWN_WRITE_RESULT'
    r=await turn(setup_business,'Повтори')
    assert (await r.call('customer_create',{**args,'display_name':'Владислав'}))['error']['code']=='UNKNOWN_WRITE_RESULT'
    assert len(adapter.executions)==1
    with pytest.raises(BusinessRuntimeError,match='UNKNOWN_WRITE_RESULT'):
        await setup_business[4].disconnect(setup_business[0],setup_business[1].id,setup_business[1].user_id,'access_token=synthetic')
    await setup_business[0].rollback()
    adapter.receipt_available=True
    assert (await r.call('customer_create',args))['status']=='OK'
    assert len(adapter.executions)==1


@pytest.mark.asyncio
async def test_generation_revocation_namespace_and_event_dedup(setup_business):
    db,agent,settings,adapter,svc=setup_business
    r=await turn(setup_business,event_id='event-1')
    await r.finish('Привет')
    replay=await turn(setup_business,event_id='event-1')
    assert replay.turn.id==r.turn.id and replay.turn.answer=='Привет'
    with pytest.raises(BusinessRuntimeError,match='IDEMPOTENCY_CONFLICT'):
        await turn(setup_business,'changed',event_id='event-1')
    await db.rollback()
    await db.refresh(agent)
    other=await CapabilityRuntime.open(db,settings,agent,'client-a','website','another-ingress',agent.user_id,adapter=adapter)
    assert other.context.session_id != r.context.session_id
    await svc.configure(db,agent.id,agent.user_id,'access_token=synthetic',enabled=[])
    with pytest.raises(BusinessRuntimeError,match='STALE_BINDING'):
        await r.authorize()
    newer=await turn(setup_business)
    assert newer.context.session_id != r.context.session_id and newer.enabled==frozenset()
    adapter.valid=False
    with pytest.raises(BusinessRuntimeError,match='PERMISSION_DENIED'):
        await newer.authorize()


@pytest.mark.asyncio
async def test_booking_and_deal_share_one_business_confirmation(setup_business):
    db,agent,_,adapter,svc=setup_business
    await svc.configure(db,agent.id,agent.user_id,'access_token=synthetic',enabled=list(DEFAULTS)+['deal.create'])
    r=await turn(setup_business)
    proposal=await r.call('business_propose',{'actions':[{'capability':'booking.create','arguments':BOOKING},
        {'capability':'deal.create','arguments':{'customer_ref':'c','title':'Стрижка','currency':'BYN','items':[{'offering_ref':'o','quantity':'1'}]}}]})
    assert not adapter.executions
    r=await turn(setup_business,'👍')
    result=await r.call('business_confirm',{'pending_action_ref':proposal['pending_action']['ref']})
    assert result['status']=='OK' and len(adapter.executions)==2
    assert {entry[2]['confirmation']['confirmed_turn_id'] for entry in adapter.executions}=={str(r.turn.id)}


@pytest.mark.asyncio
async def test_connection_singleton_empty_toggles_disconnect_not_legacy(setup_business):
    db,agent,_,adapter,svc=setup_business
    workspaces=await adapter.workspaces(agent.user_id,'cookie')
    state=await svc.state(db,agent.id,workspaces)
    assert state['runtime_mode']=='CANONICAL' and state['connected']
    await svc.configure(db,agent.id,agent.user_id,'cookie',enabled=[])
    assert not any(c['enabled'] for c in (await svc.state(db,agent.id,workspaces))['capabilities'])
    await svc.configure(db,agent.id,agent.user_id,'cookie',workspace_id=adapter.workspace_id)
    assert await db.scalar(select(func.count()).select_from(IntegrationConnection).where(IntegrationConnection.active.is_(True)))==1
    await svc.disconnect(db,agent.id,agent.user_id,'cookie')
    state=await svc.state(db,agent.id,workspaces)
    assert state['runtime_mode']=='CANONICAL' and not state['connected']


@pytest.mark.asyncio
async def test_advisory_session_lock_blocks_overlapping_chat(db_session,setup_business):
    runtime=await turn(setup_business)
    async with conversation_lock(setup_business[0],runtime.context.session_id):
        async with db_session() as second:
            with pytest.raises(BusinessRuntimeError,match='OPERATION_IN_PROGRESS'):
                async with conversation_lock(second,runtime.context.session_id):
                    pytest.fail('overlapping conversation')


@pytest.mark.asyncio
async def test_distinct_intent_allows_later_identical_safe_write(setup_business):
    args={'customer_ref':'c','title':'Позвонить клиенту'}
    r=await turn(setup_business,'Перезвоните')
    assert (await r.call('followup_create',args))['status']=='OK'
    assert (await r.call('followup_create',args))['status']=='OK'
    assert len(setup_business[3].executions)==1
    later=await turn(setup_business,'Напомните ещё раз через день')
    assert (await later.call('followup_create',args))['status']=='OK'
    assert len(setup_business[3].executions)==2


@pytest.mark.asyncio
async def test_connection_provenance_foreign_owner_rejected_by_database(db_session,setup_business):
    from sqlalchemy.exc import IntegrityError
    db,agent,_,adapter,svc=setup_business
    async with db_session() as other:
        other.add(IntegrationConnection(id=uuid4(),agent_id=agent.id,owner_id=uuid4(),provider='chotacrm',workspace_id=adapter.workspace_id,
            workspace_name='x',timezone='UTC',grant_id=uuid4(),grant_revision=1,active=False))
        with pytest.raises(IntegrityError):
            await other.commit()


@pytest.mark.asyncio
async def test_owner_api_cookie_origin_membership_and_state(api_client,setup_business,monkeypatch):
    import api.v1.business_tools as api
    db,agent,settings,adapter,svc=setup_business
    monkeypatch.setattr(api,'service',lambda:svc)
    monkeypatch.setattr(api,'get_settings',lambda:settings)
    url=f'/v1/agents/{agent.id}/business-tools'
    owner={'X-User-Id':str(agent.user_id)}
    assert (await api_client.get(url,headers=owner)).status_code==401
    cookie={'Cookie':'access_token=synthetic',**owner}
    state=await api_client.get(url,headers=cookie)
    assert state.status_code==200 and state.json()['runtime_mode']=='CANONICAL'
    assert (await api_client.patch(url,headers=cookie,json={'enabled_capabilities':[]})).status_code==403
    valid={**cookie,'Origin':'https://agents.chota.by'}
    assert (await api_client.patch(url,headers=valid,json={'enabled_capabilities':[]})).status_code==200
    assert (await api_client.patch(url,headers=valid,json={'enabled_capabilities':['unknown']})).status_code==422
    assert (await api_client.put(url,headers=valid,json={'workspace_id':str(uuid4())})).status_code==409
    assert (await api_client.get(url,headers={**cookie,'X-User-Id':str(uuid4())})).status_code==404
    adapter.valid=False
    assert (await api_client.get(url,headers=cookie)).status_code==403


@pytest.mark.asyncio
@pytest.mark.parametrize('consent',['+','ага','ок','👍','давайте'])
async def test_fake_llm_booking_dialog_real_runtime(api_client,setup_business,monkeypatch,consent):
    import services.llm_service as llm_module
    import business_capabilities.runtime as runtime_module
    import api.v1.chat as chat
    from tests.fakes import ScriptedLLMClient,build_llm_response,build_tool_call
    db,agent,settings,adapter,_=setup_business
    settings.chat_s2s_token=SecretStr('synthetic-chat-service')
    monkeypatch.setattr(chat,'get_settings',lambda:settings)
    monkeypatch.setattr(chat.llm_service,'settings',settings)
    monkeypatch.setattr(runtime_module,'ChotaCRMAdapter',lambda _:adapter)
    model=ScriptedLLMClient()
    monkeypatch.setattr(llm_module,'AsyncOpenAI',lambda **kw:model)
    model.chat.completions.queue(build_llm_response(tool_calls=[build_tool_call('catalog','catalog_search','{}')]),
        build_llm_response(tool_calls=[build_tool_call('propose','booking_create',json.dumps(BOOKING))]),
        build_llm_response(content='Стрижка у Анны 12 октября в 18:00, 40 BYN. Всё верно?'))
    headers={'X-Service-Token':'synthetic-chat-service','X-User-Id':str(agent.user_id)}
    payload={'agent_id':str(agent.id),'external_client_id':'salon-client','channel':'website','message':'Влад, +375291234567. Стрижка завтра в 18','event_id':'first'}
    first=await api_client.post('/v1/chat/completions',headers=headers,json=payload)
    assert first.status_code==200,first.text
    assert not adapter.executions
    row=await db.scalar(select(PendingBusinessAction))
    model.chat.completions.queue(build_llm_response(tool_calls=[build_tool_call('confirm','business_confirm',json.dumps({'pending_action_ref':str(row.id)}))]),
        build_llm_response(content='Вы записаны на стрижку к Анне на 18:00 😊'))
    second=await api_client.post('/v1/chat/completions',headers=headers,json={**payload,'message':consent,'event_id':'second'})
    assert second.status_code==200,second.text
    assert len(adapter.executions)==1
    assert 'записаны' in second.json()['answer']
    assert any('CONFIRMATION_REQUIRED' in str(call['messages']) for call in model.chat.completions.calls)
    assert all('<platform_policy>' in call['messages'][0]['content'] for call in model.chat.completions.calls)
    assert adapter.context_calls>=len(model.chat.completions.calls)
    replay=await api_client.post('/v1/chat/completions',headers=headers,json={**payload,'message':consent,'event_id':'second'})
    assert replay.json()==second.json() and len(adapter.executions)==1
    # No model call, history load or cached answer bypass after membership loss.
    count=len(model.chat.completions.calls)
    adapter.valid=False
    denied=await api_client.post('/v1/chat/completions',headers=headers,json={**payload,'message':'Повтори','event_id':'third'})
    assert denied.status_code==403 and len(model.chat.completions.calls)==count


@pytest.mark.asyncio
async def test_canonical_requires_authenticated_caller_even_compat_mode(api_client,setup_business,monkeypatch):
    import api.v1.chat as chat
    db,agent,settings,_,_=setup_business
    monkeypatch.setattr(chat,'get_settings',lambda:settings)
    monkeypatch.setattr(chat.llm_service,'settings',settings)
    response=await api_client.post('/v1/chat/completions',headers={'X-User-Id':str(agent.user_id)},json={
        'agent_id':str(agent.id),'external_client_id':'forged','channel':'telegram','message':'hi'})
    assert response.status_code==403


@pytest.mark.asyncio
async def test_canonical_skips_bitrix_preserves_google(api_client,setup_business,monkeypatch):
    import api.v1.chat as chat
    import business_capabilities.runtime as runtime_module
    import services.llm_service as llm_module
    from tests.fakes import FakeMCPController,ScriptedLLMClient,build_llm_response
    db,agent,settings,adapter,_=setup_business
    settings.chat_s2s_token=SecretStr('synthetic')
    monkeypatch.setattr(chat,'get_settings',lambda:settings)
    monkeypatch.setattr(chat.llm_service,'settings',settings)
    monkeypatch.setattr(runtime_module,'ChotaCRMAdapter',lambda _:adapter)
    db.add_all([AgentCredentials(agent_id=agent.id,service_name='bitrix',credentials={'webhook_url':'synthetic'}),
                AgentCredentials(agent_id=agent.id,service_name='google_workspace',credentials={'client_id':'synthetic'})])
    await db.commit()
    model=ScriptedLLMClient()
    model.chat.completions.queue(build_llm_response(content='Здравствуйте!'))
    monkeypatch.setattr(llm_module,'AsyncOpenAI',lambda **kw:model)
    controller=FakeMCPController()
    monkeypatch.setattr(llm_module,'MCPManager',controller.make_manager_class())
    response=await api_client.post('/v1/chat/completions',headers={'X-Service-Token':'synthetic','X-User-Id':str(agent.user_id)},json={
        'agent_id':str(agent.id),'external_client_id':'new','channel':'website','message':'Привет'})
    assert response.status_code==200,response.text
    assert [call['service_name'] for call in controller.init_calls]==['google_workspace']


@pytest.mark.asyncio
async def test_dispatch_configuration_race_is_frozen_until_known_result(db_session,setup_business):
    import asyncio
    db,agent,settings,adapter,svc=setup_business
    r=await turn(setup_business)
    entered,release=asyncio.Event(),asyncio.Event()
    original=adapter.execute
    async def paused(ctx,cap,args,operation=None):
        entered.set()
        await release.wait()
        return await original(ctx,cap,args,operation)
    adapter.execute=paused
    adapter.lose_response=True
    adapter.receipt_available=False
    writing=asyncio.create_task(r.call('customer_create',{'display_name':'Влад','identities':[{'kind':'PHONE','value':'+375291234567'}]}))
    await entered.wait()
    async with db_session() as config_db:
        with pytest.raises(BusinessRuntimeError,match='UNKNOWN_WRITE_RESULT'):
            await svc.configure(config_db,agent.id,agent.user_id,'cookie',enabled=[])
        await config_db.rollback()
        with pytest.raises(BusinessRuntimeError,match='UNKNOWN_WRITE_RESULT'):
            await svc.disconnect(config_db,agent.id,agent.user_id,'cookie')
    release.set()
    assert (await writing)['error']['code']=='UNKNOWN_WRITE_RESULT'
    assert adapter.revision==1 and len(adapter.executions)==1


@pytest.mark.asyncio
async def test_sensitive_retention_keeps_expired_replay_tombstones(setup_business):
    from business_capabilities.maintenance import prune_business_data
    db,agent,settings,adapter,svc=setup_business
    r=await turn(setup_business,event_id='stable-delivery')
    await r.call('customer_create',{'display_name':'Влад','identities':[{'kind':'PHONE','value':'+375291234567'}]})
    await r.finish('Ваши данные сохранены')
    op=await db.scalar(select(BusinessOperation))
    old=datetime.now(timezone.utc)-timedelta(days=31)
    op.issued_at=old
    r.turn.created_at=old
    await db.commit()
    await prune_business_data(db)
    await db.refresh(op)
    assert op.state=='EXPIRED' and op.arguments=={} and op.result is None
    await db.refresh(r.turn)
    assert r.turn.answer is None
    with pytest.raises(BusinessRuntimeError,match='OPERATION_EXPIRED'):
        await turn(setup_business,event_id='stable-delivery')


@pytest.mark.asyncio
async def test_next_turn_recovers_unknown_without_model_retry_or_second_consent(setup_business):
    r=await turn(setup_business)
    ref=(await r.call('booking_create',BOOKING))['pending_action']['ref']
    r=await turn(setup_business,'+')
    setup_business[3].lose_response=True
    assert (await r.call('business_confirm',{'pending_action_ref':ref}))['error']['code']=='UNKNOWN_WRITE_RESULT'
    status_turn=await turn(setup_business,'Получилось записаться?')
    prompt=await status_turn.prompt_context()
    assert prompt.pending_action is None
    assert len(prompt.recovered_outcomes)==1
    assert 'CONFIRMED' in prompt.recovered_outcomes[0].result
    assert len(setup_business[3].executions)==1
