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
from tests.test_chotacrm_adapter import crm_settings  # noqa: F401


@pytest.mark.parametrize('last_agent,reply,expected', [
    (None, 'Да, да, да.', True),
    (None, 'Да, я ещё на линии.', True),
    (None, 'Да, я еще на линии.', True),
    (None, 'Слушаю.', True),
    (None, 'Да, да, я на месте.', True),
    ('Вы ещё на линии?', 'Я здесь.', True),
    ('Записать вас завтра в 13:00? Подтверждаете?', 'да', False),
    (None, 'Да, подтверждаю запись.', False),
    (None, 'Нет, отмените запись.', False),
])
def test_presence_hint_does_not_consume_later_business_answers(last_agent, reply, expected):
    from services.voice_reply_style import replying_to_connection_check
    from tests.test_managed_turns import payload

    value = payload(channel='VOICE')
    messages = [{**value['messages'][0], 'text': '[silence]'}]
    for actor, text in ([('AGENT', last_agent)] if last_agent else []) + [('CUSTOMER', reply)]:
        messages.append({**value['messages'][0], 'id': str(uuid4()), 'sequence': len(messages) + 1,
                         'actor': actor, 'text': text})
    value.update(messages=messages, input_first_sequence=len(messages), input_last_sequence=len(messages),
                 context_last_sequence=len(messages), selected_input_message_ids=[messages[-1]['id']])
    assert replying_to_connection_check(ManagedTurnRequest.model_validate(value)) is expected


@pytest.mark.parametrize('first,older_reply,business_question,expected', [
    (1, False, False, True),
    (1, True, False, True),
    (3, True, False, True),
    (1, False, True, False),
    (1, True, True, False),
])
def test_presence_classifies_latest_reply_in_a_coalesced_selection(
        first, older_reply, business_question, expected):
    from services.voice_reply_style import replying_to_connection_check, past_idle_communication
    from tests.test_managed_turns import payload

    value = payload(channel='VOICE')
    messages = [{**value['messages'][0], 'text': '[silence]'}]
    history = ([('CUSTOMER', 'Да, я слышу вас.')] if older_reply else [])
    if business_question:
        history += [('AGENT', 'Записать вас завтра в 13:00? Подтверждаете?')]
    history += [('CUSTOMER', 'Да, я ещё на линии.')]
    for actor, text in history:
        messages.append({**messages[0], 'id': str(uuid4()), 'sequence': len(messages) + 1,
                         'actor': actor, 'text': text})
    value.update(messages=messages, input_first_sequence=first, input_last_sequence=len(messages),
                 context_last_sequence=len(messages), selected_input_message_ids=[m['id'] for m in messages[first - 1:]])
    request = ManagedTurnRequest.model_validate(value)
    assert replying_to_connection_check(request) is expected
    raw = request.model_dump_json()
    communication = [{'role': 'user' if m.actor == 'CUSTOMER' else 'assistant', 'content': m.text}
                     for m in request.messages]
    projected = past_idle_communication(request, communication)
    assert projected[0]['content'].startswith('[Past voice control event:')
    assert projected[1:] == communication[1:]
    assert communication[0]['content'] == '[silence]'
    assert request.model_dump_json() == raw


@pytest.mark.asyncio
@pytest.mark.parametrize('latest', ['[silence]', 'да'])
async def test_current_input_distinguishes_silence_from_reply_after_interrupted_question(
        api_client, db_session, monkeypatch, model, latest):
    agent, value = await setup(api_client, db_session, monkeypatch, channel='VOICE')
    selected = profile(value, agent['model_name'], True)
    monkeypatch.setattr(get_settings(), 'managed_voice_latency_profiles', [selected])
    value['messages'][0]['text'] = '[silence]'
    message = {**value['messages'][0], 'id': str(uuid4()), 'sequence': 2, 'text': latest}
    value.update(messages=[value['messages'][0], message], input_first_sequence=2,
                 input_last_sequence=2, context_last_sequence=2,
                 selected_input_message_ids=[message['id']])
    model.chat.completions.queue(build_llm_response(content='Продолжим запись.'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY', result
    if latest == '[silence]':
        assert result['draft_text'] == 'Вы ещё на линии?'
        assert result['operation_refs'] == []
        assert model.chat.completions.calls == []
        return
    prompt = model.chat.completions.calls[0]['messages'][0]['content']
    assert ('Текущий вход — сигнал тишины.' in prompt) is (latest == '[silence]')
    assert ('Клиент сейчас ответил; это не сигнал тишины.' in prompt) is (latest != '[silence]')
    # The canonical window and selected input remain the same; no synthetic
    # assistant delivery or confirmation is inserted to repair presentation.
    assert value['messages'][0]['text'] == '[silence]' and message['text'] == latest
    history = model.chat.completions.calls[0]['messages'][1:]
    assert history[-1] == {'role': 'user', 'content': latest}
    assert history[0]['content'].startswith('[Past voice control event:')
    assert 'Если последняя реплика клиента' not in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize('delivered', [False, True])
@pytest.mark.parametrize('intent', ['CONFIRM', 'UNRELATED'])
@pytest.mark.parametrize('coalesced', [False, True])
@pytest.mark.parametrize('prose_first', [False, True])
async def test_presence_after_idle_cannot_confirm_pending_booking(
        db_session, crm_settings, monkeypatch, model, delivered, intent, coalesced, prose_first):
    from models.business import BusinessPlan
    from services.business_runtime import BusinessRuntime
    from services.managed_runtime import ManagedRuntime
    from services.managed_turn_service import execute_managed_owned
    from tests.business_effect_helpers import FakeConversationEffectPort
    import tests.test_chotacrm_adapter as crm_fixture
    from tests.test_business_runtime import make_request

    monkeypatch.setattr(crm_fixture, 'make_request', lambda agent: make_request(
        agent, channel='VOICE', messages=[dict(
            id=uuid4(), sequence=1, actor='CUSTOMER', direction='INBOUND', content_type='TEXT',
            text='Запишите меня, Влад. Телефон +375291112233.')]))
    original, registry, provider, args = await crm_fixture.setup(db_session, crm_settings)
    prepared = await original.call_tool('booking_create', args)
    proposal_id = prepared.tool_payload['proposal_id']
    value = original.request.model_dump(mode='json')
    monkeypatch.setattr(get_settings(), 'managed_voice_latency_profiles', [profile(value, 'test')])
    monkeypatch.setattr(LLMService, '_get_agent_api_key', lambda *_: 'synthetic')
    monkeypatch.setattr(LLMService, '_create_llm_client', lambda *_args, **_kwargs: model)
    original_open = BusinessRuntime.open.__func__

    async def open_with_authority(cls, *args, **kwargs):
        kwargs.setdefault('effect_port', FakeConversationEffectPort())
        return await original_open(cls, *args, **kwargs)

    monkeypatch.setattr(BusinessRuntime, 'open', classmethod(open_with_authority))
    messages = list(value['messages'])
    for actor, text in [('CUSTOMER', '[silence]')] + ([('AGENT', 'Вы ещё на линии?')] if delivered else []) + [('CUSTOMER', 'да да да')]:
        messages.append({**value['messages'][0], 'id': str(uuid4()), 'sequence': len(messages) + 1,
                         'actor': actor, 'text': text})
    first = len(value['messages']) + 1 if coalesced else len(messages)
    value.update(turn_id=str(uuid4()), messages=messages, input_revision=2,
                 input_first_sequence=first, input_last_sequence=len(messages),
                 context_last_sequence=len(messages), selected_input_message_ids=[m['id'] for m in messages[first - 1:]])
    call = build_tool_call('presence', 'business_decide_proposal',
                          json.dumps({'proposal_id': proposal_id, 'intent': intent}))
    call.type = 'function'
    if prose_first:
        model.chat.completions.queue(build_llm_response(content='Непроверенное утверждение: вы записаны.'))
    model.chat.completions.queue(build_llm_response(tool_calls=[call]),
        build_llm_response(content='Я вас слышу. Подтвердите предложенную запись, пожалуйста.'))
    async def db_provider():
        async with db_session() as db:
            yield db

    result = (await execute_managed_owned(ManagedTurnRequest.model_validate(value), original.grant,
        ManagedRuntime(20, request=ManagedTurnRequest.model_validate(value)), db_provider, db_session, registry)).model_dump()
    assert provider['posts'] == 0
    async with db_session() as db:
        from uuid import UUID
        assert (await db.get(BusinessPlan, UUID(proposal_id))).proposal_state == 'PREPARED'
    if coalesced and delivered:
        # A selection containing an agent Message cannot supply business input.
        # No repair is allowed. The existing result guard can replace rejected
        # prose with the retained proposal; it still cannot authorize an effect.
        if prose_first:
            assert result['status'] == 'DRAFT_READY'
            assert len(model.chat.completions.calls) == 1
            assert 'Непроверенное' not in result['draft_text']
        else:
            assert result['status'] == 'FAILED' and result['error_code'] == 'TOOL_SETUP_FAILED', result
        return
    elif intent == 'CONFIRM':
        assert result['status'] == 'FAILED' and result['error_code'] == 'INVALID_RESULT', result
    else:
        assert result['status'] == 'DRAFT_READY', (result['error_code'], len(model.chat.completions.calls))
    decision_tool = next(t for t in model.chat.completions.calls[0]['tools']
                         if t['function']['name'] == 'business_decide_proposal')
    assert decision_tool['function']['parameters']['properties']['intent']['enum'] == ['UNRELATED']


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
