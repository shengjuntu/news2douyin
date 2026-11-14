"""Compare complete legacy topic rewrites with bounded fingerprinted passes.

Run from a source checkout with optional QA dependencies installed::

    python tools/benchmark_topic_projection.py --events 200 \
      --output docs/validation/0.25.2/topic-projection-benchmark.json

The default owns a new loopback-only PostgreSQL cluster (en_US.utf8), two fresh
databases and a new local FalkorDB process. No service URL or existing data
directory is accepted. ``--database sqlite`` is an explicitly labelled fallback.
Requires QA-only pgserver, psycopg and falkordblite/redislite. Initial schema and
index creation are outside the measured passes. This small local synthetic
comparison is not a production throughput estimate or a field incident replay.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))

from benchmark_graph_projection import isolated_falkor


def _port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@contextmanager
def isolated_postgres(folder, locale):
    import pgserver
    import psycopg
    from psycopg import sql
    binary = Path(pgserver.__file__).parent/'pginstall/bin'
    if hasattr(os, 'geteuid') and os.geteuid() == 0:
        raise SystemExit('The isolated PostgreSQL benchmark requires an unprivileged OS user')
    data = folder/'pg-data'
    subprocess.run([str(binary/'initdb'), '-D', str(data), '-U', 'n2dqa',
        '--auth-local=trust', '--auth-host=trust', '--encoding=UTF8',
        '--locale-provider=libc', '--locale='+locale], check=True, stdout=subprocess.DEVNULL)
    port = _port()
    with (folder/'postgres.log').open('w') as log:
        child = subprocess.Popen([str(binary/'postgres'), '-D', str(data), '-h', '127.0.0.1',
            '-p', str(port), '-k', '', '-c', 'shared_buffers=64MB'], stdout=log, stderr=subprocess.STDOUT)
        native = f'postgresql://n2dqa@127.0.0.1:{port}/postgres'
        try:
            deadline = time.monotonic()+20
            while True:
                try:
                    with psycopg.connect(native, autocommit=True, connect_timeout=1) as connection:
                        version, collation = connection.execute(
                            'SELECT version(),datcollate FROM pg_database WHERE datname=current_database()').fetchone()
                        for name in ('topic_legacy', 'topic_chunked'):
                            connection.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
                    break
                except psycopg.OperationalError:
                    if child.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError('Isolated PostgreSQL did not start')
                    time.sleep(.025)
            yield {kind: f'postgresql+psycopg://n2dqa@127.0.0.1:{port}/topic_{kind}'
                   for kind in ('legacy', 'chunked')}, dict(dialect='postgresql',
                    version=version, locale=collation, fresh_cluster=True)
        finally:
            child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill(); child.wait()


def _stats(values):
    values = sorted(values)
    if not values:
        return dict(samples=0, mean_ms=0, p95_ms=0, max_ms=0)
    return dict(samples=len(values), mean_ms=round(statistics.mean(values), 3),
                p95_ms=round(values[math.ceil(.95*len(values))-1], 3), max_ms=round(values[-1], 3))


class Commands:
    def __init__(self, mirror):
        self.original = mirror._execute
        mirror._execute = self.execute
        self.reset()

    def reset(self):
        self.counts = dict(query=0, read=0, explain=0)
        self.elapsed_ms = 0.0

    def execute(self, command, graph, query, params, timeout=None):
        name = {'GRAPH.QUERY':'query', 'GRAPH.RO_QUERY':'read', 'GRAPH.EXPLAIN':'explain'}[command]
        self.counts[name] += 1
        started = time.perf_counter()
        try:
            return self.original(command, graph, query, params, timeout)
        finally:
            self.elapsed_ms += (time.perf_counter()-started)*1000


def fixture(number, *, corrected=False):
    from news2douyin.extraction.reader import extract
    from news2douyin.desk.models import RadarEntry
    key = f'event_{number:04}'
    article = f'article_{number:04}'
    place, revision = ('Paris', 2) if corrected else ('London', 1)
    body = f'Alice visited {place} yesterday. Dispatch {number}.'
    actions, _ = extract(SimpleNamespace(article_key=article, revision=revision),
        dict(title='', content=body, published_at='2026-11-03T12:00:00Z'), 'rules')
    for action in actions:
        action['decision'] = 'pending'
    return RadarEntry(key=key, title=f'Report {number}'+(' corrected' if corrected else ''),
        seen='2026-11-03T12:00:00Z', priority=1, band='routine', reason='fixture',
        evidence='', headings='[]', material=body, geography='[]',
        sources=json.dumps([dict(key=article, url=f'https://fixture.invalid/{article}', actions=actions)]))


def seed_sql(engine, count):
    from sqlmodel import Session
    from news2douyin.desk.models import Watch, WatchEvent, GraphPending
    with Session(engine) as session:
        session.add(Watch(key='benchmark', name='Benchmark topic', kind='人物', scope='Alice'))
        for number in range(count):
            row = fixture(number)
            session.add(row)
            session.add(WatchEvent(watch='benchmark', event=row.key, choice='include',
                relevance=3, reason='Explicit fixture membership'))
        session.add(GraphPending(key='watch/benchmark', token=uuid4().hex))
        session.commit()


def prepare(engine, mirror):
    from news2douyin.desk import graph_indexes
    from news2douyin.desk.graph_queue import ensure_queue_index
    started = time.perf_counter()
    mirror._queue_index_state = ensure_queue_index(engine)
    mirror._queue_index_checked = mirror._queue_index_state.get('state') == 'ready'
    if not mirror._queue_index_checked:
        raise RuntimeError('Disposable SQL queue index did not become ready')
    mirror.bootstrap(engine)
    mirror.query('focus_benchmark', 'RETURN 1')
    for name in ('active_global', 'focus_benchmark'):
        deadline = time.monotonic()+30
        while True:
            with mirror.exclusive(engine) as acquired:
                if acquired and mirror.prepare_indexes(engine, name):
                    break
            if time.monotonic() >= deadline:
                raise RuntimeError('Benchmark graph indexes did not become ready')
            time.sleep(.001)
    return dict(elapsed_ms=round((time.perf_counter()-started)*1000, 3),
                indexes=graph_indexes.status_summary(mirror), queue_index=dict(mirror._queue_index_state))


def retokenize(engine, *, corrected=None):
    from sqlmodel import Session
    from news2douyin.desk.models import GraphPending
    with Session(engine) as session:
        if corrected is not None:
            session.merge(fixture(corrected, corrected=True))
        row = session.get(GraphPending, 'watch/benchmark')
        row.token = uuid4().hex
        session.add(row)
        session.commit()


def _observe_sql(engine):
    from sqlalchemy import event
    counter = dict(calls=0, milliseconds=0.0, benchmark_inspection_calls=0, benchmark_inspection_ms=0.0)
    def before(_conn, _cursor, _statement, _params, context, _many):
        name = 'benchmark_inspection_calls' if context.execution_options.get('n2d_topic_benchmark_inspection') else 'calls'
        counter[name] += 1
        context._topic_benchmark_started = time.perf_counter()
    def after(_conn, _cursor, _statement, _params, context, _many):
        name = 'benchmark_inspection_ms' if context.execution_options.get('n2d_topic_benchmark_inspection') else 'milliseconds'
        counter[name] += (time.perf_counter()-context._topic_benchmark_started)*1000
    event.listen(engine, 'before_cursor_execute', before)
    event.listen(engine, 'after_cursor_execute', after)
    return counter, lambda: (event.remove(engine, 'before_cursor_execute', before),
                             event.remove(engine, 'after_cursor_execute', after))


def legacy_pass(engine, mirror, commands):
    from sqlalchemy import update
    from sqlmodel import Session, select
    from news2douyin.desk.models import GraphPending, RadarEntry, Watch, WatchEvent
    from news2douyin.storage.articles import lock_news
    commands.reset()
    sql, stop_sql = _observe_sql(engine)
    started = time.perf_counter()
    try:
        with mirror.exclusive(engine) as acquired:
            if not acquired:
                raise RuntimeError('Disposable benchmark lease unexpectedly busy')
            with Session(engine, expire_on_commit=False) as session:
                w = session.get(Watch, 'benchmark')
                row = session.get(GraphPending, 'watch/benchmark')
                # Same legacy full payload load, before all graph commands.
                events = session.exec(select(RadarEntry, WatchEvent).join(WatchEvent,
                    WatchEvent.event == RadarEntry.key).where(WatchEvent.watch == w.key,
                        WatchEvent.choice != 'exclude').order_by(WatchEvent.event)).all()
            mirror.watch(w, events, [], [], [], [])
            with Session(engine) as session:
                lease = mirror._projection_lease
                lock_news(session, 'graph-queue/'+row.key, 'lease/'+lease.key)
                lease.confirm(session)
                result = session.exec(update(GraphPending).where(GraphPending.key == row.key,
                    GraphPending.token == row.token).values(written=row.token))
                if result.rowcount != 1:
                    raise RuntimeError('Unexpected synthetic outbox invalidation')
                session.commit()
    finally:
        stop_sql()
    elapsed = (time.perf_counter()-started)*1000
    return dict(elapsed_ms=round(elapsed, 3), graph_commands=dict(commands.counts),
        graph_command_ms=round(commands.elapsed_ms, 3), sql=sql, chunks=_stats([elapsed]),
        scanned_event_rows=len(events), changed_event_rows=len(events),
        max_event_rows_loaded_together=len(events),
        note='Legacy full SQL load + one graph lease + retained watch() + conditional outbox acknowledgement; no worker sleeps')


def chunked_pass(engine, mirror, commands, count):
    from sqlmodel import Session
    from news2douyin.desk.models import GraphPending
    from news2douyin.desk.graph_focus_models import GraphFocusState
    commands.reset()
    sql, stop_sql = _observe_sql(engine)
    chunks = []
    previous_scanned = previous_changed = 0
    started = time.perf_counter()
    try:
        for _ in range(count*4+200):
            with Session(engine) as session:
                session.connection(execution_options={'n2d_topic_benchmark_inspection':True})
                state = session.get(GraphFocusState, (mirror.prefix, 'benchmark'))
                phase = state.phase if state and state.phase != 'complete' else 'new_pass'
            before_calls = commands.counts['query']
            tick = time.perf_counter()
            mirror.step(engine)
            elapsed = (time.perf_counter()-tick)*1000
            with Session(engine) as session:
                session.connection(execution_options={'n2d_topic_benchmark_inspection':True})
                row = session.get(GraphPending, 'watch/benchmark')
                state = session.get(GraphFocusState, (mirror.prefix, 'benchmark'))
                if state:
                    scanned, changed = state.scanned, state.changed
                    after = state.phase
                else:
                    scanned = changed = 0
                    after = 'indexes'
                complete = row.token == row.written
            chunks.append(dict(phase_before=phase, phase_after=after, elapsed_ms=round(elapsed, 3),
                graph_query_commands=commands.counts['query']-before_calls,
                scanned_rows=max(0, scanned-previous_scanned), changed_items=max(0, changed-previous_changed)))
            previous_scanned, previous_changed = scanned, changed
            if complete:
                break
        else:
            raise RuntimeError('Chunked topic pass did not acknowledge')
    finally:
        stop_sql()
    elapsed = (time.perf_counter()-started)*1000
    return dict(elapsed_ms=round(elapsed, 3), graph_commands=dict(commands.counts),
        graph_command_ms=round(commands.elapsed_ms, 3), sql=sql,
        chunks=_stats([row['elapsed_ms'] for row in chunks]), chunk_details=chunks,
        worker_steps_total_ms=round(sum(row['elapsed_ms'] for row in chunks), 3),
        benchmark_inspection_and_loop_ms=round(elapsed-sum(row['elapsed_ms'] for row in chunks), 3),
        scanned_event_rows=sum(row['scanned_rows'] for row in chunks if row['phase_before'] == 'events'),
        changed_event_rows=sum(row['changed_items'] for row in chunks if row['phase_before'] == 'events'),
        max_event_rows_loaded_together=min(count, int(os.environ['NEWS2DOUYIN_GRAPH_FOCUS_BATCH_SIZE'])),
        note='Public step() calls; each owns/releases its lease; no worker sleeps. Full-pass elapsed includes harness checkpoint inspection, split out from worker_steps_total_ms. SQL calls/milliseconds exclude separately named benchmark inspection counters.')


def coverage(mirror):
    graph = 'focus_benchmark'
    material = mirror.read(graph,
        'MATCH (e:Event)-[:CITES]->(a:Article)-[:DESCRIBES]->(m:EventMention) '
        'RETURN e.id,e.title,a.id,a.url,m.id,m.revision,m.quote,m.kind,m.status,m.date '
        'ORDER BY e.id,a.id,m.id')
    return dict(nodes=mirror.read(graph, 'MATCH (n) RETURN count(n)')[0][0],
        events=mirror.read(graph, 'MATCH (e:Event) RETURN count(e)')[0][0],
        cites=mirror.read(graph, 'MATCH (:Event)-[r:CITES]->() RETURN count(r)')[0][0],
        current_mentions=mirror.read(graph,
            'MATCH (:Event)-[:CITES]->(:Article)-[:DESCRIBES]->(m:EventMention) RETURN count(DISTINCT m)')[0][0],
        current_material_sha256=hashlib.sha256(json.dumps(material, ensure_ascii=False).encode()).hexdigest(),
        corrected_mentions=sum(row[5] == 2 and 'Paris' in row[6] for row in material),
        maximum_reachable_nodes_per_event=mirror.read(graph,
            'MATCH (e:Event)-[:CITES]->(a:Article) '
            'OPTIONAL MATCH (a)-[:DESCRIBES]->(m:EventMention) '
            'OPTIONAL MATCH (m)-[:HAS_ARGUMENT]->(g:Argument)-[:REFERS_TO]->(r:Referent) '
            'OPTIONAL MATCH (m)-[:HAS_MATCH_KEY]->(o:OccurrenceCandidate) '
            'WITH e,count(DISTINCT a) AS ac,count(DISTINCT m) AS mc,count(DISTINCT g) AS gc,'
            'count(DISTINCT r) AS rc,count(DISTINCT o) AS oc RETURN max(1+ac+mc+gc+rc+oc)')[0][0])


def compare(urls, database, client, port, folder, count, batch):
    from news2douyin.desk.graph import GraphMirror
    from news2douyin.desk import graph_indexes, graph_focus
    from news2douyin.storage.db import init_db, make_engine
    os.environ['NEWS2DOUYIN_FALKOR_URL'] = f'redis://127.0.0.1:{port}'
    os.environ['NEWS2DOUYIN_GRAPH_FOCUS_BATCH_SIZE'] = str(batch)
    os.environ['NEWS2DOUYIN_GRAPH_FOCUS_BATCH_MS'] = '500'
    graph_indexes.POLL_INTERVAL = .001
    result = dict(database=database, events=count, batch_size=batch,
        fixture=dict(articles_per_event=1, extracted_actions_per_article=1, arguments_per_action=2,
            intent_definition=False, investigations=0, shared_article=False,
            note='Real rule extraction of synthetic Alice visit reports; source links intentionally use fixture.invalid'),
        legacy_watch_sha256=hashlib.sha256(inspect.getsource(GraphMirror.watch).encode()).hexdigest(),
        chunked_module_sha256=hashlib.sha256(Path(graph_focus.__file__).read_bytes()).hexdigest(), modes={})
    for kind in ('legacy', 'chunked'):
        engine = make_engine(urls[kind])
        try:
            init_db(engine)
            seed_sql(engine, count)
            os.environ['NEWS2DOUYIN_GRAPH_PREFIX'] = 'topicbench_'+kind+'_'+uuid4().hex[:8]
            mirror = GraphMirror()
            setup = prepare(engine, mirror)
            setup['search_index_warning'] = getattr(engine, '_n2d_search_warning', '')
            commands = Commands(mirror)
            run = (lambda: legacy_pass(engine, mirror, commands)) if kind == 'legacy' else (
                lambda: chunked_pass(engine, mirror, commands, count))
            initial = run()
            retokenize(engine, corrected=count//2)
            correction = run()
            retokenize(engine)
            unchanged = run()
            observed = coverage(mirror)
            if (observed['events'] != count or observed['cites'] != count
                    or observed['current_mentions'] != count or observed['corrected_mentions'] != 1):
                raise RuntimeError('Benchmark lost synthetic event/source/mention coverage')
            if kind == 'chunked' and (correction['changed_event_rows'] != 1 or unchanged['graph_commands']['query'] != 0):
                raise RuntimeError('Incremental/unchanged pass contract failed')
            result['modes'][kind] = dict(setup=setup, initial=initial, one_member_correction=correction,
                unchanged=unchanged, coverage_after_correction=observed)
        finally:
            engine.dispose()
    if result['modes']['legacy']['coverage_after_correction']['current_material_sha256'] != result['modes']['chunked']['coverage_after_correction']['current_material_sha256']:
        raise RuntimeError('Legacy/chunked current material differs')
    result['limitations'] = [
        'Single local run with at most 256 synthetic member events; no production contention or upstream ingestion.',
        'Legacy baseline is the retained GraphMirror.watch method, not a separately installed old product.',
        'Both modes use identical indexed material; schema/index setup is excluded from pass timings.',
        'Chunked passes still scan all current topic members and update SQL receipts; changed graph writes alone are incremental.',
        'Configured millisecond budget is checked between material items and cannot preempt an individual SQL/Cypher operation.',
        'SQL counts use SQLAlchemy cursor hooks; driver-level pool pre-ping is not included in those counts, but its time remains included.',
        'No intent graph, investigations, background GC, worker idle delays or model calls are in this fixture.',
        'These figures cannot establish field single-event latency, explain its 9-second mixed mean, or predict production throughput.']
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', type=int, default=200)
    parser.add_argument('--batch-size', type=int, default=8)
    parser.add_argument('--database', choices=('postgresql', 'sqlite'), default='postgresql')
    parser.add_argument('--locale', default='en_US.utf8')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.events <= 256 or not 1 <= args.batch_size <= 100:
        parser.error('events must be 1..256 and batch-size 1..100')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='n2d-topic-benchmark-') as temporary:
        folder = Path(temporary)
        with isolated_falkor(folder) as (client, port, config):
            if args.database == 'postgresql':
                with isolated_postgres(folder, args.locale) as (urls, database):
                    result = compare(urls, database, client, port, folder, args.events, args.batch_size)
            else:
                urls = {kind:'sqlite:///'+str(folder/(kind+'.db')) for kind in ('legacy', 'chunked')}
                result = compare(urls, dict(dialect='sqlite', fresh_files=True, locale='not_applicable'),
                    client, port, folder, args.events, args.batch_size)
            result['falkor_modules'] = client.execute_command('MODULE', 'LIST')
            result['falkor_timeout_config'] = config
    result['generated_at_utc'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    result['benchmark_tool_sha256'] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(dict(output=str(args.output.resolve()), database=result['database'],
        measured_pass_ms={kind:{name:data[name]['elapsed_ms'] for name in ('initial','one_member_correction','unchanged')}
            for kind,data in result['modes'].items()}), ensure_ascii=False))


if __name__ == '__main__':
    main()
