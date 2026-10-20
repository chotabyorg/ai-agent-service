"""Real AI HTTP/ledger, five scopes sharing one binding; all external ports are synthetic."""
import asyncio
from collections import Counter
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from uuid import UUID, uuid4
from pathlib import Path
import traceback

import httpx
import pytest
from sqlalchemy import select

from models.business import BusinessOperation, BusinessPlan, BusinessScope
from schemas.managed_turn import ManagedTurnRequest
from tests.test_booking_workflow import tool
from tests.test_business_runtime import later
from tests.test_chotacrm_managed_proposal_errors import install_managed_crm
from tests.test_managed_turns import execute


CASE = ContextVar('synthetic_five_booking_case')


@pytest.mark.integration
@pytest.mark.parametrize('channel', ['WEBSITE_WIDGET', 'VOICE'])
async def test_five_scoped_booking_cancel_cycles_keep_four_progressing_while_one_is_held(
        api_client, db_session, monkeypatch, channel):
    base, arguments, registry, _, effect_port, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, channel=channel, allow_execute=True)
    transport = registry.get('chotacrm').adapter.client.transport
    cases = []
    for index in range(5):
        value = deepcopy(base)
        value.update(conversation_id=str(uuid4()), turn_id=str(uuid4()))
        value['messages'][0]['id'] = str(uuid4())
        value['selected_input_message_ids'] = [value['messages'][0]['id']]
        args = deepcopy(arguments)
        args.update(resource_ref=f'resource_{index}', offering_ref=f'offering_{index}',
                    start_at=(datetime.fromisoformat(args['start_at'])+timedelta(hours=index)).isoformat())
        args['customer'] = {'display_name': f'Клиент {index}',
            'identity': {'kind': 'PHONE', 'value': f'+37529111010{index}'}}
        value['messages'][0]['text'] = ('Запишите меня. Меня зовут '+args['customer']['display_name']+
            ', телефон '+args['customer']['identity']['value']+'.')
        cases.append(dict(index=index, request=ManagedTurnRequest.model_validate(value), arguments=args,
            stage='prepare_create', model_calls=0, finished=False, booking_ref=f'booking_{index}'))
    books, receipts, posts = {}, {}, Counter()
    entered, release, all_creates = asyncio.Event(), asyncio.Event(), asyncio.Event()
    active_creates, maximum_creates = set(), 0
    stage_counts = Counter()
    diagnostic_errors = []

    def same_create(args, case):
        expected = deepcopy(case['arguments'])
        actual = dict(args)
        assert datetime.fromisoformat(actual.pop('start_at').replace('Z', '+00:00')) == datetime.fromisoformat(expected.pop('start_at'))
        assert actual == expected

    async def provider(request):
        nonlocal maximum_creates
        case = CASE.get()
        index = case['index']
        path = request.url.path
        if path.endswith('/context'):
            return await transport.handle_async_request(request)
        body = json.loads(request.content)
        args = body['arguments']
        if path.endswith('/execute/customer.find'):
            assert args['identity'] == case['arguments']['customer']['identity']
            return httpx.Response(200, json={'status': 'OK', 'data': {'match': 'NONE', 'candidates': []}})
        if path.endswith('/execute/booking.get'):
            assert args['booking_ref'] == case['booking_ref']
            return httpx.Response(200, json={'status': 'OK', 'data': books[case['booking_ref']]})
        if '/facts/' in path:
            capability = path.rsplit('/', 1)[-1]
            start = datetime.fromisoformat(case['arguments']['start_at'])
            times = dict(start_at=start.isoformat(), end_at=(start+timedelta(hours=1)).isoformat(),
                         timezone='Europe/Minsk')
            if capability == 'booking.create':
                same_create(args, case)
                summary = dict(times, customer=args['customer']['display_name'], service=f'Услуга {index}',
                    resource=f'Мастер {index}', price='50.00', currency='BYN')
            else:
                assert capability == 'booking.cancel'
                assert args == {'booking_ref': case['booking_ref'], 'expected_revision': '1'}
                assert books[case['booking_ref']]['status'] == 'CONFIRMED'
                summary = dict(times, offering_name=f'Услуга {index}', resource_name=f'Мастер {index}',
                    status='CONFIRMED', action='cancel')
            digest = hashlib.sha256(json.dumps([capability, args], sort_keys=True).encode()).hexdigest()
            return httpx.Response(200, json={'status': 'OK', 'data': dict(arguments=args,
                payload_hash=digest, facts_hash=digest, summary=summary)})
        if '/execute/' in path:
            capability = path.rsplit('/', 1)[-1]
            assert capability in {'booking.create', 'booking.cancel'}
            confirmation = body['operation']['managed_confirmation']
            assert confirmation['proposed_turn_id'] != confirmation['confirmed_turn_id']
            assert UUID(confirmation['confirmed_turn_id']) == case['request'].turn_id
            operation_id = body['operation']['id']
            assert operation_id not in receipts, 'A provider effect must never be dispatched twice'
            posts[index, capability] += 1
            assert posts[index, capability] == 1
            if capability == 'booking.create':
                assert case['stage'] == 'confirm_create'
                same_create(args, case)
                active_creates.add(index)
                maximum_creates = max(maximum_creates, len(active_creates))
                if len(active_creates) == 5:
                    all_creates.set()
                await asyncio.wait_for(all_creates.wait(), 8)
                if index == 0:
                    entered.set()
                    await release.wait()
                start = datetime.fromisoformat(args['start_at'])
                book = dict(ref=case['booking_ref'], customer_ref=f'customer_{index}',
                    offering_ref=args['offering_ref'], resource_ref=args['resource_ref'],
                    offering_name=f'Услуга {index}', resource_name=f'Мастер {index}',
                    timezone='Europe/Minsk', start_at=start.isoformat(),
                    end_at=(start+timedelta(hours=1)).isoformat(), status='CONFIRMED', revision='1')
                books[case['booking_ref']] = book
                active_creates.remove(index)
            else:
                assert case['stage'] == 'confirm_cancel'
                assert args == {'booking_ref': case['booking_ref'], 'expected_revision': '1'}
                book = dict(books[case['booking_ref']], status='CANCELLED', revision='2')
                books[case['booking_ref']] = book
            receipt = dict(status='OK', operation_ref='receipt_'+operation_id, operation_id=operation_id,
                           effect='APPLIED', data=book)
            receipts[operation_id] = receipt
            return httpx.Response(200, json=receipt)
        raise AssertionError('Unexpected synthetic CRM path')

    async def observed_provider(request):
        try:
            return await provider(request)
        except Exception as error:
            diagnostic_errors.append(dict(kind=type(error).__name__,
                path=request.url.path.rsplit('/', 1)[-1],
                frames=[(Path(row.filename).name, row.lineno, row.name)
                        for row in traceback.extract_tb(error.__traceback__)[-4:]]))
            raise

    registry.get('chotacrm').adapter.client.transport = httpx.MockTransport(observed_provider)

    async def completion(**kwargs):
        case = CASE.get()
        case['model_calls'] += 1
        model.chat.completions.calls.append(kwargs)
        assert case['model_calls'] <= 4, 'Unexpected additional model round trip'
        stage = case['stage']
        if stage == 'prepare_create':
            return tool('booking_create', case['arguments'], f"create_{case['index']}")
        if stage == 'prepare_cancel':
            return tool('booking_cancel', {'booking_ref': case['booking_ref'], 'expected_revision': '1'},
                        f"cancel_{case['index']}")
        assert stage in {'confirm_create', 'confirm_cancel'}
        return tool('business_decide_proposal', {'proposal_id': case['proposal'], 'intent': 'CONFIRM'},
                    f"confirm_{case['index']}_{stage}")

    monkeypatch.setattr(model.chat.completions, 'create', completion)

    async def inspect(case, *, capability, status, attempt):
        async with db_session() as db:
            rows = (await db.execute(select(BusinessPlan, BusinessOperation).join(BusinessScope,
                BusinessScope.id == BusinessPlan.scope_id).join(BusinessOperation,
                BusinessOperation.plan_id == BusinessPlan.id).where(
                BusinessScope.conversation_id == case['request'].conversation_id,
                BusinessOperation.capability == capability))).all()
            assert len(rows) == 1
            plan, operation = rows[0]
            assert operation.status == status and operation.attempt_count == attempt
            if status == 'RESERVED':
                assert plan.proposal_state == 'PREPARED' and plan.confirmed_turn_id is None
                assert operation.effect_permit_id is None
                assert str(operation.id) == case['prepared_ref']
            else:
                assert operation.effect_permit_id is not None
                assert plan.confirmed_turn_id == case['request'].turn_id
                assert plan.origin_turn_id != plan.confirmed_turn_id
                assert operation.object_id == case['booking_ref']
            return str(plan.id)

    async def send(case, stage, text=None):
        case['stage'] = stage
        if text is not None:
            case['request'] = later(case['request'], text)
        response = await execute(api_client, case['request'].model_dump(mode='json', exclude_unset=True))
        result = response.json()
        assert response.status_code == 200 and result['status'] == 'DRAFT_READY', (
            response.status_code, result['status'], result['error_code'], case['index'], stage, case['model_calls'], diagnostic_errors)
        assert result['error_code'] is None
        stage_counts[stage] += 1
        return result

    async def replay(case, result):
        count = case['model_calls']
        response = await execute(api_client, case['request'].model_dump(mode='json', exclude_unset=True))
        assert response.status_code == 200
        assert response.json()['operation_refs'] == result['operation_refs']
        assert case['model_calls'] == count

    async def cycle(case):
        token = CASE.set(case)
        try:
            proposed = await send(case, 'prepare_create')
            assert 'Подтверждаете' in proposed['draft_text']
            assert len(proposed['operation_refs']) == 1 and proposed['operation_refs'][0]['outcome'] is None
            case['prepared_ref'] = proposed['operation_refs'][0]['operation_id']
            assert posts[case['index'], 'booking.create'] == 0
            case['proposal'] = await inspect(case, capability='booking.create', status='RESERVED', attempt=0)
            created = await send(case, 'confirm_create', 'Да, подтверждаю запись')
            assert 'Запись была подтверждена' in created['draft_text']
            await inspect(case, capability='booking.create', status='SUCCEEDED', attempt=1)
            await replay(case, created)
            proposed = await send(case, 'prepare_cancel', 'Отмените эту запись')
            assert 'Подтверждаете' in proposed['draft_text']
            assert len(proposed['operation_refs']) == 1 and proposed['operation_refs'][0]['outcome'] is None
            case['prepared_ref'] = proposed['operation_refs'][0]['operation_id']
            assert posts[case['index'], 'booking.cancel'] == 0
            case['proposal'] = await inspect(case, capability='booking.cancel', status='RESERVED', attempt=0)
            cancelled = await send(case, 'confirm_cancel', 'Да, подтверждаю отмену')
            assert 'Запись была отменена' in cancelled['draft_text']
            await inspect(case, capability='booking.cancel', status='SUCCEEDED', attempt=1)
            await replay(case, cancelled)
            assert case['model_calls'] == 4
            case['finished'] = True
        finally:
            CASE.reset(token)

    tasks = [asyncio.create_task(cycle(case)) for case in cases]
    gate = asyncio.create_task(entered.wait())
    try:
        done, _ = await asyncio.wait([gate, *tasks], timeout=15, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task is not gate:
                await task
        assert gate in done, 'No five-way provider overlap before the bounded barrier'
        await asyncio.wait_for(asyncio.gather(*tasks[1:]), 20)
        assert all(case['finished'] for case in cases[1:]) and not cases[0]['finished']
        assert not release.is_set() and active_creates == {0} and maximum_creates == 5
        assert sum(book['status'] == 'CANCELLED' for book in books.values()) == 4
        async with db_session() as db:
            operations = (await db.scalars(select(BusinessOperation))).all()
            assert Counter(row.status for row in operations) == {'SUCCEEDED': 8, 'DISPATCHING': 1}
        release.set()
        await asyncio.wait_for(tasks[0], 15)
        assert len(books) == 5 and all(book['status'] == 'CANCELLED' for book in books.values())
        assert len(receipts) == len(posts) == 10 and all(value == 1 for value in posts.values())
        assert stage_counts == {stage: 5 for stage in ('prepare_create', 'confirm_create', 'prepare_cancel', 'confirm_cancel')}
        assert len(effect_port.permit_requests) == 10 and len({str(row.operation_id) for row in effect_port.permit_requests}) == 10
        assert len(model.chat.completions.calls) == 20
        async with db_session() as db:
            plans = (await db.scalars(select(BusinessPlan))).all()
            operations = (await db.scalars(select(BusinessOperation))).all()
            assert len(plans) == len(operations) == 10
            assert all(row.status == 'SUCCEEDED' and row.attempt_count == 1 for row in operations)
            assert len({row.scope_id for row in operations}) == 5
    finally:
        release.set()
        if not gate.done():
            gate.cancel()
        await asyncio.gather(gate, return_exceptions=True)
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
