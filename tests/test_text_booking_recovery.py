"""Buffered model recovery and pre-dispatch cancellation correction stay bounded."""
import json
from uuid import uuid4

import httpx
import pytest
from openai import APIStatusError
from sqlalchemy import select

from models.business import BusinessOperation
from schemas.business import BusinessToolResult
from services.managed_tools import ManagedExecutionError
from tests.fakes import ScriptedLLMClient, build_llm_response
from tests.test_booking_workflow import tool
from tests.test_chotacrm_adapter import crm_settings  # noqa: F401
from tests.test_chotacrm_managed_proposal_errors import install_managed_crm
from tests.test_managed_turns import execute
from tests.test_pending_proposal_decision import run_core
from tests.test_preparation_schema_correction import local_runtime, preparation


def failure(status):
    return APIStatusError('private-provider-sentinel', response=httpx.Response(status,
        request=httpx.Request('POST', 'https://synthetic.invalid')), body={'secret': 'private-provider-sentinel'})


@pytest.mark.integration
@pytest.mark.parametrize('status', [500, 502, 503, 504])
async def test_text_retries_only_failed_model_request_without_replaying_read(
        api_client, db_session, monkeypatch, status):
    value, args, _, calls, _, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, channel='WEBSITE_WIDGET')
    model.chat.completions.queue(tool('customer_find', {'identity': args['customer']['identity']}, 'read'),
        failure(status), build_llm_response(content='Какое время вам удобно?'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY', result
    assert calls['lookup'] == 1 and calls['execute'] == 0
    assert len(model.chat.completions.calls) == 3
    assert model.chat.completions.calls[1] == model.chat.completions.calls[2]


@pytest.mark.integration
@pytest.mark.parametrize('channel,status,expected_calls', [
    ('WEBSITE_WIDGET', 400, 1), ('WEBSITE_WIDGET', 401, 1),
    ('WEBSITE_WIDGET', 429, 2), ('VOICE', 502, 2), ('WEBSITE_WIDGET', 502, 2)])
async def test_model_retry_is_once_and_not_for_permanent_client_errors(
        api_client, db_session, monkeypatch, channel, status, expected_calls):
    """Bound transient recovery globally while preserving permanent refusals."""
    value, _, _, calls, _, model = await install_managed_crm(
        api_client, db_session, monkeypatch, 200, {}, channel=channel)
    model.chat.completions.queue(failure(status), failure(status), build_llm_response(content='never used'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'FAILED' and result['error_code'] == 'MODEL_ERROR'
    assert len(model.chat.completions.calls) == expected_calls and calls['execute'] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('name', ['booking_cancel', 'booking_reschedule', 'booking_create'])
@pytest.mark.parametrize('invalid', ['json', 'shape'])
async def test_booking_preparation_correction_dispatches_only_corrected_arguments(
        db_session, crm_settings, monkeypatch, name, invalid):
    business, runtime, provider, create_args = await local_runtime(db_session, crm_settings, monkeypatch)
    valid = {'booking_ref': 'synthetic_booking', 'expected_revision': 'synthetic_revision'}
    if name == 'booking_reschedule':
        valid.update(resource_ref='synthetic_resource', start_at='2026-10-19T11:00:00Z')
    if name == 'booking_create': valid = create_args
    dispatched = []
    async def capture(tool_name, arguments):
        dispatched.append((tool_name, arguments))
        return BusinessToolResult(safe_draft='Предложение требует подтверждения.')
    monkeypatch.setattr(business, 'call_tool', capture)
    bad = preparation(name, {}, 'bad')
    if invalid == 'json': bad.choices[0].message.tool_calls[0].function.arguments = '{private-sentinel'
    model = ScriptedLLMClient()
    model.chat.completions.queue(bad, preparation(name, valid, 'corrected'))
    assert 'подтверждения' in await run_core(db_session, runtime, model)
    assert dispatched == [(name, valid)] and provider['posts'] == 0
    repair = model.chat.completions.calls[1]
    assert 'tool_choice' not in repair
    assert [t['function']['name'] for t in repair['tools']] == [name]
    feedback = next(m for m in repair['messages'] if m.get('role') == 'tool' and m.get('tool_call_id') == 'bad')
    assert 'private-sentinel' not in feedback['content']
    async with db_session() as db:
        assert await db.scalar(select(BusinessOperation)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('batch', [False, True])
async def test_cancellation_correction_does_not_allow_second_repair_or_an_effect_in_same_batch(
        db_session, crm_settings, monkeypatch, batch):
    business, runtime, provider, _ = await local_runtime(db_session, crm_settings, monkeypatch)
    dispatched = []
    async def capture(*args): dispatched.append(args)
    monkeypatch.setattr(business, 'call_tool', capture)
    model = ScriptedLLMClient()
    first = preparation('booking_cancel', {}, 'bad')
    if batch:
        first.choices[0].message.tool_calls.insert(0, preparation('customer_create', {
            'display_name': 'Synthetic', 'identities': [{'kind': 'PHONE', 'value': '+12025550112'}]},
            'preceding-write').choices[0].message.tool_calls[0])
    model.chat.completions.queue(first,
                                preparation('booking_cancel', {}, 'bad-again'))
    with pytest.raises(ManagedExecutionError):
        await run_core(db_session, runtime, model)
    assert len(model.chat.completions.calls) == (1 if batch else 2)
    assert dispatched == [] and provider['posts'] == 0


@pytest.mark.asyncio
async def test_retry_rechecks_authority_and_does_not_reset_deadline(db_session, crm_settings, monkeypatch):
    business, runtime, _, _ = await local_runtime(db_session, crm_settings, monkeypatch)
    model = ScriptedLLMClient()
    original = model.chat.completions.create
    failed = False
    deadline = runtime.deadline
    async def create(**kwargs):
        nonlocal failed
        try: return await original(**kwargs)
        finally: failed = True
    monkeypatch.setattr(model.chat.completions, 'create', create)
    guard = business.execution_guard
    async def guarded(**kwargs):
        if failed: return BusinessToolResult(halt_status='UNKNOWN', error_code='TOOL_EXECUTION_UNKNOWN')
        return await guard(**kwargs)
    monkeypatch.setattr(business, 'execution_guard', guarded)
    model.chat.completions.queue(failure(502), build_llm_response(content='never used'))
    with pytest.raises(ManagedExecutionError, match='TOOL_EXECUTION_UNKNOWN'):
        await run_core(db_session, runtime, model)
    assert len(model.chat.completions.calls) == 1 and runtime.deadline == deadline


@pytest.mark.asyncio
async def test_no_proposal_does_not_advertise_rejection_of_a_confirmed_booking(
        db_session, crm_settings, monkeypatch):
    _, runtime, _, _ = await local_runtime(db_session, crm_settings, monkeypatch)
    model = ScriptedLLMClient()
    model.chat.completions.queue(build_llm_response(content='Уточняю запись.'))
    await run_core(db_session, runtime, model)
    names = {t['function']['name'] for t in model.chat.completions.calls[0]['tools']}
    assert 'booking_cancel' in names and 'business_decide_proposal' not in names


def test_failure_observation_contains_only_bounded_status_and_time(monkeypatch):
    from services.completion_timing import CompletionTiming
    from services.managed_completion import observe_model_failure
    from tests.test_business_runtime import make_request
    observed = []
    monkeypatch.setattr('services.managed_completion.emit_completion_diagnostic', observed.append)
    request = make_request(uuid4())
    observe_model_failure(failure(502), request=request, step=2, timing=CompletionTiming())
    assert len(observed) == 1
    assert observed[0].http_status == 502 and observed[0].branch == 'MODEL_FAILURE'
    assert observed[0].model_ms >= 0 and 'private-provider-sentinel' not in repr(observed)
