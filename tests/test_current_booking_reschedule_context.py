"""Fresh own-booking facts support moving the same resource without another model read."""
import json
from collections import Counter
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from models.business import BusinessOperation, BusinessPlan
from schemas.managed_turn import ManagedTurnRequest
from services.prompt_composer import RuntimeCapabilityContext
from tests.test_booking_workflow import tool
from tests.test_business_runtime import later
from tests.test_chotacrm_adapter import crm_settings as shared_crm_settings
from tests.test_chotacrm_managed_proposal_errors import install_managed_crm
from tests.test_managed_turns import execute
from tests.test_recent_booking_context import completed

crm_settings = shared_crm_settings


@pytest.mark.asyncio
@pytest.mark.parametrize('resource', ['resource_test', 'resource_fresh_7', None])
async def test_projection_retains_fresh_resource_without_inventing_a_missing_one(
        db_session, crm_settings, resource):
    _, current, _, provider = await completed(db_session, crm_settings)
    adapter = current.registration.adapter
    transport = adapter.client.transport

    async def fresh(request):
        response = await transport.handle_async_request(request)
        if request.url.path.endswith('/execute/booking.get'):
            value = response.json()
            value['data']['resource_ref'] = resource
            value['data']['revision'] = 'fresh-7'
            return httpx.Response(200, json=value)
        return response

    adapter.client.transport = httpx.MockTransport(fresh)
    context = await current.prompt_context()
    projection = context['current_booking']
    assert projection['resource_ref'] == resource
    assert projection['revision'] == 'fresh-7'
    assert RuntimeCapabilityContext.model_validate(context).current_booking.resource_ref == resource
    assert 'customer_ref' not in projection and 'customer_test' not in json.dumps(projection)
    assert provider['booking_reads'] == provider['posts'] == 1
    assert not current.collector.snapshot()
    assert await current.repository.selected_plan(current.request, current.grant) is None


@pytest.mark.integration
@pytest.mark.parametrize('channel', ['WEBSITE_WIDGET', 'VOICE'])
async def test_create_move_cancel_uses_fresh_resource_and_one_completion_per_turn(
        api_client, db_session, monkeypatch, channel):
    value, arguments, registry, _, effect_port, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, allow_execute=True, channel=channel)
    adapter = registry.get('chotacrm').adapter
    transport = adapter.client.transport
    state = dict(book=None, stage=None, proposal=None, reads=0)
    effects, operation_ids = Counter(), set()
    requested_start = datetime.fromisoformat(arguments['start_at'])+timedelta(hours=1)

    async def provider(request):
        path = request.url.path
        if path.endswith('/execute/booking.get'):
            assert json.loads(request.content)['arguments'] == {'booking_ref':'booking_test'}
            state['reads'] += 1
            return httpx.Response(200,json={'status':'OK','data':state['book']})
        if path.endswith('/facts/booking.cancel'):
            args = json.loads(request.content)['arguments']
            assert args == {'booking_ref':'booking_test','expected_revision':'2'}
            book = state['book']
            return httpx.Response(200,json={'status':'OK','data':dict(arguments=args,
                payload_hash='a'*64,facts_hash='b'*64,summary={
                    **{key:book[key] for key in ('start_at','end_at','timezone','status','offering_name','resource_name')},
                    'action':'cancel'})})
        if path.endswith('/execute/booking.reschedule') or path.endswith('/execute/booking.cancel'):
            body = json.loads(request.content)
            args, operation = body['arguments'], body['operation']
            assert operation['managed_confirmation']['proposed_turn_id'] != operation['managed_confirmation']['confirmed_turn_id']
            assert operation['id'] not in operation_ids
            operation_ids.add(operation['id'])
            capability = path.rsplit('/',1)[-1]
            effects[capability] += 1
            assert effects[capability] == 1
            assert state['book']['status'] == 'CONFIRMED'
            if capability == 'booking.reschedule':
                assert args['booking_ref'] == 'booking_test' and args['expected_revision'] == '1'
                assert args['resource_ref'] == arguments['resource_ref']
                assert datetime.fromisoformat(args['start_at'].replace('Z','+00:00')) == requested_start
                state['book'] = dict(state['book'],start_at=requested_start.isoformat(),
                    end_at=(requested_start+timedelta(hours=1)).isoformat(),revision='2')
            else:
                assert args == {'booking_ref':'booking_test','expected_revision':'2'}
                state['book'] = dict(state['book'],status='CANCELLED',revision='3')
            return httpx.Response(200,json=dict(status='OK',operation_ref='receipt_'+operation['id'],
                operation_id=operation['id'],effect='APPLIED',data=state['book']))
        response = await transport.handle_async_request(request)
        if path.endswith('/execute/booking.create'):
            effects['booking.create'] += 1
            state['book'] = response.json()['data']
        return response

    adapter.client.transport = httpx.MockTransport(provider)

    async def completion(**kwargs):
        model.chat.completions.calls.append(kwargs)
        stage = state['stage']
        if stage == 'prepare_create':
            return tool('booking_create',arguments,'create')
        if stage in {'prepare_move','prepare_cancel'}:
            projections = []
            for message in kwargs['messages']:
                if message['role'] != 'system':
                    continue
                for line in message['content'].splitlines():
                    if line.startswith('{"booking_ref":'):
                        projections.append(json.loads(line))
            assert len(projections) == 1
            fresh = projections[0]
            assert fresh['resource_ref'] == arguments['resource_ref']
            if stage == 'prepare_move':
                return tool('booking_reschedule',dict(booking_ref=fresh['booking_ref'],
                    expected_revision=fresh['revision'],resource_ref=fresh['resource_ref'],
                    start_at=requested_start.isoformat()),'move')
            return tool('booking_cancel',dict(booking_ref=fresh['booking_ref'],
                expected_revision=fresh['revision']),'cancel')
        assert stage in {'confirm_create','confirm_move','confirm_cancel'}
        return tool('business_decide_proposal',{'proposal_id':state['proposal'],'intent':'CONFIRM'},stage)

    monkeypatch.setattr(model.chat.completions,'create',completion)
    request = ManagedTurnRequest.model_validate(value)

    async def send(stage, text=None):
        nonlocal request
        if text is not None:
            request = later(request,text)
        state['stage'] = stage
        response = await execute(api_client,request.model_dump(mode='json',exclude_unset=True))
        result = response.json()
        assert response.status_code == 200 and result['status'] == 'DRAFT_READY', (stage,result)
        assert result['error_code'] is None
        return result

    for index, (action,text,wording) in enumerate((
            ('create',None,'подтверждена'),
            ('move','Перенесите эту запись на час позже, к тому же мастеру','перенесена'),
            ('cancel','Отмените эту запись','отменена'))):
        await send('prepare_'+action,text)
        assert sum(effects.values()) == index and len(effect_port.permit_requests) == index
        async with db_session() as db:
            plan = await db.scalar(select(BusinessPlan).where(BusinessPlan.origin_turn_id == request.turn_id))
            operation = await db.scalar(select(BusinessOperation).where(BusinessOperation.plan_id == plan.id))
            assert plan.proposal_state == 'PREPARED' and plan.confirmed_turn_id is None
            assert operation.status == 'RESERVED' and operation.attempt_count == 0
            assert operation.effect_permit_id is None
            state['proposal'] = str(plan.id)
        result = await send('confirm_'+action,'Да, подтверждаю')
        assert 'Запись была '+wording in result['draft_text']
        assert sum(effects.values()) == index+1
        if action != 'create':
            count = len(model.chat.completions.calls)
            replay = await execute(api_client,request.model_dump(mode='json',exclude_unset=True))
            assert replay.status_code == 200 and replay.json()['operation_refs'] == result['operation_refs']
            assert len(model.chat.completions.calls) == count and sum(effects.values()) == index+1
    assert effects == {'booking.create':1,'booking.reschedule':1,'booking.cancel':1}
    assert state['book']['status'] == 'CANCELLED' and state['reads'] == 2
    assert len(model.chat.completions.calls) == 6 and len(effect_port.permit_requests) == 3
    async with db_session() as db:
        operations = (await db.scalars(select(BusinessOperation))).all()
        assert len(operations) == 3
        assert all(row.status == 'SUCCEEDED' and row.attempt_count == 1 for row in operations)
