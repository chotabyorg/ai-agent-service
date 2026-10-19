"""Reproduce the five-call cancellation failure with real native persistence."""
import json
from uuid import uuid4

import httpx
import pytest

from core.config import get_settings
from services.managed_runtime import ManagedRuntime
from tests.business_effect_helpers import open_business_runtime
from tests.fakes import ScriptedLLMClient, build_llm_response
from tests.test_business_runtime import later, make_request
from tests.test_chotacrm_adapter import crm_settings  # noqa: F401
from tests.test_pending_proposal_decision import decision, run_core
from tests.test_recent_booking_context import completed


@pytest.mark.asyncio
async def test_cancel_prose_repairs_to_one_native_effect_without_extending_deadline(
        db_session, crm_settings, monkeypatch):
    """Create, prepare cancellation, then allow exactly one cancellation POST."""
    import tests.test_chotacrm_adapter as fixture

    monkeypatch.setattr(fixture, 'make_request', lambda agent: make_request(agent, channel='VOICE',
        messages=[dict(id=uuid4(), sequence=1, actor='CUSTOMER', direction='INBOUND',
            content_type='TEXT', text='Запишите меня, Влад. Телефон +375291112233.')]))
    # completed() reuses the production business runtime and disposable database.
    _, current, registry, provider = await completed(db_session, crm_settings)
    adapter = current.registration.adapter
    transport = adapter.client.transport

    async def cancellation(request):
        if request.url.path.endswith('/execute/booking.cancel'):
            body = json.loads(request.content)
            provider['posts'] += 1
            created = next(iter(provider['receipts'].values()))['data']
            receipt = dict(status='OK', operation_ref='cancel_receipt',
                operation_id=body['operation']['id'], effect='APPLIED',
                data={**created, 'status': 'CANCELLED', 'revision': '2'})
            provider['receipts'][body['operation']['id']] = receipt
            return httpx.Response(200, json=receipt)
        response = await transport.handle_async_request(request)
        if request.url.path.endswith('/facts/booking.cancel'):
            body = json.loads(response.content)
            summary = body['data']['summary']
            body['data']['summary'] = {**{k: summary[k] for k in ('start_at', 'end_at', 'timezone')},
                'action': 'cancel', 'status': 'CONFIRMED',
                'offering_name': 'Окрашивание', 'resource_name': 'Анна'}
            return httpx.Response(200, json=body)
        return response

    adapter.client.transport = httpx.MockTransport(cancellation)
    await current.prompt_context()
    proposal = await current.call_tool('booking_cancel', {
        'booking_ref': 'booking_test', 'expected_revision': '1'})
    assert proposal.halt_status is None and provider['posts'] == 1
    request = later(current.request, 'Да, отменяйте эту запись.')
    request_before = request.model_dump_json()
    runtime = ManagedRuntime(20, request=request)
    runtime.business = await open_business_runtime(request, current.grant, db_session, registry, execution=runtime)
    deadline = runtime.deadline
    monkeypatch.setattr(get_settings(), 'knowledge_base_enabled', False)
    model = ScriptedLLMClient()
    model.chat.completions.queue(build_llm_response(content='Запись отменена.'),
        build_llm_response(tool_calls=[decision(proposal.tool_payload['proposal_id'], 'CONFIRM')]))
    reply = await run_core(db_session, runtime, model)
    assert 'отмен' in reply.lower() and provider['posts'] == 2
    assert runtime.deadline == deadline and request.model_dump_json() == request_before
    assert len(model.chat.completions.calls) == 2
    plan = await runtime.business.repository.selected_plan(request, current.grant)
    assert plan.proposal_state == 'CONFIRMED'
    assert plan.operations[0].status == 'SUCCEEDED' and plan.operations[0].attempt_count == 1
