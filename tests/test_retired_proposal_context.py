"""Retired proposals must disappear from current model context and tool choice."""
from copy import deepcopy
import pytest

from tests.fakes import ScriptedLLMClient,build_llm_response
from tests.test_chotacrm_adapter import crm_settings  # noqa: F401
from tests.test_pending_proposal_decision import pending,decision,run_core
from tests.test_preparation_schema_correction import preparation
from tests.test_text_booking_recovery import failure


@pytest.mark.asyncio
@pytest.mark.parametrize('intent,text',[('CHANGE','Измените телефон на +375291110022'),('REJECT','Нет, не записывайте')])
async def test_retirement_refreshes_prompt_and_removes_obsolete_confirmation_tool(
        db_session,crm_settings,monkeypatch,intent,text):
    """Refresh the model-facing facts after the real guarded local retirement."""
    original,runtime,provider,proposal_id=await pending(db_session,crm_settings,monkeypatch,text)
    model=ScriptedLLMClient()
    requests=[]
    create=model.chat.completions.create
    async def record(**kwargs):
        """Snapshot each envelope before later context mutation can alter the fake."""
        requests.append(deepcopy(kwargs))
        return await create(**kwargs)
    monkeypatch.setattr(model.chat.completions,'create',record)
    model.chat.completions.queue(build_llm_response(tool_calls=[decision(proposal_id,intent)]),
                                 build_llm_response(content='Уточните желаемые изменения.'))
    assert await run_core(db_session,runtime,model)=='Уточните желаемые изменения.'
    before,after=requests
    assert proposal_id in before['messages'][0]['content']
    assert proposal_id not in after['messages'][0]['content']
    assert 'business_decide_proposal' not in {t['function']['name'] for t in after['tools']}
    assert 'tool_choice' not in after
    plan=await original.repository.selected_plan(original.request,original.grant)
    assert plan.proposal_state=='CANCELLED' and plan.operations[0].attempt_count==0
    assert provider['posts']==0


@pytest.mark.asyncio
@pytest.mark.parametrize('transient_error',[False,True])
async def test_changed_contact_prepares_a_new_plan_without_reusing_old_confirmation(
        db_session,crm_settings,monkeypatch,transient_error):
    """Persist the changed contact before any fresh confirmation can execute it."""
    original,runtime,provider,old_id=await pending(db_session,crm_settings,monkeypatch,
        'Измените телефон на +375291110022')
    old=await original.repository.selected_plan(original.request,original.grant)
    arguments=dict(old.operations[0].command.arguments)
    arguments['customer']={**arguments['customer'],'identity':{'kind':'PHONE','value':'+375291110022'}}
    model=ScriptedLLMClient()
    model.chat.completions.queue(build_llm_response(tool_calls=[decision(old_id,'CHANGE')]))
    if transient_error:model.chat.completions.queue(failure(429))
    model.chat.completions.queue(preparation('booking_create',arguments,'new-proposal'))
    reply=await run_core(db_session,runtime,model)
    assert '+375291110022' in reply and '+375291112233' not in reply
    plan=await runtime.business.repository.selected_plan(runtime.request,runtime.business.grant)
    assert str(plan.id)!=old_id and plan.proposal_state=='PREPARED'
    assert plan.confirmed_turn_id is None and plan.operations[0].attempt_count==0
    assert plan.operations[0].command.arguments['customer']['identity']['value']=='+375291110022'
    assert (await original.repository.selected_plan(original.request,original.grant)).proposal_state=='CANCELLED'
    assert provider['posts']==0 and runtime.deadline>0
