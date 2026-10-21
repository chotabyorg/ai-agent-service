"""CRM timing must preserve requests, per-turn ownership and failure semantics."""
import asyncio
import base64
import json
import sys
from types import SimpleNamespace
from uuid import uuid4

import httpx
import anyio
import pytest

from business_capabilities.chotacrm_client import ChotaCRMClient
from business_capabilities.context import BusinessRuntimeError
from core import managed_wait_diagnostics as diagnostics
from services.managed_runtime import ManagedRuntime
from tests.test_chotacrm_adapter import crm_settings  # noqa: F401
from tests.test_managed_wait_diagnostics import logging_boundary, observations  # noqa: F401


PRIVATE = 'private-crm-path-header-body-claim'


def runtime():
    return ManagedRuntime(10, request=SimpleNamespace(turn_id=uuid4()))


async def finish(execution):
    with anyio.CancelScope(shield=True) as lifecycle:
        await execution.cleanup(lifecycle)


@pytest.fixture(autouse=True)
async def cleanup_test_runtimes(monkeypatch):
    original, executions = runtime, []

    def create():
        execution = original()
        executions.append(execution)
        return execution

    monkeypatch.setattr(sys.modules[__name__], 'runtime', create)
    yield
    for execution in executions:
        if not execution._crm_http_closed:
            await finish(execution)


def capture_clients(monkeypatch):
    original = httpx.AsyncClient
    clients = []

    def create(**kwargs):
        value = original(**kwargs)
        clients.append(value)
        assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        return value

    monkeypatch.setattr('business_capabilities.chotacrm_client.httpx.AsyncClient', create)
    return clients


def assert_private(caplog):
    assert PRIVATE not in caplog.text
    assert all(PRIVATE not in repr(row.__dict__) for row in caplog.records)


@pytest.mark.parametrize('method,path,phase', [
    ('GET', 'context', 'CRM_CONTEXT_HTTP'),
    ('POST', 'facts/' + PRIVATE, 'CRM_FACTS_HTTP'),
    ('POST', 'execute/' + PRIVATE, 'CRM_EXECUTE_HTTP'),
    ('GET', 'operations/' + PRIVATE, 'CRM_STATUS_HTTP'),
    ('POST', 'other/' + PRIVATE, 'CRM_OTHER_HTTP'),
])
async def test_request_and_fresh_assertions_survive_route_observation(
        crm_settings, monkeypatch, caplog, method, path, phase):
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(200, json={'value': PRIVATE}, headers={'Set-Cookie': 'leak=' + PRIVATE})

    clients = capture_clients(monkeypatch)
    client = ChotaCRMClient(crm_settings, transport=httpx.MockTransport(handle))
    execution = runtime()
    deadline = execution.deadline
    claims = {'owner_id': PRIVATE}
    for _ in range(2):
        assert await client.request(method, path, scope='fixture', claims=claims,
            body={'argument': PRIVATE}, execution=execution) == {'value': PRIVATE}
    assert len(clients) == 1 and not clients[0].is_closed
    await finish(execution)
    assert all(value.is_closed for value in clients)
    assert execution.deadline == deadline and execution.active_scope is None
    assert all(request.method == method and request.url.path.endswith('/' + path)
        and json.loads(request.content) == {'argument': PRIVATE} and 'cookie' not in request.headers
        for request in requests)
    assertions = [request.headers['authorization'].split(' ')[1] for request in requests]
    payloads = [json.loads(base64.urlsafe_b64decode(value.split('.')[1] + '==')) for value in assertions]
    assert payloads[0]['jti'] != payloads[1]['jti']
    assert all(value['owner_id'] == PRIVATE and value['scope'] == 'fixture' for value in payloads)
    rows = observations(caplog)
    assert [(row.phase, row.state) for row in rows] == [
        (stage, state) for _ in range(2)
        for stage in ('CRM_ASSERTION', 'CRM_CLIENT_SETUP', phase)
        for state in ('START', 'COMPLETED')] + [('CRM_CLIENT_CLOSE', 'START'), ('CRM_CLIENT_CLOSE', 'COMPLETED')]
    assert [row.step for row in rows if row.phase == phase and row.state == 'START'] == [1, 2]
    assert_private(caplog)


@pytest.mark.parametrize('failure,state', [
    (httpx.ReadTimeout, 'TIMEOUT'), (httpx.ConnectError, 'ERROR'),
    (asyncio.CancelledError, 'CANCELLED'),
])
async def test_transport_failure_is_not_retried_or_replaced_by_timing(
        crm_settings, monkeypatch, caplog, failure, state):
    original = failure(PRIVATE)
    calls = []

    async def handle(request):
        calls.append(request)
        raise original

    clients = capture_clients(monkeypatch)
    client = ChotaCRMClient(crm_settings, transport=httpx.MockTransport(handle))
    execution = runtime()
    deadline = execution.deadline
    expected = asyncio.CancelledError if failure is asyncio.CancelledError else BusinessRuntimeError
    with pytest.raises(expected) as raised:
        await client.request('POST', 'execute/' + PRIVATE, scope='fixture',
            claims={'owner_id': PRIVATE}, execution=execution)
    if failure is asyncio.CancelledError:
        assert raised.value is original
    else:
        assert raised.value.code == 'PROVIDER_UNAVAILABLE'
    assert len(calls) == len(clients) == 1
    await finish(execution)
    assert clients[0].is_closed
    assert execution.deadline == deadline and execution.active_scope is None
    assert [row.state for row in observations(caplog) if row.phase == 'CRM_EXECUTE_HTTP'] == ['START', state]
    assert [row.state for row in observations(caplog) if row.phase == 'CRM_CLIENT_CLOSE'] == ['START', 'COMPLETED']
    assert_private(caplog)


@pytest.mark.parametrize('status,content,code', [
    (401, b'{}', 'PERMISSION_DENIED'), (403, b'{}', 'PERMISSION_DENIED'),
    (503, b'{}', 'PROVIDER_UNAVAILABLE'), (200, b'broken', 'PROVIDER_UNAVAILABLE'),
    (200, b'x' * 32769, 'PROVIDER_UNAVAILABLE'), (204, b'', None),
], ids=['unauthenticated', 'forbidden', 'unavailable', 'invalid-json', 'oversized', 'empty-success'])
async def test_response_gates_and_close_stay_intact(crm_settings, monkeypatch, caplog, status, content, code):
    clients = capture_clients(monkeypatch)
    client = ChotaCRMClient(crm_settings, transport=httpx.MockTransport(
        lambda request: httpx.Response(status, content=content)))
    execution = runtime()
    if code:
        with pytest.raises(BusinessRuntimeError) as raised:
            await client.request('GET', 'context', scope='fixture', claims={}, execution=execution)
        assert raised.value.code == code
    else:
        assert await client.request('GET', 'context', scope='fixture', claims={}, execution=execution) is None
    await finish(execution)
    assert clients[0].is_closed
    assert [row.state for row in observations(caplog) if row.phase == 'CRM_CLIENT_CLOSE'] == ['START', 'COMPLETED']


async def test_cancel_during_response_body_keeps_cleanup_and_original_cancellation(crm_settings, monkeypatch, caplog):
    entered, closed = asyncio.Event(), asyncio.Event()

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{'
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    clients = capture_clients(monkeypatch)
    client = ChotaCRMClient(crm_settings, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, stream=Body())))
    execution = runtime()
    deadline = execution.deadline
    task = asyncio.create_task(client.request('GET', 'context', scope='fixture', claims={}, execution=execution))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await finish(execution)
    assert closed.is_set() and clients[0].is_closed and execution.deadline == deadline
    assert [row.state for row in observations(caplog) if row.phase == 'CRM_CONTEXT_HTTP'] == ['START', 'CANCELLED']
    assert [row.state for row in observations(caplog) if row.phase == 'CRM_CLIENT_CLOSE'] == ['START', 'COMPLETED']


async def test_nonmanaged_request_keeps_config_cookie_and_emits_no_turn(crm_settings, monkeypatch, caplog):
    captured = []

    def handle(request):
        captured.append(request)
        return httpx.Response(204)

    clients = capture_clients(monkeypatch)
    client = ChotaCRMClient(crm_settings, transport=httpx.MockTransport(handle))
    assert await client.request('GET', 'context', scope='fixture', claims={'owner_id': PRIVATE}, cookie=PRIVATE) is None
    assert captured[0].headers['cookie'] == captured[0].headers['x-user-id'] == PRIVATE
    assert clients[0].is_closed and observations(caplog) == []
    assert_private(caplog)


async def test_observation_ordinals_are_bounded_per_turn_not_per_client(crm_settings, caplog):
    first, second = runtime(), runtime()
    for _ in range(25):
        with diagnostics.observe_managed_crm_wait(first, 'CRM_ASSERTION'):
            pass
    client = ChotaCRMClient(crm_settings, transport=httpx.MockTransport(
        lambda request: httpx.Response(204)))
    for execution in (first, second):
        await client.request('GET', 'context', scope='fixture', claims={}, execution=execution)
    starts = [row for row in observations(caplog) if row.phase == 'CRM_ASSERTION' and row.state == 'START']
    assert [row.step for row in starts if row.turn_id == str(first.request.turn_id)] == list(range(1, 21))
    assert [row.step for row in starts if row.turn_id == str(second.request.turn_id)] == [1]
    assert first._crm_wait_steps['CRM_ASSERTION'] == 21


async def test_observation_state_or_logger_failure_does_not_change_request(crm_settings, monkeypatch, caplog):
    client = ChotaCRMClient(crm_settings, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'ok': True})))
    execution = runtime()
    execution._crm_wait_steps = object()
    assert await client.request('GET', 'context', scope='fixture', claims={}, execution=execution) == {'ok': True}
    assert observations(caplog) == []

    def broken(*args, **kwargs):
        raise RuntimeError(PRIVATE)

    monkeypatch.setattr(diagnostics, 'emit_managed_wait', broken)
    assert await client.request('GET', 'context', scope='fixture', claims={}, execution=runtime()) == {'ok': True}
    assert_private(caplog)


def test_aggregate_preparation_diagnostics_are_visible_without_opening_the_allowlist(caplog):
    from core.managed_business_diagnostics import ManagedBusinessDiagnostic, emit_business_diagnostic

    for capability in ('booking_search_options', PRIVATE):
        emit_business_diagnostic(ManagedBusinessDiagnostic(
            turn_id=uuid4(), stage='LOOKUP', capability=capability,
            refusal_category='VALIDATION_ERROR', lookup_source='BOOKING_PREFLIGHT',
            lookup_mode='IDENTITY_ONLY', lookup_result='ERROR',
            proposal_present=False, operation_present=False))
    rows = [row for row in caplog.records if getattr(row, 'event', None) == 'managed_business_observed']
    assert len(rows) == 1 and rows[0].capability == 'booking_search_options'
    assert rows[0].refusal_category == 'VALIDATION_ERROR' and rows[0].operation_present is False
    assert_private(caplog)
