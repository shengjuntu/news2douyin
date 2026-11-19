"""Small graph outbox reads and bounded, process-local projection telemetry.

The caller still owns the global graph lease. Picking a row is deliberately a
read, not a claim held open across graph I/O; publication must compare its token
and count an acknowledgement only when the conditional UPDATE changes one row.
"""
from collections import deque
from math import ceil, isfinite
from threading import RLock
from time import monotonic
import re

from sqlalchemy import inspect, text
from sqlmodel import select

from .models import GraphPending


INDEX_NAME = 'ix_graphpending_pending_key'
# A different name deliberately preserves v0.25.1's default-collation index.
# Merely attaching COLLATE "C" to the query does not make that old index a
# usable range index on an en_US database.
POSTGRES_INDEX_NAME = 'ix_graphpending_pending_key_c_v2'
INDEX_PREDICATE = 'token <> written'
_INDEX_LOCK = 627854931492017
_PREFIXES = {'event': ('event/', 'event0'), 'watch': ('watch/', 'watch0'),
             'attention': ('attention/', 'attention0'), 'official': ('official/', 'official0')}


def pick(session, prefer_watch=False, *, after_event='', after_watch='',
         include_attention=False, prefer_attention=False, after_attention='',
         include_official=False, prefer_official=False, after_official=''):
    """Prefer one bounded key range, then the other; never load the outbox.

    Optional per-kind cursors make attempts round-robin even when an early key
    is continually invalidated/requeued. The caller advances a cursor after
    every attempt, including a stale acknowledgement, and prefers watches at
    least every eight event attempts. Each range uses one SELECT, or two when
    wrapping a cursor; at most four tiny reads are issued for an empty queue.
    GraphMirror opts into the third attention range (at most six empty reads),
    with its own cursor and reserved priority turn. Legacy two-range callers
    keep their query contract and never consume another subsystem's updates.
    The same opt-in applies to the selected official event evidence range.
    """
    if any(not isinstance(value, str) for value in (after_event, after_watch, after_attention, after_official)):
        raise ValueError('Graph queue cursors must be strings')
    kinds = ('watch', 'event') if prefer_watch else ('event', 'watch')
    if include_attention:
        kinds = ('attention', 'event', 'watch') if prefer_attention else (*kinds, 'attention')
    if include_official:
        kinds = ('official', *kinds) if prefer_official else (*kinds, 'official')
    key = (GraphPending.key.collate('C') if session.get_bind().dialect.name == 'postgresql'
           else GraphPending.key)
    for kind in kinds:
        lower, upper = _PREFIXES[kind]
        query = select(GraphPending).where(GraphPending.token != GraphPending.written,
            key >= lower, key < upper).order_by(key).limit(1)
        cursor = {'event': after_event, 'watch': after_watch, 'attention': after_attention,
                  'official': after_official}[kind]
        # Old/restored cursors outside the kind's range do not suppress work.
        if cursor.startswith(lower):
            row = session.exec(query.where(key > cursor).execution_options(
                n2d_query_name='desk.graph_queue.pick.'+kind)).first()
            if row is not None:
                return row
        row = session.exec(query.execution_options(n2d_query_name='desk.graph_queue.pick.'+kind)).first()
        if row is not None:
            return row
    return None


class QueueIndexError(RuntimeError):
    """Safe descriptions; no connection URL, SQL values or original rows."""


def _predicate(value):
    value = re.sub(r'\s+', '', (str(value) if value is not None else '').lower()).replace('"', '')
    value = re.sub(r'::(?:text|charactervarying)(?:\(\d+\))?', '', value)
    value = value.replace('graphpending.', '').replace('(', '').replace(')', '')
    return value


def _definition(engine):
    if engine.dialect.name == 'postgresql':
        with engine.connect() as connection:
            row = _postgres_definition(connection)
        if row is None:
            return False
        # Inspect the catalog instead of just accepting a matching name or an
        # Index Scan plan. The range predicates require exactly this collation.
        if (row['indisunique'] or row['amname'] != 'btree'
                or _predicate(row['predicate']) != 'token<>written'
                or row['indnkeyatts'] != 1 or row['indnatts'] != 1
                or row['attname'] != 'key' or not row['c_collation']
                or row['opcname'] != 'text_ops'):
            raise QueueIndexError('Graph queue index name has a different definition; inspect it before retrying')
        return True
    for row in inspect(engine).get_indexes('graphpending'):
        if row['name'] != INDEX_NAME:
            continue
        options = row.get('dialect_options', {})
        where = options.get(engine.dialect.name+'_where')
        if (row.get('column_names') != ['key'] or row.get('unique')
                or _predicate(where) != 'token<>written'
                or options.get('postgresql_using') not in (None, 'btree')
                or options.get('postgresql_include')):
            raise QueueIndexError('Graph queue index name has a different definition; inspect it before retrying')
        return True
    return False


def _postgres_definition(connection):
    return connection.execute(text('''SELECT i.indisunique,i.indisvalid,i.indisready,
        am.amname,i.indnkeyatts,i.indnatts,a.attname,op.opcname,
        pg_get_expr(i.indpred,i.indrelid) AS predicate,
        i.indcollation[0]='pg_catalog."C"'::regcollation AS c_collation
        FROM pg_index i JOIN pg_class x ON x.oid=i.indexrelid
        JOIN pg_am am ON am.oid=x.relam
        LEFT JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=i.indkey[0]
        LEFT JOIN pg_opclass op ON op.oid=i.indclass[0]
        WHERE i.indrelid=to_regclass('graphpending') AND x.relname=:name'''),
        {'name': POSTGRES_INDEX_NAME}).mappings().first()


def _verify_postgres(connection):
    row = _postgres_definition(connection)
    if row is None or not row['indisvalid'] or not row['indisready']:
        raise QueueIndexError('Graph queue index is invalid or unfinished; inspect the failed build before retrying')


def ensure_queue_index(engine):
    """Check/build the partial pending index from a background maintenance tick.

    PostgreSQL uses CONCURRENTLY on an autocommit connection, with a dedicated
    nonblocking advisory lock to avoid two concurrent builders. Existing
    definitions and invalid builds are never dropped or silently replaced.
    SQLite writes only when this new index is missing; ordinary HTTP reads do
    not invoke this function.
    """
    if engine.dialect.name not in ('sqlite', 'postgresql'):
        raise QueueIndexError('Graph queue index requires SQLite or PostgreSQL')
    name = POSTGRES_INDEX_NAME if engine.dialect.name == 'postgresql' else INDEX_NAME
    try:
        present = _definition(engine)
        if present:
            if engine.dialect.name == 'postgresql':
                with engine.connect() as connection:
                    _verify_postgres(connection)
            return dict(state='ready', index=name, created=False, valid=True)
        with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
            acquired = False
            try:
                if engine.dialect.name == 'postgresql':
                    acquired = bool(connection.execute(text('SELECT pg_try_advisory_lock(:key)'),
                                                       {'key': _INDEX_LOCK}).scalar())
                    if not acquired:
                        return dict(state='busy', index=name, created=False, valid=False)
                concurrent = ' CONCURRENTLY' if engine.dialect.name == 'postgresql' else ''
                column = '"key" COLLATE "C"' if engine.dialect.name == 'postgresql' else '"key"'
                connection.execute(text('CREATE INDEX'+concurrent+' IF NOT EXISTS "'+name+
                    '" ON "graphpending" ('+column+') WHERE '+INDEX_PREDICATE))
                if engine.dialect.name == 'postgresql':
                    _verify_postgres(connection)
            finally:
                if acquired:
                    connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': _INDEX_LOCK})
        # IF NOT EXISTS may have observed another builder. Validate the actual
        # definition, rather than treating the DDL acknowledgement as evidence.
        if not _definition(engine):
            raise QueueIndexError('Graph queue index could not be found after maintenance')
        return dict(state='ready', index=name, created=True, valid=True)
    except QueueIndexError:
        raise
    except Exception:
        raise QueueIndexError('Graph queue index maintenance did not complete; inspect database availability and retry') from None


def _duration(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, value) if isfinite(value) else 0.0


def _totals():
    return dict(attempts=0, acknowledged=0, stale=0, failed=0, partial=0,
                total_ms=0.0, graph_ms=0.0, sql_ms=0.0, cleanup_ms=0.0,
                load_ms=0.0, ack_ms=0.0, other_ms=0.0, graph_calls=0)


def _rounded_totals(totals):
    return {key: round(value, 3) if isinstance(value, float) else value for key, value in totals.items()}


def _latency(samples):
    values = sorted(sample['elapsed_ms'] for sample in samples)
    values = dict(last=samples[-1]['elapsed_ms'] if samples else None,
                  mean=sum(values)/len(values) if values else None,
                  p95=values[max(0, ceil(len(values)*.95)-1)] if values else None,
                  max=max(values) if values else None)
    return {key: round(value, 3) if value is not None else None for key, value in values.items()}


class GraphQueueMetrics:
    """Bounded telemetry, with separate event and topic latency distributions.

    An attempt can be a partial topic batch. Only the final conditional outbox
    update counts as acknowledged. Stage timings are wallclock observations,
    not CPU time; the legacy sql_ms field retains the caller-supplied value.
    Unknown kinds are folded into a fixed ``other`` bucket and never retained.
    """
    def __init__(self, capacity=256, window_seconds=300, *, clock=None):
        if not isinstance(capacity, int) or not 8 <= capacity <= 4096:
            raise ValueError('capacity must be between 8 and 4096')
        if not 1 <= window_seconds <= 3600:
            raise ValueError('window_seconds must be between 1 and 3600')
        self.capacity = capacity
        self.window_seconds = float(window_seconds)
        self.clock = clock or monotonic
        self.started = self.clock()
        self.lock = RLock()
        self.samples = deque(maxlen=capacity)
        self.buckets = deque(maxlen=ceil(window_seconds)+2)
        self.backlogs = deque(maxlen=128)
        self.totals = dict(_totals(), events=0, watches=0)
        self.by_kind = {kind: dict(totals=_totals(), samples=deque(maxlen=capacity),
                                  buckets=deque(maxlen=ceil(window_seconds)+2))
                        for kind in ('event', 'watch', 'other')}

    def record(self, kind, elapsed_ms, *, acknowledged=False, stale=False, failed=False,
               graph_ms=0, sql_ms=0, cleanup_ms=0, partial=False,
               graph_calls=0, load_ms=0, ack_ms=0):
        if sum(bool(flag) for flag in (acknowledged, stale, failed, partial)) > 1:
            raise ValueError('A projection attempt has at most one outcome')
        kind = kind if kind in ('event', 'watch') else 'other'
        now = self.clock()
        sample = dict(elapsed_ms=_duration(elapsed_ms), graph_ms=_duration(graph_ms),
                      sql_ms=_duration(sql_ms), cleanup_ms=_duration(cleanup_ms),
                      load_ms=_duration(load_ms), ack_ms=_duration(ack_ms))
        sample['other_ms'] = max(0.0, sample['elapsed_ms']-sample['graph_ms']-sample['load_ms']-sample['ack_ms'])
        calls = int(_duration(graph_calls))
        with self.lock:
            group = self.by_kind[kind]
            for totals in (self.totals, group['totals']):
                totals['attempts'] += 1
                for field, flag in (('acknowledged', acknowledged), ('stale', stale),
                                    ('failed', failed), ('partial', partial)):
                    totals[field] += int(bool(flag))
                totals['total_ms'] += sample['elapsed_ms']
                for field in ('graph_ms', 'sql_ms', 'cleanup_ms', 'load_ms', 'ack_ms', 'other_ms'):
                    totals[field] += sample[field]
                totals['graph_calls'] += calls
            if acknowledged and kind != 'other':
                self.totals['events' if kind == 'event' else 'watches'] += 1
            self.samples.append(sample)
            group['samples'].append(sample)
            stamp = int(now)
            for buckets in (self.buckets, group['buckets']):
                if buckets and buckets[-1][0] == stamp:
                    buckets[-1][1] += int(bool(acknowledged))
                else:
                    buckets.append([stamp, int(bool(acknowledged))])
            self._trim(now)

    def _trim(self, now):
        cutoff = now-self.window_seconds
        for buckets in (self.buckets, *(group['buckets'] for group in self.by_kind.values())):
            while buckets and buckets[0][0]+1 <= cutoff:
                buckets.popleft()
        while len(self.backlogs) > 2 and self.backlogs[1][0] <= cutoff:
            self.backlogs.popleft()

    def snapshot(self, pending=None):
        if pending is not None and (not isinstance(pending, int) or isinstance(pending, bool) or pending < 0):
            raise ValueError('pending must be a nonnegative integer')
        now = self.clock()
        with self.lock:
            if pending is not None:
                if self.backlogs and now-self.backlogs[-1][0] < 1:
                    # Keep the older baseline so closely spaced readers do not
                    # erase the observation needed to estimate a net drain.
                    if len(self.backlogs) == 1 and now > self.backlogs[-1][0]:
                        self.backlogs.append((now, pending))
                    else:
                        self.backlogs[-1] = (now, pending)
                else:
                    self.backlogs.append((now, pending))
            self._trim(now)
            totals = _rounded_totals(self.totals)
            samples = list(self.samples)
            acknowledgements = sum(bucket[1] for bucket in self.buckets)
            elapsed = min(max(0.0, now-self.started), self.window_seconds)
            rate = acknowledgements/elapsed if elapsed >= 1 else None
            latency = _latency(samples)
            by_kind = {}
            for kind, group in self.by_kind.items():
                count = sum(bucket[1] for bucket in group['buckets'])
                group_samples = list(group['samples'])
                by_kind[kind] = dict(totals=_rounded_totals(group['totals']),
                    latency_ms=_latency(group_samples), retained_samples=len(group_samples), capacity=self.capacity,
                    recent=dict(window_seconds=round(elapsed, 3), acknowledged=count,
                                rate_per_second=round(count/elapsed, 6) if elapsed >= 1 else None))
            backlog = dict(pending=None, observed_age_seconds=None, net_rate_per_second=None,
                           eta_seconds=None, observation_seconds=None)
            if self.backlogs:
                last_time, last_pending = self.backlogs[-1]
                backlog.update(pending=last_pending, observed_age_seconds=round(max(0, now-last_time), 3))
                if last_pending == 0:
                    backlog['eta_seconds'] = 0
                if len(self.backlogs) >= 2:
                    first_time, first_pending = self.backlogs[0]
                    duration = last_time-first_time
                    if duration >= 1:
                        net = (first_pending-last_pending)/duration
                        backlog['net_rate_per_second'] = round(net, 6)
                        backlog['observation_seconds'] = round(duration, 3)
                        if net > 0:
                            backlog['eta_seconds'] = ceil(last_pending/net)
            return dict(scope='current_process', totals=totals, latency_ms=latency, by_kind=by_kind,
                timing_basis='wallclock; load_ms and ack_ms are stage durations; sql_ms is a legacy caller-supplied field',
                retained_samples=len(samples), capacity=self.capacity,
                recent=dict(window_seconds=round(elapsed, 3), acknowledged=acknowledgements,
                            rate_per_second=round(rate, 6) if rate is not None else None),
                backlog=backlog)
