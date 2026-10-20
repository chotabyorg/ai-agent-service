"""Closed per-transaction client timings; no SQL, parameters or error text."""
import asyncio
from dataclasses import dataclass
import logging
import time
from uuid import UUID

from sqlalchemy import event

_IDENTITY = object()
_STAGES = frozenset({'ADMISSION_PROBE', 'CONFIRM', 'DISPATCH'})
_STATES = frozenset({'COMPLETED', 'ERROR', 'CANCELLED'})
_MAX = 2147483647
_NUMERIC = ('transaction_ms', 'checkout_ms', 'lock_ms', 'execute_ms', 'finish_ms',
            'other_ms', 'lock_count', 'execute_count')


@dataclass(frozen=True, slots=True)
class BusinessSQLTiming:
    turn_id: UUID
    step: int
    stage: str
    state: str
    finish_kind: str
    transaction_ms: int
    checkout_ms: int
    lock_ms: int
    execute_ms: int
    finish_ms: int
    other_ms: int
    lock_count: int
    execute_count: int


def emit_business_sql(value):
    try:
        from core.managed_security import managed_logging
        if managed_logging.get() and getattr(logging.Logger.makeRecord, '_managed_boundary', False):
            logging.getLogger('uvicorn.error').info('business_sql_observed', extra={
                '_business_sql_identity': _IDENTITY, '_business_sql_timing': value})
    except Exception:
        pass


def _trusted_business_sql_record(record):
    value = record.__dict__.get('_business_sql_timing')
    if (record.__dict__.get('_business_sql_identity') is not _IDENTITY
            or type(value) is not BusinessSQLTiming):
        return None
    if (type(value.turn_id) is not UUID or type(value.step) is not int or not 1 <= value.step <= 20
            or type(value.stage) is not str or value.stage not in _STAGES
            or type(value.state) is not str or value.state not in _STATES
            or type(value.finish_kind) is not str or value.finish_kind not in {'COMMIT', 'ROLLBACK'}
            or any(type(getattr(value, key)) is not int or not 0 <= getattr(value, key) <= _MAX for key in _NUMERIC)):
        return None
    fields = dict(turn_id=str(value.turn_id), step=value.step, stage=value.stage,
                  state=value.state, finish_kind=value.finish_kind,
                  **{key: getattr(value, key) for key in _NUMERIC})
    safe = logging.LogRecord('uvicorn.error', logging.INFO, '', 0,
        'business_sql_observed ' + ' '.join(key + '=%s' for key in fields), tuple(fields.values()), None)
    safe.event = 'business_sql_observed'
    safe.__dict__.update(fields)
    return safe


class BusinessSQLTrace:
    """Owned connection listeners end with this transaction; no task or scope.

    Checkout includes pool queue, connect, pre-ping and scheduling. Lock is the
    wall interval of SELECT FOR UPDATE/SHARE, including transport/execution,
    not isolated PostgreSQL lock-wait time. Execute covers other cursors.
    Finish covers commit or rollback including implicit flush. Other is the
    client residual. These intervals are disjoint within this transaction.
    """
    def __init__(self, runtime, stage):
        self.runtime, self.stage = runtime, stage
        self.step = 0
        self.connection = None
        self.listeners = []
        self.pending = None
        self.finishing = False
        self.finish_kind = 'ROLLBACK'
        self.elapsed = dict(checkout=0.0, lock=0.0, execute=0.0)
        self.counts = dict(lock=0, execute=0)
        self.finish_started = None
        if runtime is not None and stage in _STAGES:
            try:
                step = min(getattr(runtime, '_business_sql_step', 0) + 1, 21)
                runtime._business_sql_step = step
                self.step = step if step <= 20 else 0
            except Exception:
                pass

    def now(self):
        try:
            return time.monotonic()
        except Exception:
            self.step = 0
            return 0.0

    def __enter__(self):
        self.started = self.now()
        return self

    async def connect(self, session):
        if not self.step:
            return
        started = self.now()
        try:
            connection = await session.connection()
        finally:
            self.elapsed['checkout'] += self.now() - started
        self.connection = connection.sync_connection
        try:
            for name, callback in (('before_cursor_execute', self.before), ('after_cursor_execute', self.after)):
                event.listen(self.connection, name, callback)
                self.listeners.append((name, callback))
        except Exception:
            self.detach()
            self.step = 0

    def before(self, connection, cursor, statement, parameters, context, executemany):
        try:
            if not self.finishing:
                # Inspect the owned ORM expression, never retain SQL or binds.
                lock = getattr(getattr(context.compiled, 'statement', None), '_for_update_arg', None) is not None
                kind = 'lock' if lock else 'execute'
                self.counts[kind] += 1
                self.pending = (context, kind, self.now())
        except Exception:
            pass

    def after(self, connection, cursor, statement, parameters, context, executemany):
        self.finish_statement()

    def finish_statement(self):
        try:
            if self.pending is not None:
                _, kind, started = self.pending
                self.elapsed[kind] += self.now() - started
                self.pending = None
        except Exception:
            pass

    def finish_body(self, kind):
        self.finish_statement()
        self.finishing = True
        self.finish_kind = kind
        self.finish_started = self.now()

    def detach(self):
        for name, callback in self.listeners:
            try:
                event.remove(self.connection, name, callback)
            except Exception:
                pass
        self.listeners.clear()

    def __exit__(self, kind, error, traceback):
        try:
            self.finish_statement()
            if self.step:
                ended = self.now()
                def ms(seconds):
                    return min(_MAX, max(0, int(seconds * 1000)))
                total = ms(ended - self.started)
                values = {key + '_ms': ms(value) for key, value in self.elapsed.items()}
                values['finish_ms'] = ms(ended - self.finish_started) if self.finish_started is not None else 0
                emit_business_sql(BusinessSQLTiming(self.runtime.request.turn_id, self.step, self.stage,
                    'CANCELLED' if isinstance(error, asyncio.CancelledError) else 'ERROR' if error is not None else 'COMPLETED',
                    self.finish_kind, total, values['checkout_ms'], values['lock_ms'], values['execute_ms'],
                    values['finish_ms'], max(0, total - sum(values.values())), self.counts['lock'], self.counts['execute']))
        except Exception:
            pass
        finally:
            self.detach()
        return False
