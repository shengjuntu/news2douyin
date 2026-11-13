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
from contextlib import nullcontext

from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from ..extraction.schema import stable_id
from ..storage.articles import lock_news
from ..storage.models import utc_now_iso
from .graph_maintenance_models import GraphGcState
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
    # Label-constrained match: the previous MATCH (n) with a labels() predicate
    # scanned the whole graph on every batch and saturated FalkorDB.
    if label not in LABELS:
        raise ValueError('Unsupported maintenance label')
    return ('MATCH (n:'+label+') WHERE id(n)>=$lo AND id(n)<$hi '
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
    message = '图清理暂未完成（'+type(exc).__name__+'）'
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
    if not mirror.url or not mirror.bootstrapped:
        return False
    from .graph_indexes import ready_for
    if not all(ready_for(mirror, graph, label, 'iso' if label == 'Place' else 'id') for label in LABELS):
        return False
    settings = _settings()
    with mirror.exclusive(engine) as acquired:
        if not acquired:
            return False
        _states(engine, mirror, graph)
        row = _pick(engine, mirror, graph, time.time())
        if row is None:
            return False
        phase = 'plan'
        try:
            _check_seek(mirror, graph, row.label, settings)
            starting = row.status in ('pending', 'complete') or (row.status == 'error' and row.phase != 'batch')
            cursor, through = row.cursor, row.through_key
            cycle = row.cycle
            if starting:
                phase = 'highwater'
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
            rows = _read(mirror, graph, _select_query(row.label), settings['timeout'],
                         lo=cursor, hi=hi, limit=settings['batch'], label=row.label)
            identifiers = [int(item[0]) for item in rows]
            if len(identifiers) > settings['batch'] or any(ident < cursor or ident >= hi for ident in identifiers):
                raise ValueError('graph_gc_window_protocol_mismatch')
            deleted = 0
            if identifiers:
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
    if not mirror.url:
        return dict(state='unconfigured', last_run='', scanned=None, deleted=None, error='')
    # Status handlers already hold a read Session. Reuse it rather than
    # recursively acquiring another pool connection (pool_size=1 is valid).
    context=nullcontext(engine) if isinstance(engine,Session) else Session(engine)
    with context as session:
        rows = session.exec(select(GraphGcState).where(GraphGcState.namespace == mirror.prefix,
                                                     GraphGcState.graph == graph)).all()
    if not rows:
        return dict(state='waiting', last_run='', scanned=None, deleted=None, error='')
    errors = [row.error for row in rows if row.error]
    active = next((row for row in rows if row.status == 'sweeping'), None)
    return dict(state='error' if errors else 'running' if active else 'idle',
        phase=active.phase if active else '', label=active.label if active else '',
        last_run=max((row.last_run for row in rows), default=''),
        scanned=sum(row.scanned for row in rows), deleted=sum(row.deleted for row in rows),
        error=errors[0] if errors else '',
        complete_labels=sum(row.status == 'complete' for row in rows), labels=len(LABELS))


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
