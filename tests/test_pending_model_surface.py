"""Reduce only the pending completion; ordinary guarded continuations survive."""
import json

import pytest

from core.config import get_settings
from services.managed_runtime import ManagedRuntime
from tests.fakes import ScriptedLLMClient, build_llm_response, build_tool_call
from tests.test_chotacrm_adapter import crm_settings, setup
from tests.test_pending_proposal_decision import CHOICE, decision, pending, run_core


names=lambda tools:{t['function']['name'] for t in tools}


@pytest.mark.asyncio
@pytest.mark.parametrize('intent,text,state,effects', [
    ('CONFIRM', '+', 'CONFIRMED', 1),
    ('CHANGE', 'да, только на час позже', 'CANCELLED', 0),
    ('REJECT', 'нет', 'CANCELLED', 0),
    ('UNRELATED', 'Как вас найти?', 'PREPARED', 0),
])
async def test_pending_only_advertises_decision_then_restores_ordinary_surface(
        db_session, crm_settings, monkeypatch, intent, text, state, effects):
    original,runtime,provider,proposal_id=await pending(db_session,crm_settings,monkeypatch,text)
    await runtime.business.prompt_context()
    canonical=runtime.business.openai_tools()
    expected=names(canonical)
    assert 'catalog_search' in expected and len(expected)>1
    assert 'business_decide_proposal' in expected
    model=ScriptedLLMClient()
    model.chat.completions.queue(build_llm_response(tool_calls=[decision(proposal_id,intent)]),
        build_llm_response(content='Ответ после решения runtime.'))
    await run_core(db_session,runtime,model)
    calls=model.chat.completions.calls
    assert names(calls[0]['tools'])=={'business_decide_proposal'}
    assert calls[0]['tool_choice']==CHOICE
    schema=calls[0]['tools'][0]['function']['parameters']
    assert schema['properties']['proposal_id']['enum']==[proposal_id]
    assert names(canonical)==expected
    assert 'enum' not in next(t for t in canonical if t['function']['name']=='business_decide_proposal')['function']['parameters']['properties']['proposal_id']
    plan=await original.repository.selected_plan(original.request,original.grant)
    assert plan.proposal_state==state and provider['posts']==effects
    if intent=='CONFIRM':
        assert len(calls)==1 and plan.operations[0].attempt_count==1
    else:
        assert len(calls)==2 and 'tool_choice' not in calls[1]
        assert names(calls[1]['tools'])==(expected-{'business_decide_proposal'} if intent in {'CHANGE','REJECT'} else expected)


@pytest.mark.asyncio
async def test_unrelated_pending_reply_can_continue_with_a_guarded_read(
        db_session,crm_settings,monkeypatch):
    original,runtime,provider,proposal_id=await pending(db_session,crm_settings,monkeypatch,'Какие услуги у вас есть?')
    read=build_tool_call('read','catalog_search',json.dumps({}))
    read.type='function'
    model=ScriptedLLMClient()
    model.chat.completions.queue(
        build_llm_response(tool_calls=[decision(proposal_id,'UNRELATED')]),
        build_llm_response(tool_calls=[read]),build_llm_response(content='Ответ после чтения.'))
    assert await run_core(db_session,runtime,model)=='Ответ после чтения.'
    calls=model.chat.completions.calls
    assert len(calls)==3 and names(calls[0]['tools'])=={'business_decide_proposal'}
    assert all('catalog_search' in names(c['tools']) and 'tool_choice' not in c for c in calls[1:])
    assert any(m.get('role')=='tool' and m.get('name')=='catalog_search' for m in calls[2]['messages'])
    plan=await original.repository.selected_plan(original.request,original.grant)
    assert plan.proposal_state=='PREPARED' and plan.operations[0].attempt_count==0
    assert provider['posts']==0


@pytest.mark.asyncio
async def test_no_pending_keeps_the_full_available_surface(db_session,crm_settings,monkeypatch):
    original,_,provider,_=await setup(db_session,crm_settings)
    runtime=ManagedRuntime(20,request=original.request)
    runtime.business=original
    monkeypatch.setattr(get_settings(),'knowledge_base_enabled',False)
    expected=names(original.openai_tools())-{'business_decide_proposal'}
    model=ScriptedLLMClient()
    model.chat.completions.queue(build_llm_response(content='Обычный ответ.'))
    assert await run_core(db_session,runtime,model)=='Обычный ответ.'
    assert names(model.chat.completions.calls[0]['tools'])==expected
    assert 'tool_choice' not in model.chat.completions.calls[0] and provider['posts']==0
