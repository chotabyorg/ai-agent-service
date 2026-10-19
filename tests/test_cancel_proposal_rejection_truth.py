"""A rejected cancellation proposal is never a successful CRM cancellation."""
import json
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import select

from core.config import get_settings
from models.business import BusinessOperation, BusinessPlan
from services.llm_service import LLMService
from services.managed_runtime import ManagedRuntime
from tests.business_effect_helpers import open_business_runtime
from tests.fakes import ScriptedLLMClient, build_llm_response, build_tool_call
from tests.test_business_runtime import later, make_request
from tests.test_chotacrm_adapter import crm_settings  # noqa: F401
from tests.test_pending_proposal_decision import decision, run_core
from tests.test_recent_booking_context import completed


async def cancellation_proposal(factory, settings, monkeypatch, channel, text):
    import tests.test_chotacrm_adapter as fixture

    monkeypatch.setattr(fixture, 'make_request', lambda agent: make_request(agent, channel=channel,
        messages=[dict(id=uuid4(), sequence=1, actor='CUSTOMER', direction='INBOUND',
            content_type='TEXT', text='Запишите меня, Влад. Телефон +375291112233.')]))
    _, current, registry, provider = await completed(factory, settings)
    client = current.registration.adapter.client
    original = client.transport

    async def transport(request):
        response = await original.handle_async_request(request)
        if request.url.path.endswith('/facts/booking.cancel'):
            body = json.loads(response.content)
            summary = body['data']['summary']
            body['data']['summary'] = {**{k: summary[k] for k in ('start_at', 'end_at', 'timezone')},
                'action': 'cancel', 'status': 'CONFIRMED',
                'offering_name': 'Окрашивание', 'resource_name': 'Анна'}
            return httpx.Response(200, json=body)
        return response

    client.transport = httpx.MockTransport(transport)
    await current.prompt_context()
    proposal = await current.call_tool('booking_cancel', {
        'booking_ref': 'booking_test', 'expected_revision': '1'})
    assert proposal.halt_status is None and provider['posts'] == 1
    request = later(current.request, text)
    runtime = ManagedRuntime(20, request=request)
    runtime.business = await open_business_runtime(
        request, current.grant, factory, registry, execution=runtime)
    monkeypatch.setattr(get_settings(), 'knowledge_base_enabled', False)
    return runtime, provider, proposal.tool_payload['proposal_id']


@pytest.mark.asyncio
@pytest.mark.parametrize('channel,repair', [('VOICE', False), ('VOICE', True), ('WEBSITE_WIDGET', False)])
@pytest.mark.parametrize('text', ['Нет, не отменяйте запись.', 'Да, подтверждаю отмену этой записи.'])
@pytest.mark.parametrize('intent', ['REJECT', 'CHANGE'])
async def test_retirement_never_speaks_success_or_changes_semantic_choice(
        db_session, crm_settings, monkeypatch, channel, repair, text, intent):
    """Even a misclassified yes cannot be silently upgraded to consent."""
    runtime, provider, proposal_id = await cancellation_proposal(
        db_session, crm_settings, monkeypatch, channel, text)
    request_before = runtime.request.model_dump_json()
    deadline = runtime.deadline
    model = ScriptedLLMClient()
    if repair:
        model.chat.completions.queue(build_llm_response(content='Запись уже отменена.'))
    model.chat.completions.queue(
        build_llm_response(tool_calls=[decision(proposal_id, intent)]),
        build_llm_response(content='Запись успешно отменена. Всего доброго!'))
    published = []
    original_core = LLMService._execute_core

    async def publish(value):
        published.append(value)

    async def with_partials(self, *args, **kwargs):
        kwargs['partial_hook'] = publish
        return await original_core(self, *args, **kwargs)

    monkeypatch.setattr(LLMService, '_execute_core', with_partials)
    reply = await run_core(db_session, runtime, model)
    assert reply == ('Отмену записи не выполняю.' if intent == 'REJECT' else
        'Отмену записи не выполняю. Уточните, пожалуйста, что вы хотите изменить.')
    assert not published
    assert len(model.chat.completions.calls) == 1 + int(repair) + int(intent == 'CHANGE')
    assert len(model.chat.completions._responses) == int(intent == 'REJECT')
    assert provider['posts'] == 1
    assert runtime.business._proposal_decision == (UUID(proposal_id), intent)
    assert runtime.request.model_dump_json() == request_before and runtime.deadline == deadline
    async with db_session() as db:
        plan = await db.get(BusinessPlan, UUID(proposal_id))
        operation = await db.scalar(select(BusinessOperation).where(BusinessOperation.plan_id == plan.id))
        assert plan.proposal_state == 'CANCELLED'
        assert operation.capability == 'booking.cancel'
        assert operation.status == 'RESERVED' and operation.attempt_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('channel', ['VOICE', 'WEBSITE_WIDGET'])
async def test_change_can_prepare_replacement_but_cannot_execute_it(
        db_session, crm_settings, monkeypatch, channel):
    runtime, provider, proposal_id = await cancellation_proposal(
        db_session, crm_settings, monkeypatch, channel,
        'Проверьте заново условия отмены этой записи и подготовьте новое предложение.')
    replacement = build_tool_call('replacement', 'booking_cancel', json.dumps({
        'booking_ref': 'booking_test', 'expected_revision': '1'}))
    replacement.type = 'function'
    model = ScriptedLLMClient()
    model.chat.completions.queue(
        build_llm_response(tool_calls=[decision(proposal_id, 'CHANGE')]),
        build_llm_response(tool_calls=[replacement]))
    reply = await run_core(db_session, runtime, model)
    assert reply.endswith('Подтверждаете?') and 'успешно отменена' not in reply
    assert provider['posts'] == 1 and len(model.chat.completions.calls) == 2
    async with db_session() as db:
        assert (await db.get(BusinessPlan, UUID(proposal_id))).proposal_state == 'CANCELLED'
    plan = await runtime.business.repository.selected_plan(runtime.request, runtime.business.grant)
    assert plan.id != UUID(proposal_id) and plan.proposal_state == 'PREPARED'
    assert plan.confirmed_turn_id is None
    assert plan.operations[0].status == 'RESERVED' and plan.operations[0].attempt_count == 0
