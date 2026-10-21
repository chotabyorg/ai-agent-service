"""Never leak success audio before checking a retired cancellation proposal."""
import pytest

from services.llm_service import LLMService
from tests.fakes import ScriptedLLMClient, build_llm_response
from tests.test_cancel_proposal_rejection_truth import cancellation_proposal
from tests.test_chotacrm_adapter import crm_settings  # noqa: F401
from tests.test_managed_streaming import chunk
from tests.test_pending_proposal_decision import decision, run_core


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['VOICE', 'WEBSITE_WIDGET'])
async def test_change_buffers_false_success_before_any_partial(
        db_session, crm_settings, monkeypatch, channel):
    runtime, provider, proposal_id = await cancellation_proposal(
        db_session, crm_settings, monkeypatch, channel, 'Нет, хочу изменить условия.')
    model = ScriptedLLMClient()
    model.chat.completions.queue(build_llm_response(tool_calls=[decision(proposal_id, 'CHANGE')]))
    scripted = model.chat.completions.create
    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return await scripted(**kwargs)
        if kwargs.get('stream'):
            async def generate():
                yield chunk(text='Запись успешно отменена.')
                yield chunk(text=' Всего доброго!')
                yield chunk(finish='stop')
            return generate()
        return build_llm_response(content='Запись успешно отменена. Всего доброго!')

    monkeypatch.setattr(model.chat.completions, 'create', create)
    published = []
    core = LLMService._execute_core

    async def publish(text):
        published.append(text)

    async def with_partials(self, *args, **kwargs):
        return await core(self, *args, **kwargs, partial_hook=publish)

    monkeypatch.setattr(LLMService, '_execute_core', with_partials)
    reply = await run_core(db_session, runtime, model)
    assert not published and not calls[1].get('stream')
    assert reply == 'Отмену записи не выполняю. Уточните, пожалуйста, что вы хотите изменить.'
    assert provider['posts'] == 1
