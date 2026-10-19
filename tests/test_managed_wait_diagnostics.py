"""Operation entry/exit survives errors and cancellation without retaining content."""
import asyncio
from dataclasses import replace
import logging
from types import SimpleNamespace
from uuid import uuid4

import anyio
import httpx
from openai import APIConnectionError, APITimeoutError
import pytest

from core import managed_wait_diagnostics as diagnostics
from core.managed_security import install_managed_log_boundary, managed_logging
from services.managed_runtime import ManagedRuntime
from services.managed_tools import ManagedExecutionError
from tests.test_chotacrm_adapter import crm_settings  # noqa: F401

SECRET = 'private-prompt-contact-provider-body'


@pytest.fixture(autouse=True)
def logging_boundary(monkeypatch, request):
    if request.node.get_closest_marker('integration'):
        request.getfixturevalue('migrated_postgres')
    install_managed_log_boundary()
    logger = logging.getLogger('uvicorn.error')
    monkeypatch.setattr(logger, 'disabled', False)
    monkeypatch.setattr(logger, 'propagate', True)
    monkeypatch.setattr(logger, 'level', logging.INFO)
    token = managed_logging.set(True)
    try:
        yield
    finally:
        managed_logging.reset(token)


def observations(caplog):
    return [row for row in caplog.records if getattr(row, 'event', None) == 'managed_wait_observed']


@pytest.mark.parametrize('phase', sorted(diagnostics._PHASES))
async def test_success_observes_each_stage_and_keeps_deadline_and_result(caplog, phase):
    runtime = ManagedRuntime(10, request=SimpleNamespace(turn_id=uuid4()))
    deadline = runtime.deadline
    result = object()
    with diagnostics.observe_managed_wait(runtime, phase, step=3, attempt=2):
        actual = await runtime.operation(asyncio.sleep(0, result))
    assert actual is result and runtime.deadline == deadline and runtime.active_scope is None
    rows = observations(caplog)
    assert [row.state for row in rows] == ['START', 'COMPLETED']
    assert all(row.phase == phase and row.step == 3 and row.attempt == 2 for row in rows)
    assert 0 <= rows[1].remaining_ms <= rows[0].remaining_ms <= 10000


@pytest.mark.parametrize('kind, state', [('error', 'ERROR'), ('timeout', 'TIMEOUT'), ('cancel', 'CANCELLED')])
async def test_terminal_observation_preserves_the_original_error(caplog, kind, state):
    runtime = ManagedRuntime(10, request=SimpleNamespace(turn_id=uuid4()))
    error = (ManagedExecutionError('EXECUTION_TIMEOUT') if kind == 'timeout'
             else asyncio.CancelledError(SECRET) if kind == 'cancel' else RuntimeError(SECRET))
    with pytest.raises(type(error)) as raised:
        with diagnostics.observe_managed_wait(runtime, 'MODEL_HTTP', step=1):
            raise error
    assert raised.value is error
    assert [row.state for row in observations(caplog)] == ['START', state]
    assert SECRET not in caplog.text and all(SECRET not in repr(row.__dict__) for row in caplog.records)


@pytest.mark.parametrize('kind, state', [
    ('sdk-timeout', 'TIMEOUT'), ('sdk-connection', 'ERROR'),
    ('proxy-timeout', 'TIMEOUT'), ('proxy-connection', 'ERROR'),
    ('proxy-unavailable', 'ERROR'), ('proxy-timeout-context', 'ERROR'),
])
async def test_provider_timeout_types_preserve_error_and_deadline(caplog, kind, state):
    from services.llm_service import PrivacyProxyConnectionError
    request = httpx.Request('POST', 'https://fixture.invalid/' + SECRET)
    if kind == 'sdk-timeout':
        error = APITimeoutError(request=request)
    elif kind == 'sdk-connection':
        error = APIConnectionError(message=SECRET, request=request)
    else:
        error = PrivacyProxyConnectionError(SECRET)
        if kind == 'proxy-timeout':
            error.__cause__ = httpx.ReadTimeout(SECRET, request=request)
        elif kind == 'proxy-connection':
            error.__cause__ = httpx.ConnectError(SECRET, request=request)
        elif kind == 'proxy-timeout-context':
            # An unrelated handled timeout must not relabel a later connection error.
            error.__context__ = httpx.ReadTimeout(SECRET, request=request)
    runtime = ManagedRuntime(10, request=SimpleNamespace(turn_id=uuid4()))
    deadline = runtime.deadline
    with pytest.raises(type(error)) as raised:
        with diagnostics.observe_managed_wait(runtime, 'MODEL_HTTP', step=1):
            raise error
    assert raised.value is error and runtime.deadline == deadline
    assert [row.state for row in observations(caplog)] == ['START', state]
    assert SECRET not in caplog.text and all(SECRET not in repr(row.__dict__) for row in caplog.records)


@pytest.mark.parametrize('failure, state', [(httpx.ReadTimeout, 'TIMEOUT'), (httpx.ConnectError, 'ERROR')])
async def test_real_privacy_proxy_conversion_keeps_timeout_distinct(monkeypatch, caplog, failure, state):
    from services.llm_service import PrivacyProxyCompletions, PrivacyProxyConnectionError
    original = failure(SECRET, request=httpx.Request('POST', 'https://fixture.invalid'))

    async def fail(request):
        raise original

    client_class = httpx.AsyncClient
    clients = []

    def client(**kwargs):
        value = client_class(transport=httpx.MockTransport(fail), **kwargs)
        clients.append(value)
        return value

    monkeypatch.setattr('services.llm_service.httpx.AsyncClient', client)
    proxy = PrivacyProxyCompletions(privacy_filter_base_url='https://fixture.invalid',
        internal_api_token=SECRET, privacy_filter_timeout_seconds=1, llm_base_url='https://fixture.invalid',
        agent_api_key=SECRET, privacy_session_id=uuid4())
    runtime = ManagedRuntime(10, request=SimpleNamespace(turn_id=uuid4()))
    deadline = runtime.deadline
    with pytest.raises(PrivacyProxyConnectionError) as raised:
        with diagnostics.observe_managed_wait(runtime, 'MODEL_HTTP', step=1):
            await proxy.create(model='fixture', messages=[])
    assert raised.value.__cause__ is original and runtime.deadline == deadline
    assert len(clients) == 1 and clients[0].is_closed
    assert [row.state for row in observations(caplog)] == ['START', state]
    assert SECRET not in caplog.text and all(SECRET not in repr(row.__dict__) for row in caplog.records)


async def test_real_runtime_deadline_and_cancel_are_not_replaced(caplog):
    runtime = ManagedRuntime(.01, request=SimpleNamespace(turn_id=uuid4()))
    with pytest.raises(ManagedExecutionError, match='EXECUTION_TIMEOUT'):
        with diagnostics.observe_managed_wait(runtime, 'EXECUTION_GUARD', step=1):
            await runtime.operation(asyncio.Event().wait())
    assert [row.state for row in observations(caplog)] == ['START', 'TIMEOUT']
    assert runtime.active_scope is None
    caplog.clear()
    runtime = ManagedRuntime(10, request=SimpleNamespace(turn_id=uuid4()))

    async def cancel():
        runtime.signal_cancel()
        await anyio.sleep(0)

    with pytest.raises(asyncio.CancelledError):
        with diagnostics.observe_managed_wait(runtime, 'MODEL_HTTP', step=1):
            await runtime.operation(cancel())
    assert [row.state for row in observations(caplog)] == ['START', 'CANCELLED']
    assert runtime.active_scope is None


async def test_adapter_local_timeout_is_typed_and_does_not_extend_request(caplog):
    runtime = ManagedRuntime(10, request=SimpleNamespace(turn_id=uuid4()))
    deadline = runtime.deadline
    with pytest.raises(TimeoutError):
        with diagnostics.observe_managed_wait(runtime, 'BUSINESS_FACTS_ADAPTER', step=1):
            with anyio.fail_after(.01):
                await runtime.operation(asyncio.Event().wait())
    assert [row.state for row in observations(caplog)] == ['START', 'TIMEOUT']
    assert runtime.deadline == deadline and runtime.active_scope is None


@pytest.mark.parametrize('changes', [
    {'turn_id': SECRET}, {'step': True}, {'step': 21}, {'attempt': 0}, {'attempt': 3},
    {'phase': SECRET}, {'state': SECRET}, {'elapsed_ms': -1}, {'elapsed_ms': True},
    {'remaining_ms': float('nan')}, {'remaining_ms': SECRET},
])
def test_invalid_or_private_fields_are_replaced_before_any_handler(caplog, changes):
    value = diagnostics.ManagedWait(uuid4(), 1, 1, 'MODEL_HTTP', 'START', 0, 1000)
    diagnostics.emit_managed_wait(replace(value, **changes))
    assert observations(caplog) == []
    assert SECRET not in caplog.text and all(SECRET not in repr(row.__dict__) for row in caplog.records)


def test_forged_record_and_extra_payload_cannot_escape_boundary(caplog):
    value = diagnostics.ManagedWait(uuid4(), 1, 1, 'MODEL_HTTP', 'START', 0)
    logger = logging.getLogger('uvicorn.error')
    logger.info(SECRET, extra={'_wait_identity': object(), '_wait_diagnostic': value, 'secret': SECRET})
    logger.info(SECRET, extra={'_wait_identity': diagnostics._IDENTITY,
                             '_wait_diagnostic': value, 'secret': SECRET})
    assert len(observations(caplog)) == 1
    assert SECRET not in caplog.text and all(SECRET not in repr(row.__dict__) for row in caplog.records)


async def test_logging_and_clock_failure_do_not_change_execution(monkeypatch):
    runtime = ManagedRuntime(10, request=SimpleNamespace(turn_id=uuid4()))

    def broken(*args, **kwargs):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(logging.getLogger('uvicorn.error'), 'info', broken)
    with diagnostics.observe_managed_wait(runtime, 'MODEL_HTTP', step=1):
        assert await runtime.operation(asyncio.sleep(0, 'value')) == 'value'
    monkeypatch.setattr(diagnostics, 'time', SimpleNamespace(monotonic=broken))
    completed = []
    with diagnostics.observe_managed_wait(runtime, 'MODEL_HTTP', step=1):
        completed.append(True)
    assert completed == [True]


@pytest.mark.integration
async def test_model_retry_has_two_attempts_and_one_unchanged_guarded_read(
        db_session, crm_settings, monkeypatch, caplog):
    from tests.fakes import ScriptedLLMClient, build_llm_response
    from tests.test_pending_proposal_decision import run_core
    from tests.test_preparation_schema_correction import local_runtime, preparation
    from tests.test_text_booking_recovery import failure
    business, runtime, provider, _ = await local_runtime(db_session, crm_settings, monkeypatch)
    model = ScriptedLLMClient()
    model.chat.completions.queue(preparation('catalog_search', {}, 'read'), failure(429),
                                 build_llm_response(content='Какое время вам удобно?'))
    deadline = runtime.deadline
    assert await run_core(db_session, runtime, model) == 'Какое время вам удобно?'
    rows = observations(caplog)
    attempts = [(row.attempt, row.state) for row in rows
                if row.phase == 'MODEL_HTTP' and row.step == 2]
    assert attempts == [(1, 'START'), (1, 'ERROR'), (2, 'START'), (2, 'COMPLETED')]
    assert [(row.attempt, row.state) for row in rows if row.phase == 'RETRY_WAIT'] == [
        (1, 'START'), (1, 'COMPLETED')]
    assert model.chat.completions.calls[1] == model.chat.completions.calls[2]
    assert provider['posts'] == 0 and runtime.deadline == deadline
    assert await business.repository.selected_plan(business.request, business.grant) is None


@pytest.mark.integration
async def test_blocked_guard_timeout_is_not_reported_as_model_wait(
        db_session, crm_settings, monkeypatch, caplog):
    from tests.fakes import ScriptedLLMClient
    from tests.test_pending_proposal_decision import run_core
    from tests.test_preparation_schema_correction import local_runtime
    business, runtime, provider, _ = await local_runtime(db_session, crm_settings, monkeypatch)
    entered = []

    async def blocked(**kwargs):
        # Expire the existing scope only once this guard has actually started.
        # A deadline before run_core can instead expire during unrelated setup.
        entered.append(True)
        runtime.deadline = anyio.current_time() + .01
        runtime.active_scope.deadline = runtime.deadline
        await asyncio.Event().wait()

    monkeypatch.setattr(business, 'execution_guard', blocked)
    model = ScriptedLLMClient()
    with pytest.raises(ManagedExecutionError, match='EXECUTION_TIMEOUT'):
        await run_core(db_session, runtime, model)
    rows = observations(caplog)
    assert [(row.phase, row.state) for row in rows] == [
        ('CONTEXT_REPOSITORY', 'START'), ('CONTEXT_REPOSITORY', 'COMPLETED'),
        ('EXECUTION_GUARD', 'START'), ('EXECUTION_GUARD', 'TIMEOUT')]
    assert entered == [True] and model.chat.completions.calls == [] and provider['posts'] == 0
