"""Canonical confirmation finishes from its receipt before model continuation."""
import json
from dataclasses import replace

import httpx

import pytest

from services.business_runtime import BusinessRuntime
from services.business_registry import BusinessRegistry
from business_capabilities.chotacrm_adapter import ChotaCRMAdapter
from services.llm_service import LLMService
from services.managed_runtime import ManagedRuntime
from services.managed_turn_service import execute_managed_owned
from tests.fakes import ScriptedLLMClient, build_llm_response, build_tool_call
from tests.test_business_runtime import later
from tests.test_chotacrm_adapter import crm_settings, setup


def decision(proposal_id, intent, step):
    call = build_tool_call(f'decision_{step}', 'business_decide_proposal', json.dumps({
        'proposal_id': proposal_id, 'intent': intent}))
    call.type = 'function'
    return build_llm_response(tool_calls=[call])


async def setup_booking(factory, settings, capability):
    original, registry, provider, args = await setup(factory, settings)
    if capability == 'booking.create':
        return original, registry, provider, args
    adapter = registry['chotacrm'].adapter

    async def handle(request):
        if request.url.path.endswith('/execute/' + capability):
            response = await adapter.client.transport.handle_async_request(httpx.Request(
                request.method, request.url.copy_with(path=request.url.path.replace(capability, 'booking.create')),
                headers=request.headers, content=request.content))
            data = response.json()
            if capability == 'booking.cancel':
                data['data']['status'] = 'CANCELLED'
            provider['receipts'][data['operation_id']] = data
            return httpx.Response(200, json=data)
        response = await adapter.client.transport.handle_async_request(request)
        if request.url.path.endswith('/facts/booking.cancel'):
            data = response.json()
            summary = data['data']['summary']
            data['data']['summary'] = {key: summary[key] for key in ['start_at', 'end_at', 'timezone']}
            data['data']['summary'].update(status='CONFIRMED', action='cancel',
                offering_name='Окрашивание', resource_name='Анна')
            return httpx.Response(200, json=data)
        return response

    registry = BusinessRegistry((replace(registry['chotacrm'], adapter=ChotaCRMAdapter(
        settings, factory, transport=httpx.MockTransport(handle))),))
    runtime = await BusinessRuntime.open(original.request, original.grant, factory, registry)
    operation_args = dict(booking_ref='booking_test', expected_revision='1')
    if capability == 'booking.reschedule':
        operation_args.update(resource_ref=args['resource_ref'], start_at=args['start_at'])
    return runtime, registry, provider, operation_args


@pytest.mark.asyncio
@pytest.mark.parametrize('acceptance', ['+', 'ага', '👍'])
@pytest.mark.parametrize('capability,wording', [('booking.create', 'подтверждена'), ('booking.reschedule', 'перенесена'), ('booking.cancel', 'отменена')])
async def test_confirm_does_not_consume_later_conflicting_model_completion(
        db_session, crm_settings, monkeypatch, acceptance, capability, wording):
    original, registry, provider, args = await setup_booking(db_session, crm_settings, capability)
    proposal = await original.call_tool(capability.replace('.', '_'), args)
    request = later(original.request, acceptance)
    client = ScriptedLLMClient()
    proposal_id = proposal.tool_payload['proposal_id']
    client.chat.completions.queue(decision(proposal_id, 'CONFIRM', 1), decision(proposal_id, 'CHANGE', 2))
    monkeypatch.setattr(LLMService, '_get_agent_api_key', lambda *_: 'synthetic')
    monkeypatch.setattr(LLMService, '_create_llm_client', lambda *_args, **_kwargs: client)

    async def db_provider():
        async with db_session() as session:
            yield session

    result = await execute_managed_owned(request, original.grant,
        ManagedRuntime(20, request=request), db_provider, db_session, registry)
    assert provider['posts'] == 1
    assert [ref.outcome for ref in result.operation_refs] == ['SUCCEEDED']
    assert result.status == 'DRAFT_READY', (result.status, result.error_code)
    assert f'Запись была {wording}' in result.draft_text
    assert 'Окрашивание' in result.draft_text and '18:00' in result.draft_text
    assert len(client.chat.completions.calls) == 1
    replay = await execute_managed_owned(request, original.grant,
        ManagedRuntime(20, request=request), db_provider, db_session, registry)
    assert replay.status == 'DRAFT_READY' and replay.draft_text == result.draft_text
    assert replay.operation_refs == result.operation_refs
    assert provider['posts'] == 1 and len(client.chat.completions.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['missing', 'hash', 'operation', 'turn', 'input', 'unresolved', 'unknown', 'unavailable'])
async def test_first_confirmation_requires_fresh_coherent_receipt_gate(
        db_session, crm_settings, monkeypatch, corruption):
    import copy
    from uuid import uuid4
    from schemas.business import fingerprint

    original, registry, provider, args = await setup(db_session, crm_settings)
    proposal = await original.call_tool('booking_create', args)
    customer = await BusinessRuntime.open(later(original.request, '+'), original.grant, db_session, registry)
    old_gate = customer.last_gate
    get_gate = customer.repository.result_gate
    observed = []

    async def corrupt_gate(request, grant):
        gate = await get_gate(request, grant)
        observed.append(gate.disposition)
        if gate.disposition != 'ALL_SUCCEEDED':
            return gate
        if corruption == 'unavailable':
            raise OSError('synthetic result gate unavailable')
        if corruption == 'unresolved':
            return replace(gate, scope_unresolved=True)
        if corruption == 'unknown':
            return replace(gate, disposition='UNKNOWN')
        plan = gate.plan
        if corruption == 'turn':
            return replace(gate, plan=replace(plan, confirmed_turn_id=uuid4()))
        if corruption == 'input':
            return replace(gate, plan=replace(plan, confirmation_input_ids=(str(uuid4()),)))
        operation = plan.operations[0]
        evidence, digest = copy.deepcopy(operation.receipt_evidence), operation.receipt_fingerprint
        if corruption == 'missing':
            evidence = None
        elif corruption == 'hash':
            digest = '0' * 64
        else:
            evidence['evidence']['result']['operation_id'] = str(uuid4())
            digest = fingerprint('chota.business.receipt.v1', evidence)
        return replace(gate, plan=replace(plan, operations=(replace(operation,
            receipt_evidence=evidence, receipt_fingerprint=digest),)))

    monkeypatch.setattr(customer.repository, 'result_gate', corrupt_gate)
    result = await customer.call_tool('business_decide_proposal', {
        'proposal_id': proposal.tool_payload['proposal_id'], 'intent': 'CONFIRM'})
    assert 'ALL_SUCCEEDED' in observed and old_gate.disposition != 'ALL_SUCCEEDED'
    assert result.halt_status == 'UNKNOWN' and result.safe_draft is None
    assert provider['posts'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('intent,message', [('CHANGE', 'да, только на час позже'), ('REJECT', 'нет')])
async def test_nonconfirmation_still_allows_normal_model_continuation(
        db_session, crm_settings, monkeypatch, intent, message):
    original, registry, provider, args = await setup(db_session, crm_settings)
    proposal = await original.call_tool('booking_create', args)
    request = later(original.request, message)
    client = ScriptedLLMClient()
    client.chat.completions.queue(decision(proposal.tool_payload['proposal_id'], intent, 1),
        build_llm_response(content='Уточните новое время.' if intent == 'CHANGE' else 'Предложение отменено.'))
    monkeypatch.setattr(LLMService, '_get_agent_api_key', lambda *_: 'synthetic')
    monkeypatch.setattr(LLMService, '_create_llm_client', lambda *_args, **_kwargs: client)

    async def db_provider():
        async with db_session() as session:
            yield session

    result = await execute_managed_owned(request, original.grant,
        ManagedRuntime(20, request=request), db_provider, db_session, registry)
    assert result.status == 'DRAFT_READY' and provider['posts'] == 0
    assert len(client.chat.completions.calls) == 2
