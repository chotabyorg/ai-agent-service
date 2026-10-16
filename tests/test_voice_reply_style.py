"""Spoken brevity cannot affect other scopes, history, tools or confirmation."""
# ruff: noqa: F811 -- imported pytest fixture is requested by test parameters
import json
from uuid import uuid4

import pytest

from core.config import get_settings
from schemas.managed_turn import ManagedTurnRequest
from services.llm_service import LLMService
from services.managed_tools import ManagedExecutionError
from services.prompt_composer import PLATFORM_POLICY
from services.voice_reply_style import CONCISE_SPEECH
from tests.fakes import build_llm_response, build_tool_call
from tests.test_managed_business_fakes import install_business, plan_arguments
from tests.test_managed_turns import configure, execute, model, setup  # noqa: F401
from tests.test_voice_latency import profile


@pytest.mark.asyncio
@pytest.mark.parametrize('scope', ['VOICE', 'TELEGRAM', 'WEBSITE_WIDGET', 'INSTAGRAM', 'WHATSAPP', 'other_agent', 'off'])
async def test_style_is_opt_in_for_exact_voice_scope(api_client, db_session, monkeypatch, model, scope):
    agent, value = await setup(api_client, db_session, monkeypatch, channel='VOICE')
    selected = profile(value, agent['model_name'], True).model_copy(update={'concise_speech': scope != 'off'})
    if scope == 'other_agent':
        selected = selected.model_copy(update={'agent_id': uuid4()})
    elif scope not in {'VOICE', 'off'}:
        value['channel'] = scope
        configure(monkeypatch, value, agent['user_id'])
    monkeypatch.setattr(get_settings(), 'managed_voice_latency_profiles', [selected])
    model.chat.completions.queue(build_llm_response(content='Ответ целиком, без обрезки.'))
    result = (await execute(api_client, value)).json()
    assert result['draft_text'] == 'Ответ целиком, без обрезки.'
    call = model.chat.completions.calls[0]
    system = call['messages'][0]['content']
    assert (CONCISE_SPEECH in system) is (scope == 'VOICE')
    assert PLATFORM_POLICY in system
    assert call['max_tokens'] == 1024  # Full structured tool arguments remain possible.
    assert call['parallel_tool_calls'] is False
    assert model.closed


@pytest.mark.asyncio
async def test_public_voice_cannot_enable_spoken_profile(api_client, db_session, monkeypatch, model):
    agent, value = await setup(api_client, db_session, monkeypatch, channel='VOICE')
    selected = profile(value, agent['model_name'], True).model_copy(update={'concise_speech': True})
    monkeypatch.setattr(get_settings(), 'managed_voice_latency_profiles', [selected])
    model.chat.completions.queue(build_llm_response(content='Legacy reply.'))
    response = await api_client.post('/v1/chat/completions', json={
        'agent_id': agent['id'], 'external_client_id': 'synthetic-voice', 'channel': 'voice', 'message': 'Привет'})
    assert response.status_code == 200
    assert CONCISE_SPEECH not in model.chat.completions.calls[0]['messages'][0]['content']


@pytest.mark.asyncio
async def test_spoken_style_preserves_confirmation_and_full_safe_draft(api_client, db_session, monkeypatch, model):
    agent, value = await setup(api_client, db_session, monkeypatch, channel='VOICE')
    selected = profile(value, agent['model_name'], True).model_copy(update={'concise_speech': True})
    monkeypatch.setattr(get_settings(), 'managed_voice_latency_profiles', [selected])
    authority = await install_business(monkeypatch, value, db_session, confirmation=True)
    model.chat.completions.queue(build_llm_response(tool_calls=[build_tool_call(
        'spoken-prepare', 'business_execute_plan', plan_arguments())]))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY' and result['draft_text']
    assert authority.commands == []
    assert len(result['operation_refs']) == 1 and result['operation_refs'][0]['outcome'] is None
    assert len(model.chat.completions.calls) == 1
    assert CONCISE_SPEECH in model.chat.completions.calls[0]['messages'][0]['content']


@pytest.mark.asyncio
async def test_complete_tool_continuation_and_reply_are_not_truncated(api_client, db_session, monkeypatch, model):
    agent, value = await setup(api_client, db_session, monkeypatch, channel='VOICE')
    selected = profile(value, agent['model_name'], True).model_copy(update={'concise_speech': True})
    monkeypatch.setattr(get_settings(), 'managed_voice_latency_profiles', [selected])
    authority = await install_business(monkeypatch, value, db_session, confirmation=False)
    complete = 'Существенные сведения о предложении. ' * 35
    model.chat.completions.queue(
        build_llm_response(tool_calls=[build_tool_call('spoken-read', 'business_execute_plan', plan_arguments())]),
        build_llm_response(content=complete),
    )
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY' and result['draft_text'] == complete.strip()
    assert len(authority.commands) == 1
    assert len(model.chat.completions.calls) == 2
    for call in model.chat.completions.calls:
        assert CONCISE_SPEECH in call['messages'][0]['content']
        assert call['max_tokens'] == 1024
    # The additional prompt must also count toward the managed envelope budget.
    service = LLMService()
    request = ManagedTurnRequest.model_validate(value)
    from types import SimpleNamespace
    selected_agent = SimpleNamespace(model_name=agent['model_name'], temperature=0.4)
    call = model.chat.completions.calls[0]
    envelope = service._managed_envelope(selected_agent, call['messages'], call['tools'], request=request)
    size = len(json.dumps(envelope, ensure_ascii=False).encode())
    monkeypatch.setattr(service.settings, 'managed_ai_context_bytes', size - 1)
    with pytest.raises(ManagedExecutionError, match='CONTEXT_LIMIT'):
        service._check_managed_envelope(selected_agent, call['messages'], call['tools'], request=request)
