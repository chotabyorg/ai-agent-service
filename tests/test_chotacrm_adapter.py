"""Real ledger/typed adapter with bounded HTTP fixtures; full-stack lives separately."""
from tests.business_effect_helpers import open_business_runtime
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import func, select

from core.config import Settings
from core.managed_security import ManagedGrant
from models.agent import AgentTemplate
from models.business import BusinessOperation, BusinessPlan
from models.business_connection import BusinessConnection
from services.business_registry import BusinessRegistry
from business_capabilities.chotacrm_adapter import ChotaCRMAdapter
from business_capabilities.registration import create_business_registry
from business_capabilities.registry import DEFAULTS, CAPABILITIES
from tests.test_business_runtime import make_request, later


@pytest.fixture(scope='module')
def crm_settings():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    return Settings(_env_file=None, business_capabilities_enabled=True, business_crm_key_id='synthetic',
                    business_crm_private_key=pem, business_crm_url='http://synthetic.local')


async def setup(factory, settings):
    owner, agent, connection, workspace, grant_id = (uuid4() for _ in range(5))
    async with factory() as db:
        db.add(AgentTemplate(id=agent, user_id=owner, name='Synthetic', system_prompt='test',
            llm_provider='openai', model_name='test', temperature=0, is_active=True))
        db.add(BusinessConnection(id=connection, agent_id=agent, owner_id=owner,
            workspace_id=workspace, workspace_name='Synthetic', timezone='Europe/Minsk',
            grant_id=grant_id, grant_revision=1, generation=1, active=True, enabled_capabilities=list(DEFAULTS)))
        await db.commit()
    records = dict(posts=0, lost=False, receipts={})
    start = (datetime.now(timezone.utc) + timedelta(days=2)).replace(hour=15, minute=0, second=0, microsecond=0).isoformat()
    args = dict(resource_ref='resource_test', offering_ref='offering_test', start_at=start,
                customer=dict(display_name='Влад', identity=dict(kind='PHONE', value='+375291112233')))
    async def handle(request):
        path = request.url.path
        if path.endswith('/context'):
            return httpx.Response(200, json=dict(workspace_id=str(workspace), grant_id=str(grant_id),
                revision=1, capabilities=list(DEFAULTS), timezone='Europe/Minsk'))
        if path.endswith('/execute/catalog.search'):
            return httpx.Response(200, json=dict(status='OK', data={'items':[]}))
        if path.endswith('/execute/customer.find'):
            arguments = json.loads(request.content)['arguments']
            assert 'name' not in arguments
            return httpx.Response(200, json=dict(status='OK', data={'match':'NONE','candidates':[]}))
        if '/facts/' in path:
            body = json.loads(request.content)
            return httpx.Response(200, json=dict(status='OK', data=dict(arguments=body['arguments'],
                payload_hash='a'*64, facts_hash='b'*64, summary=dict(customer='Влад', service='Окрашивание',
                    resource='Анна', start_at=start, end_at=start, timezone='Europe/Minsk', price='50.00', currency='BYN'))))
        if '/operations/' in path:
            return httpx.Response(200, json=records['receipts'][path.rsplit('/',1)[-1]])
        if path.endswith('/execute/booking.create'):
            body = json.loads(request.content)
            operation = body['operation']
            assert operation['expected_facts_hash'] == 'b'*64
            assert operation['managed_confirmation']['proposed_turn_id'] != operation['managed_confirmation']['confirmed_turn_id']
            records['posts'] += 1
            if records.get('reject'):
                value = dict(status='ERROR', operation_ref='op_rejected', operation_id=operation['id'],
                    effect='NOT_APPLIED', error=dict(code='BOOKING_CONFLICT', message='ignored provider text', retry='NEVER'))
                records['receipts'][operation['id']] = value
                if records['lost']:
                    raise httpx.ReadError('Synthetic negative response lost', request=request)
                return httpx.Response(200, json=value)
            value = dict(status='OK', operation_ref='op_receipt', operation_id=operation['id'], effect='APPLIED',
                data=dict(ref='booking_test', customer_ref='customer_test', offering_ref='offering_test',
                    resource_ref='resource_test', offering_name='Окрашивание', resource_name='Анна',
                    timezone='Europe/Minsk', start_at=start, end_at=start, status='CONFIRMED', revision='1'))
            records['receipts'][operation['id']] = value
            if records['lost']:
                raise httpx.ReadError('Synthetic response lost', request=request)
            return httpx.Response(200, json=value)
        raise AssertionError(path)
    original = create_business_registry(settings, factory)['chotacrm']
    registry = BusinessRegistry((replace(original, adapter=ChotaCRMAdapter(settings, factory,
        transport=httpx.MockTransport(handle))),))
    request = make_request(agent)
    grant = ManagedGrant(owner_id=request.owner_id, integration_id=request.integration_id,
        channel=request.channel, agent_id=agent, agent_owner_id=owner)
    runtime = await open_business_runtime(request, grant, factory, registry)
    await runtime.prompt_context()
    return runtime, registry, records, args


@pytest.mark.asyncio
async def test_proposal_tool_instructs_preparation_before_later_confirmation(db_session, crm_settings):
    runtime, _, _, _ = await setup(db_session, crm_settings)
    tools = {tool['function']['name']: tool['function'] for tool in runtime.openai_tools()}
    description = tools['business_propose']['description'].lower()
    # Check the model-facing ordering and effect boundary, not the whole wording.
    assert 'prepare and persist a proposal before asking the customer to confirm' in description
    assert 'returned frozen business summary' in description
    assert 'wait for a later customer turn' in description
    assert 'preparing a proposal does not execute provider effects' in description
    assert 'execution requires that later confirmation' in description
    assert 'one business action' in description
    assert 'same existing customer_ref' in description
    assert 'other combinations are unsupported' in description
    assert 'if lookup returns none' in description
    assert 'only after confirmation' in description
    assert 'never call customer_create first' in description
    booking_description = tools['booking_create']['description'].lower()
    assert 'if lookup returns none' in booking_description
    assert 'only after confirmation' in booking_description
    assert 'never call customer_create first' in booking_description


@pytest.mark.asyncio
@pytest.mark.parametrize('proposal_tool', ['booking_create', 'business_propose'])
async def test_reads_do_not_consume_plan_then_natural_booking_uses_frozen_aggregate(db_session, crm_settings, proposal_tool):
    runtime, registry, provider, args = await setup(db_session, crm_settings)
    result = await runtime.call_tool('catalog_search', {})
    assert result.tool_payload['status'] == 'OK'
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
    arguments = {'actions': [{'capability': 'booking.create', 'arguments': args}]} if proposal_tool == 'business_propose' else args
    proposed = await runtime.call_tool(proposal_tool, arguments)
    assert 'Окрашивание' in proposed.safe_draft and '18:00' in proposed.safe_draft
    assert 'resource_test' not in proposed.safe_draft and 'confirm ' not in proposed.safe_draft
    assert provider['posts'] == 0
    plan = await runtime.repository.selected_plan(runtime.request, runtime.grant)
    assert plan.proposal_state == 'PREPARED'
    assert len(plan.operations) == 1 and plan.operations[0].status == 'RESERVED'
    later_runtime = await open_business_runtime(later(runtime.request, '+'), runtime.grant, db_session, registry)
    await later_runtime.prompt_context()
    result = await later_runtime.call_tool('business_decide_proposal',
        dict(proposal_id=proposed.tool_payload['proposal_id'], intent='CONFIRM'))
    assert result.halt_status is None and provider['posts'] == 1
    assert result.tool_payload['results'][0]['result']['data']['ref'] == 'booking_test'


@pytest.mark.asyncio
async def test_lost_response_remains_unknown_until_lookup_without_redispatch(db_session, crm_settings):
    runtime, registry, provider, args = await setup(db_session, crm_settings)
    proposal = await runtime.call_tool('booking_create', args)
    runtime = await open_business_runtime(later(runtime.request, 'ага'), runtime.grant, db_session, registry)
    await runtime.prompt_context()
    provider['lost'] = True
    result = await runtime.call_tool('business_decide_proposal', dict(proposal_id=proposal.tool_payload['proposal_id'], intent='CONFIRM'))
    assert result.halt_status == 'UNKNOWN' and provider['posts'] == 1
    selected = await runtime.repository.selected_plan(runtime.request, runtime.grant)
    operation = selected.operations[0]
    assert operation.status == 'UNKNOWN'
    reconciled = await runtime.reconcile(operation.id)
    assert reconciled.operations[0].status == 'SUCCEEDED' and provider['posts'] == 1
    assert reconciled.operations[0].command == operation.command


@pytest.mark.asyncio
async def test_disabled_deal_and_model_authority_injection_denied(db_session, crm_settings):
    runtime, _, provider, _ = await setup(db_session, crm_settings)
    names = {t['function']['name'] for t in runtime.openai_tools()}
    assert 'deal_create' not in names and 'deal_move' not in names and 'deal_get' in names
    assert 'business_execute_plan' not in names and len(CAPABILITIES) == 18
    result = await runtime.call_tool('catalog_search', {'workspace_id':str(uuid4())})
    assert result.halt_status == 'FAILED' and provider['posts'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('lost', [False, True])
async def test_durable_negative_receipt_terminates_only_original_operation(db_session, crm_settings, lost):
    runtime, registry, provider, args = await setup(db_session, crm_settings)
    proposal = await runtime.call_tool('booking_create', args)
    runtime = await open_business_runtime(later(runtime.request, '👍'), runtime.grant, db_session, registry)
    await runtime.prompt_context()
    provider.update(reject=True, lost=lost)
    result = await runtime.call_tool('business_decide_proposal', dict(proposal_id=proposal.tool_payload['proposal_id'], intent='CONFIRM'))
    plan = await runtime.repository.selected_plan(runtime.request, runtime.grant)
    if lost:
        assert result.halt_status == 'UNKNOWN' and plan.operations[0].status == 'UNKNOWN'
        plan = await runtime.reconcile(plan.operations[0].id)
    else:
        assert result.safe_draft and 'Не удалось' in result.safe_draft
    assert plan.operations[0].status == 'FAILED' and provider['posts'] == 1
    gate = await runtime.guard_result()
    assert gate.safe_draft and gate.halt_status is None


@pytest.mark.asyncio
async def test_reconfigured_connection_cannot_confirm_old_proposal(db_session, crm_settings):
    runtime, registry, provider, args = await setup(db_session, crm_settings)
    proposal = await runtime.call_tool('booking_create', args)
    async with db_session() as db:
        row = await db.get(BusinessConnection, runtime.binding.connection_id)
        row.generation += 1
        row.enabled_capabilities = ['catalog.search']
        await db.commit()
    runtime = await open_business_runtime(later(runtime.request, '+'), runtime.grant, db_session, registry)
    await runtime.prompt_context()
    result = await runtime.call_tool('business_decide_proposal', dict(proposal_id=proposal.tool_payload['proposal_id'], intent='CONFIRM'))
    assert result.halt_status == 'FAILED' and provider['posts'] == 0


@pytest.mark.asyncio
async def test_self_invalidating_or_dependent_bundle_denied_before_effect(db_session, crm_settings):
    runtime, _, provider, args = await setup(db_session, crm_settings)
    for second in ['deal.move', 'deal.create']:
        result = await runtime.call_tool('business_propose', {'actions': [
            {'capability':'booking.create', 'arguments':args},
            {'capability':second, 'arguments':{'customer_ref':'customer_test'}}]})
        assert result.halt_status == 'FAILED' and provider['posts'] == 0
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('tool', ['booking_create', 'business_propose'])
async def test_empty_reference_with_new_customer_still_fails_before_reservation(db_session, crm_settings, tool):
    runtime, _, provider, args = await setup(db_session, crm_settings)
    # Real model failure: new-customer details plus an empty optional reference.
    # Advertising the XOR must not turn this into an accepted/normalized input.
    arguments = dict(args, customer_ref='')
    if tool == 'business_propose':
        arguments = {'actions': [{'capability': 'booking.create', 'arguments': arguments}]}
    result = await runtime.call_tool(tool, arguments)
    assert result.halt_status == 'FAILED' and result.error_code == 'TOOL_SETUP_FAILED'
    assert provider['posts'] == 0
    async with db_session() as db:
        assert await db.scalar(select(func.count()).select_from(BusinessPlan)) == 0
        assert await db.scalar(select(func.count()).select_from(BusinessOperation)) == 0
