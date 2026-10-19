"""A model cannot prepare a spoken booking with an invented contact."""
from types import SimpleNamespace

import pytest

from schemas.business import BusinessToolResult
from schemas.managed_turn import ManagedTurnRequest
from services.managed_preparation import prepare_booking_customer_reuse
from tests.test_managed_turns import payload


@pytest.mark.parametrize('text,expected', [
    *[(f'Телефон плюс триста семьдесят пять двадцать девять сто двадцать три сорок два ноль {word}.',
       (f'+37529123420{digit}',)) for digit, word in enumerate(('один', 'два', 'три', 'четыре', 'пять'), 1)],
    ('Телефон +375 (29) 123-42-01.', ('+375291234201',)),
    ('Мой телефон 375 29 123 42 01.', ('+375291234201',)),
    ('Плюс три семь пять два девять один два три четыре два ноль один.', ('+375291234201',)),
    ('Телефон плюс триста семьдесят пять двадцать девять сто двадцать три сорок ноль один.', ('+375291234001',)),
    ('Телефон +375291234201, или +375291234202.', ()),
    ('Телефон 375291234201 или 375291234202.', ()),
    ('Телефон +375291234201 или два.', ()),
    ('Телефон +375291234201, запишите завтра или послезавтра.', ('+375291234201',)),
    ('Завтра 20.10.2026 в 10:30, цена 22 рубля.', ()),
    ('Телефон плюс триста семьдесят пять двадцать девять.', ()),
    ('Телефон плюс триста семьдесят пять миллионов.', ()),
    ('Телефон +3752912342011234.', ()),
    ('Телефон плюс неизвестно сорок два.', ()),
])
def test_phone_facts_preserve_spoken_groups_and_reject_incomplete_values(text, expected):
    from services.voice_contact import voice_phone_facts, voice_phone_hint
    value = payload(channel='VOICE')
    value['messages'][0]['text'] = text
    request = ManagedTurnRequest.model_validate(value)
    original = request.model_dump_json()
    assert voice_phone_facts(request) == expected
    assert voice_phone_hint(request)
    assert request.model_dump_json() == original


def test_only_customer_input_before_selected_boundary_supplies_phone():
    from uuid import uuid4
    from services.voice_contact import voice_phone_facts
    value = payload(channel='VOICE')
    value['messages'][0]['text'] = 'Запишите меня завтра.'
    value['messages'].append({**value['messages'][0], 'id': str(uuid4()), 'sequence': 2,
                              'text': 'Телефон +375291234201.'})
    value['context_last_sequence'] = 2
    assert voice_phone_facts(ManagedTurnRequest.model_validate(value)) == ()
    value['messages'][1].update(actor='AGENT', direction='OUTBOUND')
    value.update(input_first_sequence=2, input_last_sequence=2,
        selected_input_message_ids=[value['messages'][1]['id']])
    assert voice_phone_facts(ManagedTurnRequest.model_validate(value)) == ()


def test_explicit_contact_overrides_caller_and_other_channels_are_unchanged():
    from services.voice_contact import voice_phone_facts, voice_phone_hint
    value = payload(channel='VOICE')
    request = ManagedTurnRequest.model_validate(value)
    assert voice_phone_facts(request, '+375291234201') == ('+375291234201',)
    value['messages'][0]['text'] = 'Телефон +375291234202.'
    assert voice_phone_facts(ManagedTurnRequest.model_validate(value), '+375291234201') == ('+375291234202',)
    value['messages'][0]['text'] = 'Телефон плюс триста семьдесят пять.'
    assert voice_phone_facts(ManagedTurnRequest.model_validate(value), '+375291234201') == ()
    value['channel'] = 'WEBSITE_WIDGET'
    request = ManagedTurnRequest.model_validate(value)
    assert voice_phone_facts(request, '+375291234201') is None
    assert voice_phone_hint(request, '+375291234201') == ''


@pytest.mark.parametrize('question,answer,expected', [
    ('Назовите, пожалуйста, телефон для записи.', '375 29 123 42 01', ('+375291234201',)),
    ('Продиктуйте ваш контактный номер.', 'триста семьдесят пять двадцать девять сто двадцать три сорок два ноль один', ('+375291234201',)),
    ('На какую дату вас записать?', '20.10.2026', ()),
    ('Цена услуги 22 рубля.', '375291234201', ()),
])
def test_digits_alone_require_a_customer_answer_to_a_contact_question(question, answer, expected):
    from uuid import uuid4
    from services.voice_contact import voice_phone_facts
    value = payload(channel='VOICE')
    question_message = {**value['messages'][0], 'actor': 'AGENT', 'direction': 'OUTBOUND', 'text': question}
    answer_message = {**value['messages'][0], 'id': str(uuid4()), 'sequence': 2, 'text': answer}
    value.update(messages=[question_message, answer_message], context_last_sequence=2,
        input_first_sequence=2, input_last_sequence=2, selected_input_message_ids=[answer_message['id']])
    assert voice_phone_facts(ManagedTurnRequest.model_validate(value)) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize('text,phone', [
    ('Запишите меня завтра в 13:00. Меня зовут Артем.', '+375000000000'),
    ('Телефон плюс триста семьдесят пять двадцать девять сто двадцать три сорок два ноль один.', '+375291234001'),
    ('Телефон +375291234201.', '+375291234001'),
    ('Телефон +375291234201.', '+375291234201'),
    ('Телефон плюс триста семьдесят пять двадцать девять сто двадцать три сорок два ноль один.', '+375291234201'),
])
async def test_only_grounded_phone_can_reach_customer_lookup(monkeypatch, text, phone):
    value = payload(channel='VOICE')
    value['messages'][0]['text'] = text
    request = ManagedTurnRequest.model_validate(value)
    original = request.model_dump_json()
    calls = []

    async def guard(**kwargs):
        return None

    async def read(*args, **kwargs):
        calls.append((args, kwargs))
        return BusinessToolResult(tool_payload={'status': 'OK', 'data': {'match': 'NONE', 'candidates': []}})

    monkeypatch.setattr('business_capabilities.runtime_tools.call_capability', read)
    definition = SimpleNamespace(normalize=lambda args: args)
    runtime = SimpleNamespace(request=request, execution=SimpleNamespace(caller_phone=''),
        registration=SimpleNamespace(capability=lambda name: definition),
        binding=SimpleNamespace(enabled_capabilities=['booking.create', 'customer.find']),
        execution_guard=guard, collector=SimpleNamespace(snapshot=lambda: []))
    args = dict(resource_ref='resource_test', offering_ref='offering_test',
        start_at='2099-01-01T12:00:00Z',
        customer=dict(display_name='Артем', identity=dict(kind='PHONE', value=phone)))
    result = await prepare_booking_customer_reuse(runtime, 'booking_create', args)
    if phone == '+375291234201':
        assert result is None and len(calls) == 1
        assert calls[0][0][2] == {'identity': {'kind': 'PHONE', 'value': phone}}
    else:
        assert result is not None and result.safe_draft
        assert 'Подтверждаете' not in result.safe_draft
        assert not calls
    assert args['customer']['identity']['value'] == phone
    assert request.model_dump_json() == original


@pytest.mark.integration
@pytest.mark.parametrize('tool', ['booking_create', 'business_propose'])
@pytest.mark.parametrize('speech', [
    'Запишите меня завтра. Меня зовут Влад.',
    'Телефон +375291112234.',
    'Телефон +375291112233 или +375291112234.',
])
async def test_voice_contact_refusal_cannot_reserve_a_plan_or_lookup_another_customer(
        api_client, db_session, monkeypatch, tool, speech):
    from uuid import UUID
    from sqlalchemy import func, select
    from core.config import get_settings
    from models.business import BusinessOperation, BusinessPlan
    from tests.business_effect_helpers import open_business_runtime
    from tests.test_chotacrm_managed_proposal_errors import install_managed_crm, preparation_arguments

    value, args, registry, calls, effect, _ = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, channel='VOICE')
    value['messages'][0]['text'] = speech
    request = ManagedTurnRequest.model_validate(value)
    original = request.model_dump_json()
    grant = next(grant for grant in get_settings().managed_principals[0].grants
        if grant.scope() == (value['owner_id'], UUID(value['integration_id']),
                             value['channel'], UUID(value['agent_id'])))
    runtime = await open_business_runtime(request, grant, db_session, registry, effect_port=effect)
    await runtime.prompt_context()
    result = await runtime.call_tool(tool, preparation_arguments(tool, args))
    assert result.safe_draft and 'Подтверждаете' not in result.safe_draft
    assert calls['lookup'] == calls['facts'] == calls['execute'] == 0
    assert not effect.permit_requests
    assert request.model_dump_json() == original
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0
