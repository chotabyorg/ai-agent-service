"""Closed limiter evidence must survive HTTP/SSE without changing execution."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import logging
from uuid import UUID, uuid4

import httpx
from openai import APIError, RateLimitError
import pytest

import core.managed_completion_diagnostics as diagnostics
from core.managed_security import managed_logging
from services.model_limit_observation import model_limit_observation
from services.model_retry import model_retry_delay, classify_model_failure
from tests.fakes import build_llm_response
from tests.test_managed_turns import setup, execute, model  # noqa: F401
from tests.test_managed_completion import capture_production_logger  # noqa: F401


SECRET = 'private-limit-sentinel'


def failure(*, metadata=None, headers=None, stream=False):
    body = {'error': {'code': 429, 'message': SECRET, 'metadata': metadata}}
    request = httpx.Request('POST', 'https://synthetic.invalid')
    if stream:
        return APIError(SECRET, request=request, body=body)
    return RateLimitError(SECRET, body=body,
        response=httpx.Response(429, request=request, headers=headers))


@pytest.mark.parametrize('source,expected', [
    ('openrouter_in_flight_budget', 'OPENROUTER_IN_FLIGHT_BUDGET'),
    ('openrouter_credits', 'OPENROUTER_CREDITS'),
    ('openrouter_key_limit', 'OPENROUTER_KEY_LIMIT'),
    ('openrouter_guardrail_budget', 'OPENROUTER_GUARDRAIL_BUDGET'),
    ('openrouter_workspace_budget', 'OPENROUTER_WORKSPACE_BUDGET'),
    (SECRET, 'OTHER'), ([], 'OTHER'), (True, 'OTHER'), (None, 'MISSING')])
def test_only_allowlisted_structured_source_is_retained(source, expected):
    result = model_limit_observation(failure(metadata={'limit_source': source, 'provider_name': SECRET}))
    assert result['limit_source'] == expected
    assert SECRET not in repr(result)


@pytest.mark.parametrize('code,expected', [(None, 'MISSING'), ('', 'INVALID'),
    (SECRET, 'PRESENT'), (429, 'PRESENT'), (True, 'INVALID'), ([], 'INVALID')])
def test_provider_code_is_only_presence_evidence(code, expected):
    result = model_limit_observation(failure(metadata={'provider_code': code}))
    assert result['provider_code_state'] == expected and result['limit_source'] == 'MISSING'
    assert SECRET not in repr(result)


@pytest.mark.parametrize('metadata', [None, SECRET, [], True])
def test_unstructured_metadata_does_not_invent_a_source(metadata):
    result = model_limit_observation(failure(metadata=metadata))
    assert result['limit_source'] == result['provider_code_state'] == 'MISSING'


@pytest.mark.parametrize('headers,state', [({}, 'MISSING'),
    ({'X-RateLimit-Limit': SECRET}, 'PARTIAL'),
    ({key: SECRET for key in ('X-RateLimit-Limit', 'X-RateLimit-Remaining', 'X-RateLimit-Reset')}, 'COMPLETE')])
def test_platform_headers_keep_only_completeness(headers, state):
    result = model_limit_observation(failure(headers=headers))
    assert result['rate_limit_headers'] == state and SECRET not in repr(result)


@pytest.mark.parametrize('raw,state', [(None, 'MISSING'), ('0', 'WITHIN_BUDGET'),
    ('2', 'WITHIN_BUDGET'), ('-1', 'WITHIN_BUDGET'), ('2.001', 'OVER_BUDGET'),
    ('60', 'OVER_BUDGET'), ('NaN', 'INVALID'), ('Infinity', 'INVALID'),
    (SECRET, 'INVALID'), ('1'*101, 'INVALID')])
def test_retry_hint_is_bounded_and_does_not_change_existing_delay(raw, state):
    error = failure(headers={} if raw is None else {'Retry-After': raw})
    before = model_retry_delay(error)
    assert model_limit_observation(error)['retry_after_state'] == state
    after = model_retry_delay(error)
    assert (before is None) == (after is None)
    assert (after is not None) == (state in {'MISSING', 'WITHIN_BUDGET'})
    assert classify_model_failure(error).category == 'RATE_LIMITED'


@pytest.mark.parametrize('seconds,expected', [(-60, 'WITHIN_BUDGET'), (60, 'OVER_BUDGET')])
def test_http_date_is_observed_without_recording_it(seconds, expected):
    raw = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=seconds), usegmt=True)
    assert model_limit_observation(failure(headers={'Retry-After': raw}))['retry_after_state'] == expected


def test_sse_body_retains_metadata_without_inventing_http_headers():
    result = model_limit_observation(failure(stream=True, metadata={'provider_code': SECRET}))
    assert result == dict(limit_source='MISSING', provider_code_state='PRESENT',
        rate_limit_headers='MISSING', retry_after_state='MISSING')
    assert model_limit_observation(ValueError(SECRET)) == {}


@pytest.mark.integration
@pytest.mark.parametrize('stream', [False, True])
async def test_actual_managed_failure_observes_evidence_then_recovers_once(
        api_client, db_session, monkeypatch, model, caplog, stream):
    _, value = await setup(api_client, db_session, monkeypatch, channel='VOICE')
    error = failure(stream=stream, metadata={'provider_code': SECRET},
        headers={'Retry-After': '0', 'X-RateLimit-Limit': SECRET})
    model.chat.completions.queue(error, build_llm_response(content='Какое время вам удобно?'))
    with caplog.at_level(logging.INFO):
        result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY' and result['draft_text'] == 'Какое время вам удобно?'
    assert len(model.chat.completions.calls) == 2
    assert model.chat.completions.calls[0] == model.chat.completions.calls[1]
    rows = [record for record in caplog.records if getattr(record, 'event', None) == 'managed_completion_observed']
    failed = [record for record in rows if record.branch == 'MODEL_FAILURE']
    assert len(failed) == 1 and failed[0].provider_code_state == 'PRESENT'
    assert failed[0].limit_source == 'MISSING'
    assert failed[0].rate_limit_headers == ('MISSING' if stream else 'PARTIAL')
    assert failed[0].retry_after_state == ('MISSING' if stream else 'WITHIN_BUDGET')
    assert all(SECRET not in record.getMessage() and SECRET not in repr(record.__dict__) for record in caplog.records)


@pytest.mark.integration
async def test_long_retry_hint_remains_terminal_without_deactivating_agent(
        api_client, db_session, monkeypatch, model, caplog):
    from models.agent import AgentTemplate
    _, value = await setup(api_client, db_session, monkeypatch, channel='VOICE')
    model.chat.completions.queue(failure(headers={'Retry-After': '60'},
        metadata={'limit_source': 'openrouter_key_limit', 'provider_code': SECRET}))
    with caplog.at_level(logging.INFO):
        result = (await execute(api_client, value)).json()
    assert result['status'] == 'FAILED' and result['error_code'] == 'MODEL_RATE_LIMITED'
    assert not result['operation_refs'] and len(model.chat.completions.calls) == 1
    async with db_session() as session:
        assert (await session.get(AgentTemplate, UUID(value['agent_id']))).is_active
    rows = [record for record in caplog.records if getattr(record, 'event', None) == 'managed_completion_observed']
    assert len(rows) == 1 and rows[0].limit_source == 'OPENROUTER_KEY_LIMIT'
    assert rows[0].retry_after_state == 'OVER_BUDGET'


@pytest.mark.integration
async def test_observation_failure_cannot_prevent_existing_model_recovery(
        api_client, db_session, monkeypatch, model):
    import services.model_limit_observation as observation
    def broken(error):
        raise RuntimeError(SECRET)
    monkeypatch.setattr(observation, 'model_limit_observation', broken)
    _, value = await setup(api_client, db_session, monkeypatch)
    model.chat.completions.queue(failure(headers={'Retry-After': '0'}),
        build_llm_response(content='Готово.'))
    result = (await execute(api_client, value)).json()
    assert result['status'] == 'DRAFT_READY' and result['draft_text'] == 'Готово.'
    assert len(model.chat.completions.calls) == 2 and model.closed


@pytest.mark.parametrize('field', list(diagnostics._LIMIT_FIELDS))
@pytest.mark.parametrize('invalid', [SECRET, True, [], 1])
def test_private_marker_cannot_bypass_new_field_validation(caplog, field, invalid):
    dto = diagnostics.CompletionDiagnostic(uuid4(), 1, 'MODEL_FAILURE', 'MISSING', 'NOT_INSPECTED')
    mark = managed_logging.set(True)
    try:
        with caplog.at_level(logging.INFO):
            diagnostics.emit_completion_diagnostic(replace(dto, **{field: invalid}))
    finally:
        managed_logging.reset(mark)
    assert not any(getattr(record, 'event', None) == 'managed_completion_observed' for record in caplog.records)
    assert all(SECRET not in record.getMessage() for record in caplog.records)


def test_success_cannot_carry_failure_evidence(caplog):
    mark = managed_logging.set(True)
    try:
        with caplog.at_level(logging.INFO):
            diagnostics.emit_completion_diagnostic(diagnostics.CompletionDiagnostic(
                uuid4(), 1, 'FINAL', 'STOP', 'TEXT', limit_source='OPENROUTER_CREDITS'))
    finally:
        managed_logging.reset(mark)
    assert not any(getattr(record, 'event', None) == 'managed_completion_observed' for record in caplog.records)
