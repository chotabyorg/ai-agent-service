"""Bounded operation lifecycle observations without arguments or exceptions."""
import asyncio
from contextlib import contextmanager
from dataclasses import dataclass
import logging
import time
from uuid import UUID

import anyio
import httpx
from openai import APITimeoutError

_IDENTITY = object()
_PHASES = frozenset({'MODEL_HTTP', 'EXECUTION_GUARD', 'FINAL_GUARD', 'RETRY_WAIT',
                    'CONTEXT_REPOSITORY', 'CONTEXT_RECEIPT', 'CONTEXT_BOOKING_READ'})
_STATES = frozenset({'START', 'COMPLETED', 'ERROR', 'CANCELLED', 'TIMEOUT'})
_MAX_MS = 2147483647


@dataclass(frozen=True, slots=True)
class ManagedWait:
    turn_id: UUID
    step: int
    attempt: int
    phase: str
    state: str
    elapsed_ms: int
    remaining_ms: int | None = None


def emit_managed_wait(value):
    try:
        from core.managed_security import managed_logging
        if not managed_logging.get() or not getattr(logging.Logger.makeRecord, '_managed_boundary', False):
            return
        logging.getLogger('uvicorn.error').info('managed_wait_observed', extra={
            '_wait_identity': _IDENTITY, '_wait_diagnostic': value})
    except Exception:
        pass


def _clock():
    try:
        return time.monotonic()
    except Exception:
        return None


def _observe(runtime, phase, step, attempt, state, started):
    try:
        if runtime is None or started is None:
            return
        remaining = max(0, round((runtime.deadline - anyio.current_time()) * 1000))
        elapsed = max(0, round((time.monotonic() - started) * 1000))
        emit_managed_wait(ManagedWait(runtime.request.turn_id, step, attempt, phase,
                                     state, min(_MAX_MS, elapsed), min(_MAX_MS, remaining)))
    except Exception:
        pass


@contextmanager
def observe_managed_wait(runtime, phase, *, step, attempt=1):
    """Observe the existing cancellation scope; never create or suppress one."""
    started = _clock()
    _observe(runtime, phase, step, attempt, 'START', started)
    try:
        yield
    except BaseException as error:
        state = 'CANCELLED' if isinstance(error, asyncio.CancelledError) else 'ERROR'
        try:
            from services.managed_tools import ManagedExecutionError
            if (isinstance(error, APITimeoutError)
                    or isinstance(error, ManagedExecutionError) and error.code == 'EXECUTION_TIMEOUT'):
                state = 'TIMEOUT'
            elif isinstance(error.__cause__, httpx.TimeoutException):
                # The proxy retains the typed HTTPX timeout as its explicit cause.
                # Other connection failures and unrelated exception contexts stay ERROR.
                from services.llm_service import PrivacyProxyConnectionError
                if isinstance(error, PrivacyProxyConnectionError):
                    state = 'TIMEOUT'
        except Exception:
            pass
        _observe(runtime, phase, step, attempt, state, started)
        raise
    else:
        _observe(runtime, phase, step, attempt, 'COMPLETED', started)


def _trusted_wait_record(record):
    if record.__dict__.get('_wait_identity') is not _IDENTITY:
        return None
    value = record.__dict__.get('_wait_diagnostic')
    if type(value) is not ManagedWait:
        return None
    if (type(value.turn_id) is not UUID or type(value.step) is not int
            or not 1 <= value.step <= 20 or type(value.attempt) is not int
            or not 1 <= value.attempt <= 2 or type(value.phase) is not str
            or value.phase not in _PHASES or type(value.state) is not str
            or value.state not in _STATES or type(value.elapsed_ms) is not int
            or not 0 <= value.elapsed_ms <= _MAX_MS):
        return None
    fields = {key: getattr(value, key) for key in
              ('step', 'attempt', 'phase', 'state', 'elapsed_ms')}
    fields = {'turn_id': str(value.turn_id), **fields}
    if value.remaining_ms is not None:
        if type(value.remaining_ms) is not int or not 0 <= value.remaining_ms <= _MAX_MS:
            return None
        fields['remaining_ms'] = value.remaining_ms
    safe = logging.LogRecord('uvicorn.error', logging.INFO, '', 0,
        'managed_wait_observed ' + ' '.join(key + '=%s' for key in fields),
        tuple(fields.values()), None)
    safe.event = 'managed_wait_observed'
    safe.__dict__.update(fields)
    return safe
