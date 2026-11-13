"""Bounded, process-local query telemetry without SQL text or bind values."""
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
import os
from threading import RLock
from time import perf_counter

from sqlalchemy import event

_request = ContextVar('n2d_db_request', default=None)


class QueryObserver:
    def __init__(self, capacity=128):
        self.capacity = capacity
        self.lock = RLock()
        self.operations = OrderedDict()
        self.total = dict(calls=0, errors=0, slow=0, total_ms=0.0, max_ms=0.0)
        try:
            self.slow_ms = max(0.0, float(os.getenv('NEWS2DOUYIN_DB_SLOW_QUERY_MS', '250')))
        except ValueError:
            self.slow_ms = 250.0

    def record(self, name, elapsed, error):
        # Names are code-owned execution options; ignore invalid/unbounded names.
        if not isinstance(name, str) or len(name) > 100 or any(c not in
                'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-' for c in name):
            name = 'sql.other'
        values = dict(calls=1, errors=int(error), slow=int(elapsed >= self.slow_ms),
                      total_ms=elapsed, max_ms=elapsed)
        with self.lock:
            current = self.operations.pop(name, None)
            if current is None:
                current = dict(calls=0, errors=0, slow=0, total_ms=0.0, max_ms=0.0)
            for target in (current, self.total):
                for field in ('calls', 'errors', 'slow', 'total_ms'):
                    target[field] += values[field]
                target['max_ms'] = max(target['max_ms'], elapsed)
            self.operations[name] = current
            if len(self.operations) > self.capacity:
                self.operations.popitem(last=False)
        request = _request.get()
        if request is not None:
            with request['lock']:
                request['calls'] += 1
                request['total_ms'] += elapsed
                request['errors'] += int(error)

    def snapshot(self):
        with self.lock:
            rows = [dict(name=name, **values) for name, values in self.operations.items()]
            totals = dict(self.total)
        for row in rows:
            row['mean_ms'] = round(row['total_ms'] / row['calls'], 3)
            row['total_ms'], row['max_ms'] = round(row['total_ms'], 3), round(row['max_ms'], 3)
        totals['total_ms'], totals['max_ms'] = round(totals['total_ms'], 3), round(totals['max_ms'], 3)
        return dict(scope='current_process', retained_names=len(rows), capacity=self.capacity,
            slow_threshold_ms=self.slow_ms, totals=totals,
            operations=sorted(rows, key=lambda row: (-row['total_ms'], row['name'])))


def install_observer(engine):
    """Register once per engine; timers finish on both success and driver errors."""
    if hasattr(engine, '_n2d_query_observer'):
        return engine._n2d_query_observer
    observer = QueryObserver()
    engine._n2d_query_observer = observer

    @event.listens_for(engine, 'before_cursor_execute')
    def before(_connection, _cursor, _statement, _parameters, context, _many):
        context._n2d_query_started = perf_counter()

    def finish(context, failed=False):
        started = getattr(context, '_n2d_query_started', None)
        if started is None:
            return
        context._n2d_query_started = None
        name = context.execution_options.get('n2d_query_name', 'sql.unnamed')
        observer.record(name, (perf_counter() - started) * 1000, failed)

    @event.listens_for(engine, 'after_cursor_execute')
    def after(_connection, _cursor, _statement, _parameters, context, _many):
        finish(context)

    @event.listens_for(engine, 'handle_error')
    def error(context):
        finish(context.execution_context, True)
    return observer


@contextmanager
def request_queries():
    metrics = dict(calls=0, errors=0, total_ms=0.0, lock=RLock())
    token = _request.set(metrics)
    try:
        yield metrics
    finally:
        _request.reset(token)


def snapshot(engine):
    observer = getattr(engine, '_n2d_query_observer', None)
    data = observer.snapshot() if observer else dict(scope='current_process', operations=[], totals={})
    pool = engine.pool
    data['database'] = dict(dialect=engine.dialect.name, driver=engine.dialect.driver)
    data['pool'] = dict(kind=type(pool).__name__)
    for key, method in (('size', 'size'), ('checked_out', 'checkedout'),
                        ('overflow', 'overflow'), ('wait_timeout_seconds', 'timeout')):
        getter = getattr(pool, method, None)
        if callable(getter):
            data['pool'][key] = getter()
    return data
