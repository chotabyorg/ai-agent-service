"""A retained confirmed booking must not become a fresh model booking search."""
from tests.business_effect_helpers import open_business_runtime
import copy
from dataclasses import replace
from uuid import uuid4

import pytest

from schemas.business import fingerprint
from services.business_runtime import BusinessRuntime
from services.llm_service import LLMService
from services.managed_runtime import ManagedRuntime
from services.managed_turn_service import execute_managed_owned
from tests.test_business_runtime import later
from tests.test_chotacrm_adapter import crm_settings, setup


async def confirmed(factory, settings):
    original, registry, provider, args = await setup(factory, settings)
    proposal = await original.call_tool('booking_create', args)
    customer = await open_business_runtime(later(original.request, '+'), original.grant, factory, registry)
    await customer.prompt_context()
    assert customer.completed_confirmation_replay() is None
    result = await customer.call_tool('business_decide_proposal', {
        'proposal_id': proposal.tool_payload['proposal_id'], 'intent': 'CONFIRM'})
    assert result.halt_status is None and provider['posts'] == 1
    assert result.safe_draft and 'Запись была подтверждена' in result.safe_draft
    # General ALL_SUCCEEDED guard remains unchanged; only confirmation is terminal.
    assert await customer.guard_result() is None
    return customer, registry, provider


@pytest.mark.asyncio
async def test_exact_confirmation_replay_returns_retained_receipt_without_model_or_effect(
        db_session, crm_settings, monkeypatch):
    customer, registry, provider = await confirmed(db_session, crm_settings)
    before = customer.collector.snapshot()
    monkeypatch.setattr(LLMService, '_get_agent_api_key', lambda *_: 'synthetic')

    def no_model(*args, **kwargs):
        raise AssertionError('Completed confirmation replay must not create a model client')

    monkeypatch.setattr(LLMService, '_create_llm_client', no_model)

    async def db_provider():
        async with db_session() as session:
            yield session

    result = await execute_managed_owned(customer.request, customer.grant,
        ManagedRuntime(20, request=customer.request), db_provider, db_session, registry)
    assert result.status == 'DRAFT_READY' and result.error_code is None
    assert result.operation_refs == before
    assert 'Запись была подтверждена' in result.draft_text
    assert 'Окрашивание' in result.draft_text and 'Анна' in result.draft_text
    assert '18:00' in result.draft_text and 'Europe/Minsk' in result.draft_text
    assert 'booking_test' not in result.draft_text and 'customer_test' not in result.draft_text
    assert provider['posts'] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('corruption', ['missing', 'hash', 'binding', 'object', 'operation', 'shape'])
async def test_replay_never_claims_success_from_invalid_retained_receipt(
        db_session, crm_settings, corruption):
    customer, registry, provider = await confirmed(db_session, crm_settings)
    replay = await open_business_runtime(customer.request, customer.grant, db_session, registry)
    operation = replay.last_gate.plan.operations[0]
    evidence = copy.deepcopy(operation.receipt_evidence)
    digest = operation.receipt_fingerprint
    # Terminal PostgreSQL evidence is immutable. Exercise the formatter's
    # defense against invalid internal snapshots without disabling that guard.
    if corruption == 'missing':
        evidence = None
    elif corruption == 'hash':
        digest = '0' * 64
    elif corruption == 'binding':
        evidence['binding']['operation_id'] = str(uuid4())
    elif corruption == 'object':
        evidence['object_id'] = 'another_booking'
    elif corruption == 'operation':
        evidence['evidence']['result']['operation_id'] = str(uuid4())
    elif corruption == 'shape':
        del evidence['evidence']['result']['data']['start_at']
    if corruption not in {'missing', 'hash'}:
        digest = fingerprint('chota.business.receipt.v1', evidence)
    operation = replace(operation, receipt_evidence=evidence, receipt_fingerprint=digest)
    replay.last_gate = replace(replay.last_gate, plan=replace(replay.last_gate.plan, operations=(operation,)))
    result = replay.completed_confirmation_replay()
    assert result.halt_status == 'UNKNOWN' and result.safe_draft is None
    assert provider['posts'] == 1


@pytest.mark.asyncio
async def test_replay_gate_does_not_apply_to_other_turn_request_conflict_or_unresolved_scope(
        db_session, crm_settings):
    customer, registry, _ = await confirmed(db_session, crm_settings)
    replay = await open_business_runtime(customer.request, customer.grant, db_session, registry)
    gate = replay.last_gate
    assert replay.completed_confirmation_replay().safe_draft
    replay.last_gate = replace(gate, scope_unresolved=True)
    assert replay.completed_confirmation_replay() is None
    replay.last_gate = replace(gate, plan=replace(gate.plan, confirmed_turn_id=uuid4()))
    assert replay.completed_confirmation_replay() is None
    replay.last_gate = replace(gate, plan=replace(gate.plan, proposal_state='PREPARED'))
    assert replay.completed_confirmation_replay() is None
    replay.last_gate = replace(gate, plan=replace(gate.plan, confirmation_input_ids=(str(uuid4()),)))
    assert replay.completed_confirmation_replay().halt_status == 'UNKNOWN'
    # The durable gate rejects modified content even with the original Turn UUID.
    messages = list(customer.request.messages)
    messages[-1] = messages[-1].model_copy(update={'text': 'да, только на час позже'})
    changed = customer.request.model_copy(update={'messages': messages})
    replay.last_gate = await replay.repository.result_gate(changed, customer.grant)
    assert replay.last_gate.disposition == 'UNAVAILABLE'
    assert replay.completed_confirmation_replay() is None
    assert (await replay.guard_result(reload=False)).halt_status == 'UNKNOWN'
