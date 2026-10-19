"""Give the model a format hint before pending decisions, then retire the hint."""
from copy import deepcopy
import json
from uuid import UUID

import pytest
from sqlalchemy import select

from models.business import BusinessOperation, BusinessPlan
from schemas.managed_turn import ManagedTurnRequest
from services.business_runtime import BusinessRuntime
from services.llm_service import LLMService
from tests.fakes import build_llm_response
from tests.test_business_runtime import later
from tests.test_managed_turns import execute
from tests.test_pending_proposal_decision import decision
from tests.test_voice_pending_prose_repair import prepared


def format_hints(call):
    """Find only the code-owned hint, not a substring in customer data."""
    return [m for m in call['messages'] if m['role'] == 'system'
            and m['content'].startswith('A pending proposal still requires a semantic decision.')]


@pytest.mark.integration
@pytest.mark.parametrize('channel', ['VOICE', 'WEBSITE_WIDGET'])
@pytest.mark.parametrize('intent,text,state,effect_count', [
    ('CONFIRM', 'Да, подтверждаю запись.', 'CONFIRMED', 1),
    ('CHANGE', 'Нет, измените время.', 'CANCELLED', 0),
    ('REJECT', 'Нет, не записывайте.', 'CANCELLED', 0),
    ('UNRELATED', 'Какой у вас адрес?', 'PREPARED', 0),
])
async def test_first_hint_is_voice_only_and_retires_after_semantic_decision(
        api_client, db_session, monkeypatch, channel, intent, text, state, effect_count):
    """A hint changes model guidance while the real runtime owns every effect."""
    value, proposal_id, _, calls, effects, model = await prepared(
        api_client, db_session, monkeypatch, channel=channel)
    request = later(ManagedTurnRequest.model_validate(value), text)
    original = request.model_dump_json()
    captured = []
    create = model.chat.completions.create

    async def capture(**kwargs):
        captured.append(deepcopy(kwargs))
        return await create(**kwargs)

    monkeypatch.setattr(model.chat.completions, 'create', capture)
    model.chat.completions.queue(build_llm_response(tool_calls=[decision(proposal_id, intent)]),
        build_llm_response(content='Подскажите, пожалуйста, детали вопроса.'))
    result = (await execute(api_client, request.model_dump(mode='json'))).json()
    assert result['status'] == 'DRAFT_READY', result
    assert len(format_hints(captured[0])) == (1 if channel == 'VOICE' else 0)
    assert captured[0]['messages'][-1] == {'role': 'user', 'content': text}
    if channel == 'VOICE':
        assert 'tool_choice' not in captured[0]
        assert [tool['function']['name'] for tool in captured[0]['tools']] == ['business_decide_proposal']
    for call in captured[1:]:
        assert not format_hints(call)
        if channel == 'VOICE':
            assert 'business_decide_proposal' not in {t['function']['name'] for t in call['tools']}
    assert request.model_dump_json() == original
    assert calls['execute'] == len(effects.permit_requests) == effect_count
    async with db_session() as db:
        assert (await db.get(BusinessPlan, UUID(proposal_id))).proposal_state == state
        assert (await db.scalars(select(BusinessOperation))).one().attempt_count == effect_count


@pytest.mark.integration
async def test_repaired_unrelated_reply_removes_format_hint_before_normal_answer(
        api_client, db_session, monkeypatch):
    """An existing repair hint must not demand a hidden tool after UNRELATED."""
    value, proposal_id, _, calls, effects, model = await prepared(api_client, db_session, monkeypatch)
    captured = []
    create = model.chat.completions.create

    async def capture(**kwargs):
        captured.append(deepcopy(kwargs))
        return await create(**kwargs)

    monkeypatch.setattr(model.chat.completions, 'create', capture)
    model.chat.completions.queue(build_llm_response(content='Непроверенное утверждение: готово.'),
        build_llm_response(tool_calls=[decision(proposal_id, 'UNRELATED')]),
        build_llm_response(content='Подскажите, пожалуйста, какой адрес вас интересует.'))
    request = later(ManagedTurnRequest.model_validate(value), 'Какой у вас адрес?')
    result = (await execute(api_client, request.model_dump(mode='json'))).json()
    assert result['status'] == 'DRAFT_READY', result
    assert len(captured) == 3 and len(format_hints(captured[1])) == 1
    assert not format_hints(captured[2])
    assert 'Непроверенное' not in str(captured[1]['messages'])
    assert calls['execute'] == 0 and not effects.permit_requests


@pytest.mark.integration
async def test_nonconfirmable_projection_does_not_receive_first_confirmation_hint(
        api_client, db_session, monkeypatch):
    """Keep the existing stale-proposal prompt instead of strengthening it."""
    value, proposal_id, _, calls, effects, model = await prepared(api_client, db_session, monkeypatch)
    project = BusinessRuntime.prompt_context

    async def nonconfirmable(self):
        result = await project(self)
        if result.get('pending'):
            result = {**result, 'pending': {**result['pending'], 'confirmable': False}}
        return result

    monkeypatch.setattr(BusinessRuntime, 'prompt_context', nonconfirmable)
    model.chat.completions.queue(build_llm_response(tool_calls=[decision(proposal_id, 'UNRELATED')]),
        build_llm_response(content='Уточните, пожалуйста, вопрос.'))
    request = later(ManagedTurnRequest.model_validate(value), 'Какой у вас адрес?')
    result = (await execute(api_client, request.model_dump(mode='json'))).json()
    assert result['status'] == 'DRAFT_READY', result
    assert not format_hints(model.chat.completions.calls[-2])
    assert calls['execute'] == 0 and not effects.permit_requests


@pytest.mark.integration
@pytest.mark.parametrize('fits', [True, False])
async def test_first_hint_is_mandatory_when_context_budget_trims_old_history(
        api_client, db_session, monkeypatch, fits):
    """Keep selected input and the hint, or fail closed before asking the model."""
    value, proposal_id, _, calls, effects, model = await prepared(api_client, db_session, monkeypatch)
    request = later(ManagedTurnRequest.model_validate(value), 'Какой у вас адрес?')
    budget = LLMService._budget_managed_messages
    observed = []

    def with_minimal_budget(self, agent, messages, tools, selected_indices, communication, simple, **kwargs):
        prefix = messages[:-len(communication)]
        assert len(format_hints({'messages': prefix})) == 1
        assert simple and len(communication) > len(selected_indices) == 1
        selected = [communication[i] for i in selected_indices]
        envelope = self._managed_envelope(agent, prefix + selected, tools, **kwargs)
        size = len(json.dumps(envelope, ensure_ascii=False, allow_nan=False).encode('utf-8'))
        with monkeypatch.context() as settings_patch:
            settings_patch.setattr(self.settings, 'managed_ai_context_bytes', size if fits else size - 1)
            result = budget(self, agent, messages, tools, selected_indices, communication, simple, **kwargs)
        assert result == prefix + selected
        observed.append(deepcopy(result))
        return result

    monkeypatch.setattr(LLMService, '_budget_managed_messages', with_minimal_budget)
    before = len(model.chat.completions.calls)
    model.chat.completions.queue(build_llm_response(tool_calls=[decision(proposal_id, 'UNRELATED')]),
        build_llm_response(content='Подскажите, пожалуйста, какой адрес вас интересует.'))
    result = (await execute(api_client, request.model_dump(mode='json'))).json()
    if fits:
        assert result['status'] == 'DRAFT_READY' and len(observed) == 1
        assert observed[0][-1] == {'role': 'user', 'content': 'Какой у вас адрес?'}
        assert len(model.chat.completions.calls) == before + 2
    else:
        assert result['status'] == 'FAILED' and result['error_code'] == 'CONTEXT_LIMIT'
        assert not observed and len(model.chat.completions.calls) == before
    assert calls['execute'] == 0 and not effects.permit_requests
