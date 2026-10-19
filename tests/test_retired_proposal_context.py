"""Retired proposals must disappear from current model context and tool choice."""
import pytest

from tests.fakes import ScriptedLLMClient,build_llm_response
from tests.test_chotacrm_adapter import crm_settings  # noqa: F401
from tests.test_pending_proposal_decision import pending,decision,run_core


@pytest.mark.asyncio
@pytest.mark.parametrize('intent,text',[('CHANGE','Измените телефон на +375291110022'),('REJECT','Нет, не записывайте')])
async def test_retirement_refreshes_prompt_and_removes_obsolete_confirmation_tool(
        db_session,crm_settings,monkeypatch,intent,text):
    """Refresh the model-facing facts after the real guarded local retirement."""
    original,runtime,provider,proposal_id=await pending(db_session,crm_settings,monkeypatch,text)
    model=ScriptedLLMClient()
    model.chat.completions.queue(build_llm_response(tool_calls=[decision(proposal_id,intent)]),
                                 build_llm_response(content='Уточните желаемые изменения.'))
    assert await run_core(db_session,runtime,model)=='Уточните желаемые изменения.'
    before,after=model.chat.completions.calls
    assert proposal_id in before['messages'][0]['content']
    assert proposal_id not in after['messages'][0]['content']
    assert 'business_decide_proposal' not in {t['function']['name'] for t in after['tools']}
    assert 'tool_choice' not in after
    plan=await original.repository.selected_plan(original.request,original.grant)
    assert plan.proposal_state=='CANCELLED' and plan.operations[0].attempt_count==0
    assert provider['posts']==0
