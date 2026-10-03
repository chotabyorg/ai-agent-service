"""Consumer security-label tests; actual identity evidence rules live in CRM tests."""
import json
from uuid import UUID
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import select,func

from business_capabilities.context import BusinessRuntimeError,failure
from business_capabilities.runtime import CapabilityRuntime
from models.business import BusinessConversation,PendingBusinessAction,BusinessOperation
from tests.test_business_runtime import setup_business,turn,BOOKING
from tests.fakes import ScriptedLLMClient,build_llm_response


def install_stale_context(provider, stale):
    original_context=provider.context
    original_reconcile=provider.reconcile
    async def context(ctx):
        if ctx.session_id in stale:
            raise BusinessRuntimeError('STALE_CUSTOMER_CONTEXT')
        return await original_context(ctx)
    async def reconcile(ctx,capability,operation_id):
        if ctx.session_id in stale:
            return failure('STALE_CUSTOMER_CONTEXT')
        return await original_reconcile(ctx,capability,operation_id)
    provider.context=context
    provider.reconcile=reconcile


@pytest.mark.asyncio
@pytest.mark.parametrize('provider_reason',['phone_collision','email_collision','identity_deleted','identity_reassigned','customer_archived','evidence_expired'])
async def test_invalid_customer_context_never_replays_answer_or_loads_history(api_client,setup_business,monkeypatch,provider_reason):
    import api.v1.chat as chat
    import business_capabilities.runtime as runtime_module
    import services.llm_service as llm_module
    db,agent,settings,provider,svc=setup_business
    settings.chat_s2s_token=SecretStr('synthetic-chat')
    monkeypatch.setattr(chat,'get_settings',lambda:settings)
    monkeypatch.setattr(chat.llm_service,'settings',settings)
    monkeypatch.setattr(runtime_module,'ChotaCRMAdapter',lambda _:provider)
    model=ScriptedLLMClient()
    monkeypatch.setattr(llm_module,'AsyncOpenAI',lambda **kw:model)
    model.chat.completions.queue(build_llm_response(content='PRIVATE_APPOINTMENT_AT_18'))
    headers={'X-Service-Token':'synthetic-chat','X-User-Id':str(agent.user_id)}
    payload={'agent_id':str(agent.id),'external_client_id':'returning-customer','channel':'website','message':'Когда моя запись?',
        'event_id':'appointment-answer'}
    first=await api_client.post('/v1/chat/completions',headers=headers,json=payload)
    assert first.status_code==200
    old_session=UUID(first.json()['session_id'])
    # Provider emits the same closed signal for each reason, independently tested
    # against real customer/identity rows in the CRM repository (not mocked proof here).
    stale={old_session}
    install_stale_context(provider,stale)
    replay=await api_client.post('/v1/chat/completions',headers=headers,json=payload)
    assert replay.status_code==409 and replay.json()['detail']['code']=='STALE_CUSTOMER_CONTEXT'
    assert 'PRIVATE_APPOINTMENT' not in replay.text
    assert len(model.chat.completions.calls)==1
    old=await db.get(BusinessConversation,old_session,populate_existing=True)
    assert old.retired
    model.chat.completions.queue(build_llm_response(content='Подскажите актуальный номер телефона, пожалуйста.'))
    fresh=await api_client.post('/v1/chat/completions',headers=headers,json={**payload,'message':'Повтори время','event_id':'fresh-request'})
    assert fresh.status_code==200 and fresh.json()['session_id']!=str(old_session)
    current=await db.get(BusinessConversation,UUID(fresh.json()['session_id']))
    assert current.lineage_id==old.lineage_id and current.generation==old.generation
    assert 'PRIVATE_APPOINTMENT' not in json.dumps(model.chat.completions.calls[-1]['messages'])
    assert len(model.chat.completions.calls[-1]['messages'])==2
    # Restoring evidence cannot revive the old session/cache.
    stale.clear()
    replay_again=await api_client.post('/v1/chat/completions',headers=headers,json=payload)
    assert replay_again.status_code==409 and len(model.chat.completions.calls)==2


@pytest.mark.asyncio
async def test_pending_summary_retired_and_fresh_valid_lookup_still_works(setup_business):
    db,agent,settings,provider,svc=setup_business
    old=await turn(setup_business)
    ref=(await old.call('booking_create',BOOKING))['pending_action']['ref']
    install_stale_context(provider,{old.context.session_id})
    fresh=await turn(setup_business,'Мой новый телефон +375291111111')
    assert fresh.context.session_id!=old.context.session_id
    assert (await fresh.prompt_context()).pending_action is None
    pending=await db.get(PendingBusinessAction,UUID(ref),populate_existing=True)
    assert pending.state=='CANCELLED'
    assert (await fresh.call('business_confirm',{'pending_action_ref':ref}))['error']['code']=='NOT_FOUND'
    with pytest.raises(BusinessRuntimeError,match='STALE_CUSTOMER_CONTEXT'):
        await old.authorize()
    original=provider.execute
    async def execute(ctx,capability,args,operation=None):
        if capability=='customer.find':
            return {'status':'OK','data':{'match':'SINGLE','candidates':[{'ref':'fresh-customer','display_name':'Влад','archived':False,'identities':[]}]}}
        return await original(ctx,capability,args,operation)
    provider.execute=execute
    lookup=await fresh.call('customer_find',{'identity':{'kind':'PHONE','value':'+375291111111'}})
    assert lookup['status']=='OK' and lookup['data']['match']=='SINGLE'
    assert provider.executions==[]


@pytest.mark.asyncio
async def test_unknown_predecessor_blocks_fresh_write_without_exposing_recovered_facts(setup_business):
    db,agent,settings,provider,svc=setup_business
    old=await turn(setup_business)
    ref=(await old.call('booking_create',BOOKING))['pending_action']['ref']
    old=await turn(setup_business,'+')
    provider.lose_response=True
    result=await old.call('business_confirm',{'pending_action_ref':ref})
    assert result['error']['code']=='UNKNOWN_WRITE_RESULT'
    assert len(provider.executions)==1
    install_stale_context(provider,{old.context.session_id})
    fresh=await turn(setup_business,'Хочу записаться заново')
    context=await fresh.prompt_context()
    assert context.pending_action is None and context.recovered_outcomes==()
    # Both unchanged and changed proposals are blocked by predecessor uncertainty.
    for arguments in [BOOKING,{**BOOKING,'start_at':'2026-10-12T16:00:00Z'}]:
        result=await fresh.call('booking_create',arguments)
        assert result['error']['code']=='UNKNOWN_WRITE_RESULT'
    result=await fresh.call('customer_create',{'display_name':'Влад','identities':[{'kind':'PHONE','value':'+375291111111'}]})
    assert result['error']['code']=='UNKNOWN_WRITE_RESULT'
    assert len(provider.executions)==1
    assert (await fresh.call('catalog_search',{}))['status']=='OK'
    assert await db.scalar(select(func.count()).select_from(BusinessOperation))==1


@pytest.mark.asyncio
async def test_restored_evidence_recovers_only_old_receipt_and_never_old_history(setup_business):
    db,agent,settings,provider,svc=setup_business
    old=await turn(setup_business)
    ref=(await old.call('booking_create',BOOKING))['pending_action']['ref']
    old=await turn(setup_business,'+')
    provider.lose_response=True
    assert (await old.call('business_confirm',{'pending_action_ref':ref}))['error']['code']=='UNKNOWN_WRITE_RESULT'
    stale={old.context.session_id}
    install_stale_context(provider,stale)
    fresh=await turn(setup_business,'Попробовать снова')
    assert (await fresh.call('booking_create',BOOKING))['error']['code']=='UNKNOWN_WRITE_RESULT'
    # Original exact evidence is restored. Only receipt access may recover;
    # local retirement still rejects all reuse of the old conversation context.
    stale.clear()
    provider.lose_response=False
    assert (await fresh.call('booking_create',BOOKING))['error']['code']=='PREVIOUS_ACTION_COMPLETED'
    assert len(provider.executions)==1
    operation=await db.scalar(select(BusinessOperation))
    assert operation.state=='COMPLETED'
    assert (await db.get(PendingBusinessAction,UUID(ref),populate_existing=True)).state=='COMPLETED'
    context=await fresh.prompt_context()
    assert context.pending_action is None and context.recovered_outcomes==()
    with pytest.raises(BusinessRuntimeError,match='STALE_CUSTOMER_CONTEXT'):
        await old.authorize()
    # A separate later request may start a new business intent after settlement.
    later=await turn(setup_business,'Нужна другая запись')
    assert (await later.call('booking_create',{**BOOKING,'start_at':'2026-10-15T17:00:00Z'}))['error']['code']=='CONFIRMATION_REQUIRED'
    assert len(provider.executions)==1


@pytest.mark.asyncio
async def test_recovered_summary_rechecks_context_before_model_exposure(setup_business):
    db,agent,settings,provider,svc=setup_business
    old=await turn(setup_business)
    ref=(await old.call('booking_create',BOOKING))['pending_action']['ref']
    old=await turn(setup_business,'+')
    provider.lose_response=True
    assert (await old.call('business_confirm',{'pending_action_ref':ref}))['error']['code']=='UNKNOWN_WRITE_RESULT'
    current=await turn(setup_business,'Получилось?')
    original=provider.reconcile
    async def reconcile(ctx,capability,operation):
        result=await original(ctx,capability,operation)
        async def invalidated(ctx):
            raise BusinessRuntimeError('STALE_CUSTOMER_CONTEXT')
        provider.context=invalidated
        return result
    provider.reconcile=reconcile
    with pytest.raises(BusinessRuntimeError,match='STALE_CUSTOMER_CONTEXT'):
        await current.prompt_context()


@pytest.mark.asyncio
async def test_context_invalidation_after_model_before_final_response_is_blocked(api_client,setup_business,monkeypatch):
    import api.v1.chat as chat
    import business_capabilities.runtime as runtime_module
    import services.llm_service as llm_module
    db,agent,settings,provider,_=setup_business
    settings.chat_s2s_token=SecretStr('synthetic-chat')
    monkeypatch.setattr(chat,'get_settings',lambda:settings)
    monkeypatch.setattr(chat.llm_service,'settings',settings)
    monkeypatch.setattr(runtime_module,'ChotaCRMAdapter',lambda _:provider)
    async def completion(**kwargs):
        async def revoked(ctx):
            raise BusinessRuntimeError('STALE_CUSTOMER_CONTEXT')
        provider.context=revoked
        return build_llm_response(content='PRIVATE_SHOULD_NOT_RETURN')
    model=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
    monkeypatch.setattr(llm_module,'AsyncOpenAI',lambda **kw:model)
    response=await api_client.post('/v1/chat/completions',headers={'X-Service-Token':'synthetic-chat','X-User-Id':str(agent.user_id)},json={
        'agent_id':str(agent.id),'external_client_id':'client','channel':'website','message':'Когда запись?'})
    assert response.status_code==409 and 'PRIVATE_SHOULD_NOT_RETURN' not in response.text
