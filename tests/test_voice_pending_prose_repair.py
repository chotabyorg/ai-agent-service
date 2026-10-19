"""An omitted decision can be repaired once, without accepting model prose."""
from copy import deepcopy
from uuid import UUID

import pytest
from sqlalchemy import select

from models.business import BusinessOperation, BusinessPlan
from schemas.managed_turn import ManagedTurnRequest
from services.llm_service import LLMService
from tests.fakes import build_llm_response
from tests.test_business_runtime import later
from tests.test_chotacrm_managed_proposal_errors import install_managed_crm
from tests.test_managed_turns import execute
from tests.test_pending_proposal_decision import decision
from tests.test_preparation_schema_correction import preparation
from tests.test_text_booking_recovery import failure


async def prepared(api_client, db_session, monkeypatch, channel='VOICE'):
    """Persist a real proposal before constructing a later customer reply."""
    value, args, registry, calls, effects, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, channel=channel, allow_execute=True)
    model.chat.completions.queue(preparation('booking_create', args, 'prepare'))
    assert (await execute(api_client, value)).json()['status'] == 'DRAFT_READY'
    async with db_session() as db:
        proposal_id = str((await db.scalars(select(BusinessPlan))).one().id)
    return value, proposal_id, registry, calls, effects, model


@pytest.mark.integration
@pytest.mark.parametrize('partials', [False, True])
@pytest.mark.parametrize('intent,text,state,count', [
    ('CONFIRM', 'Да, подтверждаю запись.', 'CONFIRMED', 1),
    ('REJECT', 'Нет, не записывайте.', 'CANCELLED', 0),
    ('CHANGE', 'Нет, перенесите на час позже.', 'CANCELLED', 0),
    ('UNRELATED', 'Какой у вас адрес?', 'PREPARED', 0),
])
async def test_pending_prose_is_discarded_before_one_semantic_repair(
        api_client, db_session, monkeypatch, partials, intent, text, state, count):
    """Only a fresh runtime-validated decision can authorize an effect."""
    value, proposal_id, _, calls, effects, model = await prepared(api_client, db_session, monkeypatch)
    request = later(ManagedTurnRequest.model_validate(value), text)
    original_request = request.model_dump_json()
    captured, published = [], []
    create = model.chat.completions.create

    async def capture(**kwargs):
        captured.append(deepcopy(kwargs))
        if len(captured) <= 2:
            assert calls['execute'] == 0 and not effects.permit_requests
        return await create(**kwargs)

    monkeypatch.setattr(model.chat.completions, 'create', capture)
    if partials:
        original_core = LLMService._execute_core

        async def publish(text):
            published.append(text)

        async def with_partials(self, *args, **kwargs):
            kwargs['partial_hook'] = publish
            return await original_core(self, *args, **kwargs)

        monkeypatch.setattr(LLMService, '_execute_core', with_partials)
    rejected_prose = 'Непроверенное утверждение: всё готово, запись отменена.'
    model.chat.completions.queue(build_llm_response(content=rejected_prose),
        build_llm_response(tool_calls=[decision(proposal_id, intent)]),
        build_llm_response(content='Уточните, пожалуйста, ваш вопрос.'))
    result = (await execute(api_client, request.model_dump(mode='json'))).json()
    assert result['status'] == 'DRAFT_READY', result
    assert calls['execute'] == count and len(effects.permit_requests) == count
    assert len(captured) == (2 if intent == 'CONFIRM' else 3)
    for call in captured[:2]:
        assert 'tool_choice' not in call and not call.get('stream')
        assert [tool['function']['name'] for tool in call['tools']] == ['business_decide_proposal']
    assert rejected_prose not in str(captured[1]['messages'])
    assert rejected_prose not in published and rejected_prose not in result['draft_text']
    assert request.model_dump_json() == original_request
    async with db_session() as db:
        plan = await db.get(BusinessPlan, UUID(proposal_id))
        operation = (await db.scalars(select(BusinessOperation))).one()
        assert plan.proposal_state == state
        assert operation.attempt_count == count


@pytest.mark.integration
@pytest.mark.parametrize('bad,expected_calls', [
    ('again', 2), ('wrong_proposal', 2), ('multiple', 2), ('malformed', 2),
    ('provider_failure', 2), ('empty', 1), ('length', 1), ('content_filter', 1), ('missing_finish', 1),
])
async def test_repair_is_bounded_and_never_authorizes_invalid_or_incomplete_output(
        api_client, db_session, monkeypatch, bad, expected_calls):
    """A second invalid response leaves the original operation untouched."""
    value, proposal_id, _, calls, effects, model = await prepared(api_client, db_session, monkeypatch)
    first = build_llm_response(content='Вы записаны.')
    second = build_llm_response(content='Запись отменена.')
    if bad == 'wrong_proposal':
        second = build_llm_response(tool_calls=[decision('unselected', 'CONFIRM')])
    elif bad == 'multiple':
        second = build_llm_response(tool_calls=[decision(proposal_id, 'CONFIRM'), decision(proposal_id, 'REJECT')])
    elif bad == 'malformed':
        second = build_llm_response(tool_calls=[decision(proposal_id, 'CONFIRM')])
        second.choices[0].message.tool_calls[0].function.arguments = '{'
    elif bad == 'provider_failure':
        second = failure(400)
    elif bad == 'empty':
        first = build_llm_response(content=' ')
    elif bad in {'length', 'content_filter', 'missing_finish'}:
        first.choices[0].finish_reason = None if bad == 'missing_finish' else bad
    model.chat.completions.queue(first, second, build_llm_response(tool_calls=[decision(proposal_id, 'CONFIRM')]))
    request = later(ManagedTurnRequest.model_validate(value), 'Да, подтверждаю запись.')
    result = (await execute(api_client, request.model_dump(mode='json'))).json()
    assert result['status'] == 'FAILED'
    assert len(model.chat.completions.calls) == expected_calls + 1  # includes original preparation
    assert calls['execute'] == 0 and not effects.permit_requests
    async with db_session() as db:
        plan = await db.get(BusinessPlan, UUID(proposal_id))
        operation = (await db.scalars(select(BusinessOperation))).one()
        assert plan.proposal_state == 'PREPARED'
        assert operation.status == 'RESERVED' and operation.attempt_count == 0


@pytest.mark.integration
async def test_text_pending_prose_keeps_existing_single_request_failure(api_client, db_session, monkeypatch):
    """The Voice compatibility repair does not alter text provider behavior."""
    value, proposal_id, _, calls, effects, model = await prepared(
        api_client, db_session, monkeypatch, channel='WEBSITE_WIDGET')
    model.chat.completions.queue(build_llm_response(content='Готово.'),
        build_llm_response(tool_calls=[decision(proposal_id, 'CONFIRM')]))
    request = later(ManagedTurnRequest.model_validate(value), 'Да, подтверждаю.')
    result = (await execute(api_client, request.model_dump(mode='json'))).json()
    assert result['status'] == 'FAILED' and result['error_code'] == 'INVALID_RESULT'
    assert len(model.chat.completions.calls) == 2 and calls['execute'] == 0
    assert not effects.permit_requests


@pytest.mark.integration
async def test_pending_prose_cannot_escape_through_sse(api_client, db_session, monkeypatch):
    """The public stream must publish only the admitted final, never raw prose."""
    from core.managed_boundary import MANAGED_PATH
    from tests.test_managed_stream_sse import sse_events
    from tests.test_managed_turns import TOKEN

    value, proposal_id, _, calls, _, model = await prepared(api_client, db_session, monkeypatch)
    request = later(ManagedTurnRequest.model_validate(value), 'Да, подтверждаю запись.')
    value = request.model_dump(mode='json')
    value.update(contract_version=2, stream_partials=True, max_partial_chars=500)
    model.chat.completions.queue(build_llm_response(content='Непроверенное утверждение: запись отменена.'),
        build_llm_response(tool_calls=[decision(proposal_id, 'CONFIRM')]))
    response = await api_client.post(MANAGED_PATH, json=value, headers={'X-Service-Token': TOKEN})
    assert response.status_code == 200
    events = sse_events(response.content)
    assert len(events) == 1 and events[0]['partial_final'] is True
    assert events[0]['status'] == 'DRAFT_READY' and calls['execute'] == 1
    assert 'Непроверенное' not in str(events)


@pytest.mark.integration
async def test_repair_rechecks_changed_effect_authority(api_client, db_session, monkeypatch):
    """Losing eligibility after prose cannot be hidden by a repaired tool call."""
    value, proposal_id, _, calls, effects, model = await prepared(api_client, db_session, monkeypatch)
    create = model.chat.completions.create

    async def lose_authority(**kwargs):
        result = await create(**kwargs)
        effects.eligible = False
        return result

    monkeypatch.setattr(model.chat.completions, 'create', lose_authority)
    model.chat.completions.queue(build_llm_response(content='Готово.'),
        build_llm_response(tool_calls=[decision(proposal_id, 'CONFIRM')]))
    request = later(ManagedTurnRequest.model_validate(value), 'Да, подтверждаю запись.')
    result = (await execute(api_client, request.model_dump(mode='json'))).json()
    assert result['status'] == 'DRAFT_READY'
    assert result['draft_text'] == ('Не удалось выполнить действие: условия изменились. '
                                    'Давайте уточним детали и попробуем другой вариант.'), result
    assert calls['execute'] == 0 and len(effects.permit_requests) == 1
    assert len(model.chat.completions.calls) == 3
    async with db_session() as db:
        operation = (await db.scalars(select(BusinessOperation))).one()
        assert operation.attempt_count == 0 and operation.status == 'FAILED'


@pytest.mark.integration
async def test_unknown_effect_after_repair_is_not_repeated(api_client, db_session, monkeypatch):
    """Losing the effect response preserves UNKNOWN instead of another attempt."""
    import httpx

    value, proposal_id, registry, calls, effects, model = await prepared(api_client, db_session, monkeypatch)
    adapter = registry.get('chotacrm').adapter
    transport = adapter.client.transport

    async def lose_receipt(request):
        response = await transport.handle_async_request(request)
        if request.url.path.endswith('/execute/booking.create'):
            raise httpx.ReadError('Synthetic response lost', request=request)
        return response

    adapter.client.transport = httpx.MockTransport(lose_receipt)
    model.chat.completions.queue(build_llm_response(content='Готово.'),
        build_llm_response(tool_calls=[decision(proposal_id, 'CONFIRM')]),
        build_llm_response(tool_calls=[decision(proposal_id, 'CONFIRM')]))
    request = later(ManagedTurnRequest.model_validate(value), 'Да, подтверждаю запись.')
    result = (await execute(api_client, request.model_dump(mode='json'))).json()
    assert result['status'] == 'UNKNOWN'
    assert len(model.chat.completions.calls) == 3 and calls['execute'] == 1
    assert len(effects.permit_requests) == 1
    async with db_session() as db:
        operation = (await db.scalars(select(BusinessOperation))).one()
        assert operation.attempt_count == 1 and operation.status == 'UNKNOWN'
