"""Independent orphan maintenance with durable cursors and bounded deletions.

Projection acknowledgement never calls this module. Each tick takes the same
global graph lease as projection, rechecks references at deletion time, and
visits at most a small native-ID window. Sorting happens only within that
window; ``ORDER BY property LIMIT`` over an open index range is deliberately
not used, because a top-k sort could still inspect the entire remaining graph.

The once-per-label-cycle ``max(id(n))`` high-water read is an aggregate and is
NOT a bounded scan. It has its own server query timeout, runs outside event
acknowledgement, and is followed by durable, bounded windows. The next cycle
starts only after the configured maintenance interval.
"""
from __future__ import annotations

import json
import os
import re
import time
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from math import isfinite
from threading import RLock

from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from ..extraction.schema import stable_id
from ..storage.articles import lock_news
from ..storage.models import utc_now_iso
from .graph_maintenance_models import GraphGcControl, GraphGcState
from .leases import LeaseLost


# Dependency order allows one cycle to reclaim children detached by an earlier
# label. Business Event/Focus/Question/Interest* nodes are never GC candidates.
LABELS = ('Article', 'EventMention', 'Argument', 'OccurrenceCandidate', 'Referent', 'Entity', 'Place')
_REFERENCES = {
    'Article': ('HAS_SOURCE', 'CITES'),
    'EventMention': ('DESCRIBES',),
    'Argument': ('HAS_ARGUMENT',),
    'OccurrenceCandidate': ('HAS_MATCH_KEY',),
    'Referent': ('REFERS_TO', 'MAPS_TO'),
    'Entity': ('MENTIONS', 'WATCHES'),
    'Place': ('MENTIONS_PLACE', 'ACTION_AT', 'IMPACTS'),
}


def enabled(mirror=None):
    """An environment off switch stops maintenance without altering progress."""
    value = os.getenv('NEWS2DOUYIN_GRAPH_GC_ENABLED', '1').strip().lower()
    if value not in ('0', '1', 'false', 'true', 'off', 'on', 'no', 'yes'):
        raise ValueError('NEWS2DOUYIN_GRAPH_GC_ENABLED must be a boolean')
    return value in ('1', 'true', 'on', 'yes')


def _control_key(mirror):
    return 'gcctl_'+stable_id(mirror.prefix)


def control(engine, mirror):
    """Cheap read-only effective settings; use an existing status Session."""
    active = enabled(mirror)
    context = nullcontext(engine) if isinstance(engine, Session) else Session(engine)
    with context as session:
        row = session.get(GraphGcControl, _control_key(mirror))
        resume_at = row.resume_at if row and row.paused else 0.0
        paused = bool(row and row.paused and (not resume_at or resume_at > time.time()))
    return dict(enabled=active, paused=paused, resume_at=resume_at if paused else 0.0)


def set_paused(engine, mirror, paused=True, *, resume_at=0):
    """Pause/resume all graphs in this namespace without changing a cursor.

    The current small batch may finish and publish its checkpoint. Subsequent
    ticks stop before graph I/O. Resuming never resets a cycle, clears a prior
    error or changes scheduling/backoff; use retry explicitly for an error.
    """
    if not isinstance(paused, bool):
        raise ValueError('paused must be a boolean')
    if isinstance(resume_at, bool):
        raise ValueError('resume_at must be a nonnegative UTC epoch')
    try:
        resume_at = float(resume_at)
    except (ValueError, TypeError):
        raise ValueError('resume_at must be a nonnegative UTC epoch') from None
    if not isfinite(resume_at) or resume_at < 0 or (not paused and resume_at):
        raise ValueError('resume_at must be a nonnegative UTC epoch for a pause')
    key = _control_key(mirror)
    with Session(engine) as session:
        lock_news(session, 'graph-gc-control/'+key)
        row = session.get(GraphGcControl, key)
        if row is None:
            row = GraphGcControl(key=key, namespace=mirror.prefix)
        row.paused, row.resume_at, row.updated = paused, resume_at, utc_now_iso()
        session.add(row)
        session.commit()
    return dict(ok=True, **control(engine, mirror))


_DIAGNOSTIC_LOCK = RLock()
def _timeout(exc):
    # Driver messages are inspected only to classify the error. Their contents
    # (which may include query text or endpoints) are never retained or exposed.
    return isinstance(exc, TimeoutError) or 'timeout' in type(exc).__name__.lower() or bool(
        re.search(r'timed?\s*out|timeout', str(exc), re.IGNORECASE))


@contextmanager
def _timed(mirror, graph, label, phase):
    """Bounded, process-local diagnostics distinguish lock/scan/error phases."""
    started, failure = time.perf_counter(), None
    try:
        yield
    except Exception as exc:
        failure = dict(type=type(exc).__name__, timeout=_timeout(exc))
        raise
    finally:
        elapsed = max(0., (time.perf_counter()-started)*1000)
        with _DIAGNOSTIC_LOCK:
            store = getattr(mirror, '_gc_diagnostics', None)
            if store is None:
                store = mirror._gc_diagnostics = OrderedDict()
            if graph not in store:
                store[graph] = dict(phases={}, last={})
            item = store[graph]
            value = item['phases'].setdefault(phase, dict(calls=0, errors=0, timeouts=0, total_ms=0., last_ms=0.))
            value['calls'] += 1
            value['total_ms'] += elapsed
            value['last_ms'] = elapsed
            if failure:
                value['errors'] += 1
                value['timeouts'] += int(failure['timeout'])
            item['last'] = dict(phase=phase, label=label, elapsed_ms=elapsed,
                                error=failure['type'] if failure else '',
                                timeout=failure['timeout'] if failure else False)
            if failure:
                item['last_failure'] = dict(item['last'])
            store.move_to_end(graph)
            while len(store) > 128:
                store.popitem(last=False)


def diagnostics(mirror, graph='active_global'):
    """No query parameters/driver messages; bounded to 128 graphs per worker."""
    with _DIAGNOSTIC_LOCK:
        item = getattr(mirror, '_gc_diagnostics', {}).get(graph, {})
        return dict(phases={key:dict(value) for key, value in item.get('phases', {}).items()},
                    last=dict(item.get('last', {})), last_failure=dict(item.get('last_failure', {})),
                    scope='process')


def _integer(name, default, low, high):
    raw = os.getenv(name, '').strip()
    try:
        result = int(raw) if raw else default
    except ValueError:
        raise ValueError(name+' must be an integer') from None
    if not low <= result <= high:
        raise ValueError(name+' is outside its supported range')
    return result


def _settings():
    batch = _integer('NEWS2DOUYIN_GRAPH_GC_BATCH_SIZE', 50, 1, 500)
    return dict(batch=batch,
        window=max(batch, _integer('NEWS2DOUYIN_GRAPH_GC_WINDOW', 1024, 64, 16384)),
        interval=_integer('NEWS2DOUYIN_GRAPH_GC_INTERVAL_SEC', 300, 30, 86400),
        timeout=_integer('NEWS2DOUYIN_GRAPH_GC_QUERY_TIMEOUT_MS', 500, 10, 10000))


def _key(mirror, graph, label):
    return 'gc_'+stable_id(mirror.prefix, graph, label)


def _valid_graph(graph):
    # Both graph names come from the application's own namespace. Never accept
    # a graph expression or label supplied by a model or a free-form query.
    return graph == 'active_global' or bool(re.fullmatch(r'focus_w_[0-9a-f]{32}', graph))


def _states(engine, mirror, graph):
    with Session(engine) as session:
        rows = session.exec(select(GraphGcState).where(
            GraphGcState.namespace == mirror.prefix, GraphGcState.graph == graph)).all()
        present = {row.label for row in rows}
        for label in LABELS:
            if label not in present:
                session.add(GraphGcState(key=_key(mirror, graph, label), namespace=mirror.prefix,
                                         graph=graph, label=label))
        session.commit()


def _pick(engine, mirror, graph, now):
    with Session(engine, expire_on_commit=False) as session:
        rows = session.exec(select(GraphGcState).where(
            GraphGcState.namespace == mirror.prefix, GraphGcState.graph == graph,
            GraphGcState.next_due <= now)).all()
        if not rows:
            return None
        # Finish an active label before starting another; then serve the oldest
        # due label. A first label becoming due again cannot starve later ones.
        return min(rows, key=lambda row: (row.status != 'sweeping', row.next_due,
                                        LABELS.index(row.label) if row.label in LABELS else len(LABELS)))


def _select_query(label):
    return ('MATCH (n) WHERE id(n)>=$lo AND id(n)<$hi AND $label IN labels(n) '
            'RETURN id(n),n.id ORDER BY id(n) LIMIT $limit')


def _delete_query(label):
    predicate = ' AND '.join('NOT (n)<-[:'+edge+']-()' for edge in _REFERENCES[label])
    return ('UNWIND $ids AS ident MATCH (n) WHERE id(n)=ident AND $label IN labels(n) AND '
            +predicate+' WITH n DELETE n RETURN count(n)')


def _explain(mirror, graph, query, **params):
    method = getattr(mirror, 'explain', None)
    if method:
        return method(graph, query, **params)
    # The production mirror centralizes this command. This compatibility path
    # is also safe: it never executes the explained query or modifies data.
    prefix = ' '.join(key+'='+json.dumps(value, ensure_ascii=False) for key, value in params.items())
    return mirror.client.execute_command('GRAPH.EXPLAIN', mirror.prefix+'_'+graph,
        ('CYPHER '+prefix+' ' if prefix else '')+query)


def _check_seek(mirror, graph, label, settings):
    cache = getattr(mirror, '_gc_seek_plans', None)
    if cache is None:
        cache = mirror._gc_seek_plans = set()
    key = (graph, label)
    if key in cache:
        return
    plans = [_explain(mirror, graph, _select_query(label), lo=0,
                      hi=settings['window'], limit=settings['batch'], label=label),
             _explain(mirror, graph, _delete_query(label), ids=[0], label=label)]
    if any('nodebyidseek' not in re.sub(r'[^a-z]', '', str(plan).lower()) for plan in plans):
        raise ValueError('graph_gc_native_id_seek_unavailable')
    cache.add(key)


def _read(mirror, graph, query, timeout, **params):
    return mirror.read(graph, query, _query_timeout_ms=timeout, **params)


def _deleted_count(result):
    # GRAPH.QUERY ordinarily returns [columns, rows, statistics]. Keep metrics
    # honest when a server sends statistics only instead of a RETURN row.
    if isinstance(result, (list, tuple)):
        if len(result) > 1 and isinstance(result[1], (list, tuple)) and result[1]:
            row = result[1][0]
            if isinstance(row, (list, tuple)) and row and isinstance(row[0], int):
                return row[0]
        for item in result:
            if isinstance(item, (list, tuple)):
                for value in item:
                    match = re.fullmatch(r'Nodes deleted:\s*(\d+)', str(value))
                    if match:
                        return int(match[1])
    raise ValueError('graph_gc_deletion_count_missing')


def _persist(engine, mirror, previous, values):
    lease = mirror._projection_lease
    with Session(engine) as session:
        lock_news(session, 'lease/'+lease.key, 'graph-gc/'+previous.key)
        lease.confirm(session)
        current = session.get(GraphGcState, previous.key)
        if current is None or (current.cursor, current.cycle) != (previous.cursor, previous.cycle):
            return False
        for name, value in values.items():
            setattr(current, name, value)
        current.updated = utc_now_iso()
        session.add(current)
        session.commit()
    return True


def _error(engine, mirror, row, exc, phase):
    # Only safe exception class names enter user-visible state, never a Redis
    # URL, query parameters, source text or raw driver exception message.
    failed = diagnostics(mirror, row.graph)['last_failure']
    exact_phase = failed.get('phase', phase)
    names = dict(plan='执行计划检查', highwater='扫描上界读取', read='候选读取', delete='孤立节点删除')
    message = '图清理'+names.get(exact_phase, '批次')+('超时' if _timeout(exc) else '暂未完成')+'（'+type(exc).__name__+'）'
    _persist(engine, mirror, row, dict(status='error', phase=phase,
        error=message, next_due=time.time()+30))
    mirror.gc_error = message
    return False


def step(engine, mirror, graph='active_global'):
    """Run one maintenance batch, independently of successful event acks.

    The caller may already hold ``mirror.exclusive``; its reentrant lease is
    intentionally the exact same lease used by event projection and analysis.
    No SQL Session remains open during high-water, explain, read or delete I/O.
    """
    if not _valid_graph(graph):
        raise ValueError('Unsupported application graph for maintenance')
    if not enabled(mirror) or not mirror.url or not mirror.bootstrapped:
        return False
    if control(engine, mirror)['paused']:
        return False
    from .graph_indexes import ready_for
    if not all(ready_for(mirror, graph, label, 'iso' if label == 'Place' else 'id') for label in LABELS):
        return False
    settings = _settings()
    with mirror.exclusive(engine) as acquired:
        if not acquired:
            return False
        # A request may have paused maintenance while this tick waited for the
        # shared lease. Recheck before creating state or touching FalkorDB.
        if control(engine, mirror)['paused']:
            return False
        _states(engine, mirror, graph)
        row = _pick(engine, mirror, graph, time.time())
        if row is None:
            return False
        # An explain failure must not discard an already established high-water
        # checkpoint. Exact failure stages live separately in diagnostics.
        phase = 'batch' if row.status == 'sweeping' or (row.status == 'error' and row.phase == 'batch') else 'plan'
        try:
            with _timed(mirror, graph, row.label, 'plan'):
                _check_seek(mirror, graph, row.label, settings)
            starting = row.status in ('pending', 'complete') or (row.status == 'error' and row.phase != 'batch')
            cursor, through = row.cursor, row.through_key
            cycle = row.cycle
            if starting:
                phase = 'highwater'
                with _timed(mirror, graph, row.label, 'highwater'):
                    values = _read(mirror, graph, 'MATCH (n:'+row.label+') RETURN max(id(n))', settings['timeout'])
                value = values[0][0] if values and values[0] else None
                through = int(value) if value is not None else -1
                cursor = 0
                cycle += 1
                if through < 0:
                    _persist(engine, mirror, row, dict(cursor=0, through_key=-1, cycle=cycle,
                        status='complete', phase='highwater', cycle_scanned=0, cycle_deleted=0,
                        error='', last_run=utc_now_iso(), next_due=time.time()+settings['interval']))
                    mirror.gc_error = ''
                    return True
                if not _persist(engine, mirror, row, dict(cursor=0, through_key=through, cycle=cycle,
                    status='sweeping', phase='batch', cycle_scanned=0, cycle_deleted=0, error='', next_due=0)):
                    return True
                # Update the detached expected checkpoint for the second short
                # publication below, not a Session retained across graph I/O.
                row.cursor, row.through_key, row.cycle = cursor, through, cycle
                row.cycle_scanned = row.cycle_deleted = 0
            phase = 'batch'
            hi = min(through+1, cursor+settings['window'])
            with _timed(mirror, graph, row.label, 'read'):
                rows = _read(mirror, graph, _select_query(row.label), settings['timeout'],
                             lo=cursor, hi=hi, limit=settings['batch'], label=row.label)
            identifiers = [int(item[0]) for item in rows]
            if len(identifiers) > settings['batch'] or any(ident < cursor or ident >= hi for ident in identifiers):
                raise ValueError('graph_gc_window_protocol_mismatch')
            deleted = 0
            if identifiers:
                with _timed(mirror, graph, row.label, 'delete'):
                    result = mirror.query(graph, _delete_query(row.label),
                        ids=identifiers, label=row.label, _query_timeout_ms=settings['timeout'])
                    deleted = _deleted_count(result)
            # An undersubscribed window is fully inspected. A full page only
            # advances through its final native ID so no matching row is skipped.
            next_cursor = identifiers[-1]+1 if len(identifiers) == settings['batch'] else hi
            complete = next_cursor > through
            _persist(engine, mirror, row, dict(cursor=next_cursor, through_key=through, cycle=cycle,
                status='complete' if complete else 'sweeping', phase='highwater' if complete else 'batch',
                scanned=row.scanned+len(identifiers), deleted=row.deleted+deleted,
                cycle_scanned=row.cycle_scanned+len(identifiers), cycle_deleted=row.cycle_deleted+deleted,
                error='', last_run=utc_now_iso(), next_due=time.time()+settings['interval'] if complete else 0))
            mirror.gc_error = ''
            return True
        except (SQLAlchemyError, LeaseLost):
            # A broken SQL connection or lost owner is handled by the existing
            # worker backoff; do not try another write merely to log that fault.
            raise
        except Exception as exc:
            return _error(engine, mirror, row, exc, phase)


def summary(engine, mirror, graph='active_global'):
    """Cheap read-only status: never contacts FalkorDB or creates maintenance."""
    settings = control(engine, mirror)
    extra = dict(**settings, diagnostics=diagnostics(mirror, graph))
    if not mirror.url:
        return dict(state='unconfigured', last_run='', scanned=None, deleted=None, error='', **extra)
    # Status handlers already hold a read Session. Reuse it rather than
    # recursively acquiring another pool connection (pool_size=1 is valid).
    context=nullcontext(engine) if isinstance(engine,Session) else Session(engine)
    with context as session:
        rows = session.exec(select(GraphGcState).where(GraphGcState.namespace == mirror.prefix,
                                                     GraphGcState.graph == graph)).all()
    if not rows:
        state = 'disabled' if not settings['enabled'] else 'paused' if settings['paused'] else 'waiting'
        return dict(state=state, last_run='', scanned=None, deleted=None, error='', complete_labels=0,
                    labels=len(LABELS), cycle_scanned=0, cycle_deleted=0, **extra)
    errors = [row.error for row in rows if row.error]
    active = next((row for row in rows if row.status == 'sweeping'), None)
    state = 'disabled' if not settings['enabled'] else 'paused' if settings['paused'] else 'error' if errors else 'running' if active else 'idle'
    return dict(state=state,
        phase=active.phase if active else '', label=active.label if active else '',
        last_run=max((row.last_run for row in rows), default=''),
        scanned=sum(row.scanned for row in rows), deleted=sum(row.deleted for row in rows),
        cycle_scanned=sum(row.cycle_scanned for row in rows),
        cycle_deleted=sum(row.cycle_deleted for row in rows),
        cursor=active.cursor if active else None, through_key=active.through_key if active else None,
        cycle=active.cycle if active else None,
        error=errors[0] if errors else '',
        complete_labels=sum(row.status == 'complete' for row in rows), labels=len(LABELS), **extra)


def retry(engine, mirror, graph='active_global'):
    """Reset maintenance backoff only; HTTP retry does not modify graph data."""
    if not _valid_graph(graph):
        raise ValueError('Unsupported application graph for maintenance')
    with Session(engine) as session:
        rows = session.exec(select(GraphGcState).where(GraphGcState.namespace == mirror.prefix,
                                                     GraphGcState.graph == graph)).all()
        for row in rows:
            if row.status == 'error':
                row.next_due = 0
                row.error = ''
                row.updated = utc_now_iso()
                session.add(row)
        session.commit()
    mirror.gc_error = ''
    return dict(ok=True)
