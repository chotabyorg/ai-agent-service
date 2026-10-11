"""One pre-runtime shape correction; never a retry of business execution."""
import json

import httpx
import pytest

from core.config import get_settings
from services.managed_runtime import ManagedRuntime
from services.managed_tools import ManagedExecutionError
from services.managed_preparation import PreparationSchema
from schemas.business import BusinessSafetyError
from tests.fakes import ScriptedLLMClient, build_llm_response, build_tool_call
from tests.test_chotacrm_adapter import crm_settings, setup
from tests.test_pending_proposal_decision import run_core


def preparation(name, args, call_id):
    call = build_tool_call(call_id, name, json.dumps(args))
    call.type = 'function'
    return build_llm_response(tool_calls=[call])


async def local_runtime(factory, settings, monkeypatch):
    business, _, provider, args = await setup(factory, settings)
    runtime = ManagedRuntime(20, request=business.request)
    runtime.business, runtime.collector = business, business.collector
    business.execution = runtime
    monkeypatch.setattr(get_settings(), 'knowledge_base_enabled', False)
    return business, runtime, provider, args


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['business_propose', 'booking_create'])
async def test_stringified_actions_are_corrected_once_before_real_proposal_preparation(
        db_session, crm_settings, monkeypatch, name):
    business, runtime, provider, args = await local_runtime(db_session, crm_settings, monkeypatch)
    actions = [{'capability': 'booking.create', 'arguments': args}]
    model = ScriptedLLMClient()
    valid = {'actions': actions} if name == 'business_propose' else args
    invalid = {'actions': json.dumps(actions)} if name == 'business_propose' else {**args, 'customer': None}
    model.chat.completions.queue(preparation(name, invalid, 'bad'), preparation(name, valid, 'corrected'))
    draft = await run_core(db_session, runtime, model)
    plan = await business.repository.selected_plan(business.request, business.grant)
    assert plan.proposal_state == 'PREPARED' and plan.operations[0].status == 'RESERVED'
    assert 'Подтверждаете' in draft and provider['posts'] == 0
    assert len(model.chat.completions.calls) == 2
    assert model.chat.completions.calls[1]['tool_choice'] == {
        'type': 'function', 'function': {'name': name}}


@pytest.mark.asyncio
@pytest.mark.parametrize('bad_reply', ['text', 'wrong_tool', 'multiple', 'same_id', 'invalid_again', 'bad_json'])
async def test_correction_cannot_be_repeated_or_changed_into_another_action(
        db_session, crm_settings, monkeypatch, bad_reply):
    business, runtime, provider, args = await local_runtime(db_session, crm_settings, monkeypatch)
    response = preparation('booking_create', args, 'corrected')
    calls = response.choices[0].message.tool_calls
    if bad_reply == 'text':
        response = build_llm_response(content='Готово!')
    elif bad_reply == 'wrong_tool':
        calls[0].function.name = 'customer_create'
    elif bad_reply == 'multiple':
        calls.append(build_tool_call('other', 'catalog_search', '{}'))
    elif bad_reply == 'same_id':
        calls[0].id = 'bad'
    elif bad_reply == 'invalid_again':
        calls[0].function.arguments = '{}'
    elif bad_reply == 'bad_json':
        calls[0].function.arguments = '{'
    model = ScriptedLLMClient()
    model.chat.completions.queue(preparation('booking_create', {}, 'bad'), response,
        preparation('booking_create', args, 'never_called'))
    with pytest.raises(ManagedExecutionError):
        await run_core(db_session, runtime, model)
    assert await business.repository.selected_plan(business.request, business.grant) is None
    assert provider['posts'] == 0 and len(model.chat.completions.calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('invalid_first', [False, True])
async def test_invalid_preparation_in_multi_call_response_precedes_any_dispatch(
        db_session, crm_settings, monkeypatch, invalid_first):
    business, runtime, provider, _ = await local_runtime(db_session, crm_settings, monkeypatch)
    seen = []
    original = business.call_tool
    async def capture(*args):
        seen.append(args[0])
        return await original(*args)
    monkeypatch.setattr(business, 'call_tool', capture)
    bad = preparation('booking_create', {}, 'bad').choices[0].message.tool_calls[0]
    other = build_tool_call('other', 'customer_create', json.dumps({
        'display_name': 'Synthetic', 'identity': {'kind': 'PHONE', 'value': '+12025550112'}}))
    model = ScriptedLLMClient()
    model.chat.completions.queue(build_llm_response(tool_calls=[bad, other] if invalid_first else [other, bad]))
    with pytest.raises(ManagedExecutionError, match='TOOL_SETUP_FAILED'):
        await run_core(db_session, runtime, model)
    assert seen == [] and provider['posts'] == 0 and len(model.chat.completions.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize('error', ['denied', 'unknown', 'timeout'])
async def test_valid_arguments_runtime_failure_never_gets_schema_correction(
        db_session, crm_settings, monkeypatch, error):
    business, runtime, provider, args = await local_runtime(db_session, crm_settings, monkeypatch)
    async def fail(*_):
        if error == 'timeout':
            raise ManagedExecutionError('EXECUTION_TIMEOUT')
        raise BusinessSafetyError('SYNTHETIC_DENIAL', processing_status='UNKNOWN' if error == 'unknown' else 'FAILED')
    monkeypatch.setattr(business, 'call_tool', fail)
    model = ScriptedLLMClient()
    model.chat.completions.queue(preparation('booking_create', args, 'first'),
        preparation('booking_create', args, 'never_called'))
    with pytest.raises(ManagedExecutionError):
        await run_core(db_session, runtime, model)
    assert provider['posts'] == 0 and len(model.chat.completions.calls) == 1


@pytest.mark.asyncio
async def test_retained_success_prohibits_preparation_correction(db_session, crm_settings, monkeypatch):
    from tests.test_completed_confirmation_replay import confirmed
    business, _, provider = await confirmed(db_session, crm_settings)
    runtime = ManagedRuntime(20, request=business.request)
    runtime.business, runtime.collector = business, business.collector
    business.execution = runtime
    monkeypatch.setattr(get_settings(), 'knowledge_base_enabled', False)
    model = ScriptedLLMClient()
    model.chat.completions.queue(preparation('booking_create', {}, 'bad'))
    with pytest.raises(ManagedExecutionError, match='TOOL_SETUP_FAILED'):
        await run_core(db_session, runtime, model)
    assert provider['posts'] == 1 and len(model.chat.completions.calls) == 1
    assert business.collector.snapshot()[0].outcome == 'SUCCEEDED'


@pytest.mark.asyncio
async def test_correction_message_and_choice_still_obey_context_budget(db_session, crm_settings, monkeypatch):
    _, runtime, provider, _ = await local_runtime(db_session, crm_settings, monkeypatch)
    model = ScriptedLLMClient()
    model.chat.completions.queue(preparation('booking_create', {}, 'bad'))
    create = model.chat.completions.create
    async def limit_after_response(**kwargs):
        result = await create(**kwargs)
        envelope = {key: value for key, value in kwargs.items() if key != 'extra_headers'}
        monkeypatch.setattr(get_settings(), 'managed_ai_context_bytes',
            len(json.dumps(envelope, ensure_ascii=False).encode()) + 10)
        return result
    monkeypatch.setattr(model.chat.completions, 'create', limit_after_response)
    with pytest.raises(ManagedExecutionError, match='CONTEXT_LIMIT'):
        await run_core(db_session, runtime, model)
    assert provider['posts'] == 0 and len(model.chat.completions.calls) == 1


@pytest.mark.asyncio
async def test_valid_multiple_reads_remain_unforced(db_session, crm_settings, monkeypatch):
    _, runtime, provider, _ = await local_runtime(db_session, crm_settings, monkeypatch)
    model = ScriptedLLMClient()
    model.chat.completions.queue(build_llm_response(tool_calls=[
        build_tool_call('read1', 'catalog_search', '{}'), build_tool_call('read2', 'catalog_search', '{}')]),
        build_llm_response(content='Список услуг.'))
    assert await run_core(db_session, runtime, model) == 'Список услуг.'
    assert all('tool_choice' not in call for call in model.chat.completions.calls)
    assert provider['posts'] == 0


def test_feedback_has_only_fixed_rule_and_schema_owned_path_and_never_resolves_refs():
    schema = PreparationSchema({'type': 'object', 'properties': {'actions': {'type': 'array'}},
        'required': ['actions'], 'additionalProperties': False})
    feedback = schema.feedback({'actions': 'PRIVATE_SENTINEL'})
    assert feedback['error'] == {'code': 'TOOL_ARGUMENT_VALIDATION_FAILED', 'path': '/actions', 'rule': 'type'}
    assert 'PRIVATE_SENTINEL' not in json.dumps(feedback)
    extra = schema.feedback({'actions': [], 'PRIVATE_KEY_SENTINEL': 'PRIVATE_VALUE_SENTINEL'})
    assert 'PRIVATE_' not in json.dumps(extra)
    for ref in ['https://unreachable.invalid/schema', 'file:///private/secret', '#/$defs/local']:
        with pytest.raises(ManagedExecutionError, match='TOOL_SETUP_FAILED'):
            PreparationSchema({'$ref': ref})


def test_customer_xor_remains_strict_and_feedback_does_not_coerce():
    from business_capabilities.registry import WIRE_REGISTRY
    schema = PreparationSchema(WIRE_REGISTRY['booking_create'].tool()['function']['parameters'])
    arguments = {'resource_ref': 'resource', 'offering_ref': 'offering', 'start_at': '2030-01-01T10:00:00Z'}
    assert schema.feedback(arguments)['error']['rule'] == 'oneOf'
    assert set(arguments) == {'resource_ref', 'offering_ref', 'start_at'}


@pytest.mark.asyncio
@pytest.mark.parametrize('reuse_previous_id', [False, True])
async def test_empty_customer_ref_with_inline_customer_after_reads_requires_fresh_correction_id(
        db_session, crm_settings, monkeypatch, reuse_previous_id):
    business, runtime, provider, args = await local_runtime(db_session, crm_settings, monkeypatch)
    client = business.registration.adapter.client
    transport = client.transport
    async def handle(request):
        if request.url.path.endswith('/execute/booking.check_availability'):
            return httpx.Response(200, json={'status': 'OK', 'data': {
                'available': True, 'start_at': args['start_at'], 'end_at': args['start_at'],
                'checked_at': args['start_at'], 'reservation': False}})
        return await transport.handle_async_request(request)
    monkeypatch.setattr(client, 'transport', httpx.MockTransport(handle))
    seen = []
    call_tool = business.call_tool
    async def capture(name, arguments):
        seen.append(name)
        return await call_tool(name, arguments)
    monkeypatch.setattr(business, 'call_tool', capture)
    availability = {key: args[key] for key in ('resource_ref', 'offering_ref', 'start_at')}
    malformed = {'actions': [{'capability': 'booking.create', 'arguments': {**args, 'customer_ref': ''}}]}
    valid = {'actions': [{'capability': 'booking.create', 'arguments': args}]}
    model = ScriptedLLMClient()
    model.chat.completions.queue(
        build_llm_response(tool_calls=[build_tool_call('catalog', 'catalog_search', '{}')]),
        build_llm_response(tool_calls=[build_tool_call('availability', 'booking_check_availability', json.dumps(availability))]),
        preparation('business_propose', malformed, 'bad'),
        preparation('business_propose', valid, 'catalog' if reuse_previous_id else 'corrected'))
    if reuse_previous_id:
        with pytest.raises(ManagedExecutionError, match='INVALID_RESULT'):
            await run_core(db_session, runtime, model)
        assert await business.repository.selected_plan(business.request, business.grant) is None
        assert seen == ['catalog_search', 'booking_check_availability']
    else:
        assert 'Подтверждаете' in await run_core(db_session, runtime, model)
        plan = await business.repository.selected_plan(business.request, business.grant)
        assert plan.proposal_state == 'PREPARED' and plan.operations[0].status == 'RESERVED'
        assert seen == ['catalog_search', 'booking_check_availability', 'business_propose']
    assert provider['posts'] == 0 and len(model.chat.completions.calls) == 4
    assert 'tool_choice' not in model.chat.completions.calls[2]
    assert model.chat.completions.calls[3]['tool_choice']['function']['name'] == 'business_propose'
