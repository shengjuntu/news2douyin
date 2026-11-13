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
INDEX_PREDICATE = 'token <> written'
_INDEX_LOCK = 627854931492017
_PREFIXES = {'event': 'event/', 'watch': 'watch/'}


def pick(session, prefer_watch=False, *, after_event='', after_watch=''):
    """Prefer one bounded key range, then the other; never load the outbox.

    Optional per-kind cursors make attempts round-robin even when an early key
    is continually invalidated/requeued. The caller advances a cursor after
    every attempt, including a stale acknowledgement, and prefers watches at
    least every eight event attempts. Each range uses one SELECT, or two when
    wrapping a cursor; at most four tiny reads are issued for an empty queue.
    """
    if not isinstance(after_event, str) or not isinstance(after_watch, str):
        raise ValueError('Graph queue cursors must be strings')
    kinds = ('watch', 'event') if prefer_watch else ('event', 'watch')
    for kind in kinds:
        # Range comparison on a non-C collation (glibc en_US.utf8 ignores
        # punctuation at the primary level) makes ['event/', 'event0')
        # exclude every 'event/...' key, so prefix matching is used instead.
        prefix = _PREFIXES[kind]
        query = select(GraphPending).where(GraphPending.token != GraphPending.written,
            GraphPending.key.startswith(prefix)).order_by(GraphPending.key).limit(1)
        cursor = after_watch if kind == 'watch' else after_event
        # Old/restored cursors outside the kind's range do not suppress work.
        if cursor.startswith(prefix):
            row = session.exec(query.where(GraphPending.key > cursor).execution_options(
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


def _verify_postgres(connection):
    row = connection.execute(text('''SELECT i.indisvalid, i.indisready
        FROM pg_index i JOIN pg_class x ON x.oid=i.indexrelid
        WHERE i.indrelid=to_regclass('graphpending') AND x.relname=:name'''), {'name': INDEX_NAME}).first()
    if row is None or not all(row):
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
    try:
        present = _definition(engine)
        if present:
            if engine.dialect.name == 'postgresql':
                with engine.connect() as connection:
                    _verify_postgres(connection)
            return dict(state='ready', index=INDEX_NAME, created=False, valid=True)
        with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
            acquired = False
            try:
                if engine.dialect.name == 'postgresql':
                    acquired = bool(connection.execute(text('SELECT pg_try_advisory_lock(:key)'),
                                                       {'key': _INDEX_LOCK}).scalar())
                    if not acquired:
                        return dict(state='busy', index=INDEX_NAME, created=False, valid=False)
                concurrent = ' CONCURRENTLY' if engine.dialect.name == 'postgresql' else ''
                connection.execute(text('CREATE INDEX'+concurrent+' IF NOT EXISTS "'+INDEX_NAME+
                    '" ON "graphpending" ("key") WHERE '+INDEX_PREDICATE))
                if engine.dialect.name == 'postgresql':
                    _verify_postgres(connection)
            finally:
                if acquired:
                    connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': _INDEX_LOCK})
        # IF NOT EXISTS may have observed another builder. Validate the actual
        # definition, rather than treating the DDL acknowledgement as evidence.
        if not _definition(engine):
            raise QueueIndexError('Graph queue index could not be found after maintenance')
        return dict(state='ready', index=INDEX_NAME, created=True, valid=True)
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


class GraphQueueMetrics:
    """No event IDs or query text; bounded latency/rate/backlog observations."""
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
        self.totals = dict(attempts=0, acknowledged=0, stale=0, failed=0, events=0, watches=0,
                           total_ms=0.0, graph_ms=0.0, sql_ms=0.0, cleanup_ms=0.0)

    def record(self, kind, elapsed_ms, *, acknowledged=False, stale=False, failed=False,
               graph_ms=0, sql_ms=0, cleanup_ms=0):
        if sum(bool(flag) for flag in (acknowledged, stale, failed)) > 1:
            raise ValueError('A projection attempt has at most one outcome')
        now = self.clock()
        sample = dict(elapsed_ms=_duration(elapsed_ms), graph_ms=_duration(graph_ms),
                      sql_ms=_duration(sql_ms), cleanup_ms=_duration(cleanup_ms))
        with self.lock:
            self.totals['attempts'] += 1
            for field, flag in (('acknowledged', acknowledged), ('stale', stale), ('failed', failed)):
                self.totals[field] += int(bool(flag))
            if acknowledged and kind in ('event', 'watch'):
                self.totals['events' if kind == 'event' else 'watches'] += 1
            self.totals['total_ms'] += sample['elapsed_ms']
            for field in ('graph_ms', 'sql_ms', 'cleanup_ms'):
                self.totals[field] += sample[field]
            self.samples.append(sample)
            stamp = int(now)
            if self.buckets and self.buckets[-1][0] == stamp:
                self.buckets[-1][1] += int(bool(acknowledged))
            else:
                self.buckets.append([stamp, int(bool(acknowledged))])
            self._trim(now)

    def _trim(self, now):
        cutoff = now-self.window_seconds
        while self.buckets and self.buckets[0][0]+1 <= cutoff:
            self.buckets.popleft()
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
            totals = {key: round(value, 3) if isinstance(value, float) else value for key, value in self.totals.items()}
            samples = list(self.samples)
            acknowledgements = sum(bucket[1] for bucket in self.buckets)
            elapsed = min(max(0.0, now-self.started), self.window_seconds)
            rate = acknowledgements/elapsed if elapsed >= 1 else None
            values = sorted(sample['elapsed_ms'] for sample in samples)
            latency = dict(last=samples[-1]['elapsed_ms'] if samples else None,
                mean=sum(values)/len(values) if values else None,
                p95=values[max(0, ceil(len(values)*.95)-1)] if values else None,
                max=max(values) if values else None)
            latency = {key: round(value, 3) if value is not None else None for key, value in latency.items()}
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
            return dict(scope='current_process', totals=totals, latency_ms=latency,
                retained_samples=len(samples), capacity=self.capacity,
                recent=dict(window_seconds=round(elapsed, 3), acknowledged=acknowledgements,
                            rate_per_second=round(rate, 6) if rate is not None else None),
                backlog=backlog)
