"""Isolated FalkorDB projection benchmark; never opens a production graph.

Requires the optional QA-only redislite/FalkorDB module and a prior source tree.
Compares old/new event() on the same reachable synthetic graph with and without
identity indexes. PostgreSQL, leases, queue acknowledgement, GC and index build
time are deliberately reported separately from steady event graph writes.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import contextmanager
import hashlib
import json
import math
from pathlib import Path
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from types import MethodType, SimpleNamespace
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))


def port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@contextmanager
def isolated_falkor(output):
    import redislite
    from redis import Redis
    binary = Path(redislite.__file__).parent/'bin'
    if not (binary/'falkordb.so').is_file():
        raise SystemExit('QA FalkorDB module unavailable; install falkordblite for this local check')
    with tempfile.TemporaryDirectory(prefix='n2d-graph-bench-') as folder:
        endpoint = port()
        with (output/'falkor.log').open('w') as log:
            child = subprocess.Popen([str(binary/'redis-server'), '--loadmodule', str(binary/'falkordb.so'),
                '--port', str(endpoint), '--bind', '127.0.0.1', '--dir', folder,
                '--save', '', '--appendonly', 'no'], stdout=log, stderr=subprocess.STDOUT)
            client = Redis(host='127.0.0.1', port=endpoint, decode_responses=True,
                           socket_connect_timeout=1, socket_timeout=60)
            try:
                for _ in range(200):
                    try:
                        if client.ping():
                            break
                    except Exception:
                        if child.poll() is not None:
                            raise RuntimeError((output/'falkor.log').read_text())
                    time.sleep(.025)
                else:
                    raise RuntimeError('Temporary FalkorDB did not start')
                config = {}
                for key, value in [('TIMEOUT_MAX', 60000), ('TIMEOUT_DEFAULT', 30000)]:
                    try:
                        client.execute_command('GRAPH.CONFIG', 'SET', key, value)
                        config[key] = client.execute_command('GRAPH.CONFIG', 'GET', key)
                    except Exception as exc:
                        config[key] = type(exc).__name__
                yield client, endpoint, config
            finally:
                client.close()
                child.terminate()
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill(); child.wait()


def load_baseline(root):
    """Freeze both baseline event and extraction projection, not current helpers."""
    from news2douyin.extraction.schema import stable_id, referent, cluster_key, entity_key
    extraction = root/'src/news2douyin/extraction/graph.py'
    namespace = dict(stable_id=stable_id, referent=referent, cluster_key=cluster_key, entity_key=entity_key)
    tree = ast.parse(extraction.read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in ('project', 'clean')]
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(extraction), 'exec'), namespace)
    graph = root/'src/news2douyin/desk/graph.py'
    tree = ast.parse(graph.read_text())
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'GraphMirror')
    event = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == 'event')
    env = dict(json=json, uuid4=uuid4, extraction_graph=SimpleNamespace(project=namespace['project'], clean=namespace['clean']))
    exec(compile(ast.Module(body=[event], type_ignores=[]), str(graph), 'exec'), env)
    return env['event'], dict(event_sha256=hashlib.sha256(ast.get_source_segment(graph.read_text(), event).encode()).hexdigest(),
                              extraction_sha256=hashlib.sha256(extraction.read_bytes()).hexdigest())


def command(client, graph, query, **params):
    prefix = ' '.join(key+'='+json.dumps(value, ensure_ascii=False) for key, value in params.items())
    return client.execute_command('GRAPH.QUERY', graph, ('CYPHER '+prefix+' ' if prefix else '')+query)


def seed(client, graph, count):
    """Every seeded source/action/argument has a live parent; GC finds no junk."""
    started = time.perf_counter()
    for offset in range(0, count, 1000):
        rows = [[str(i), 'person_'+str(i), 'organization_'+str(i)] for i in range(offset, min(count, offset+1000))]
        command(client, graph, 'UNWIND $rows AS row CREATE (e:Event {id:"fixture_event_"+row[0],title:"fixture",seen:"2026-11-01"}) '
                'CREATE (a:Article {id:"fixture_article_"+row[0],title:"fixture",domain:"fixture.example",duplicate_root:"fixture_article_"+row[0]}) '
                'CREATE (e)-[:HAS_SOURCE]->(a) CREATE (p:Entity {id:row[1]}) CREATE (o:Entity {id:row[2]}) '
                'CREATE (a)-[:MENTIONS]->(p) CREATE (a)-[:MENTIONS]->(o)', rows=rows)
    # Bulk fixture construction itself must not perform N label scans. This
    # temporary index is removed before the unindexed measurements.
    command(client, graph, 'CREATE INDEX FOR (a:Article) ON (a.id)')
    deadline=time.monotonic()+60
    while True:
        rows=command(client,graph,'CALL db.indexes() YIELD label,status RETURN label,status')[1]
        if rows and all(row[1]=='OPERATIONAL' for row in rows):break
        if time.monotonic()>=deadline:raise RuntimeError('Fixture seed index did not become ready')
        time.sleep(.025)
    for offset in range(0, count, 5000):
        rows = [[str(i), 'referent_'+str(i)] for i in range(offset, min(count, offset+5000), 5)]
        command(client, graph, 'UNWIND $rows AS row MATCH (a:Article {id:"fixture_article_"+row[0]}) '
                'CREATE (m:EventMention {id:"fixture_mention_"+row[0],kind:"meeting",status:"reported"}) '
                'CREATE (a)-[:DESCRIBES]->(m) CREATE (r:Referent {id:row[1],name:row[1]}) '
                'CREATE (x:Argument {id:"fixture_argument_"+row[0],role:"actor"}) '
                'CREATE (y:Argument {id:"fixture_argument_peer_"+row[0],role:"participant"}) '
                'CREATE (m)-[:HAS_ARGUMENT]->(x) CREATE (m)-[:HAS_ARGUMENT]->(y) '
                'CREATE (x)-[:REFERS_TO]->(r) CREATE (y)-[:REFERS_TO]->(r)', rows=rows)
    command(client, graph, 'CREATE (:Place {iso:"zz"}), (:OccurrenceCandidate {id:"fixture_candidate"})')
    command(client, graph, 'DROP INDEX ON :Article(id)')
    # This intentionally orphaned candidate is a one-time warm-up cleanup and
    # cannot change any measured live source/action material.
    return (time.perf_counter()-started)*1000


INDEXES = [('Event', 'id'), ('Article', 'id'), ('Entity', 'id'), ('EventMention', 'id'),
           ('Argument', 'id'), ('Referent', 'id'), ('OccurrenceCandidate', 'id'), ('Place', 'iso')]


def add_indexes(client, graph):
    started = time.perf_counter()
    for label, prop in INDEXES:
        command(client, graph, 'CREATE INDEX FOR (n:'+label+') ON (n.'+prop+')')
    deadline = time.monotonic()+60
    while True:
        result = command(client, graph, 'CALL db.indexes() YIELD label,properties,status RETURN label,properties,status')
        rows = result[1]
        if len(rows) == len(INDEXES) and all(row[2] == 'OPERATIONAL' for row in rows):
            return (time.perf_counter()-started)*1000
        if time.monotonic() >= deadline:
            raise RuntimeError('Benchmark fixture indexes did not become operational')
        time.sleep(.025)


def summary(samples):
    totals = sorted(sample['elapsed_ms'] for sample in samples)
    return dict(samples=len(samples), mean_ms=statistics.mean(totals), median_ms=statistics.median(totals),
                p95_ms=totals[max(0, math.ceil(.95*len(totals))-1)], min_ms=totals[0], max_ms=totals[-1],
                mean_queries=statistics.mean(sample['query_count'] for sample in samples),
                mean_cleanup_ms=statistics.mean(sample['cleanup_ms'] for sample in samples),
                raw=samples)


def run_condition(client, endpoint, prefix, baseline_event, samples):
    from news2douyin.desk.graph import GraphMirror
    data = SimpleNamespace(title='Benchmark target', seen='2026-11-01', sources=json.dumps([dict(
        key='benchmark_target_article', title='Benchmark target', domain='target.example', persons=['benchmark person'],
        orgs=[], mention_iso=[], actions=[], duplicate_of='')]))
    projectors = {}
    measurements = {'baseline':[], 'baseline_without_gc':[], 'new':[]}
    for name in measurements:
        mirror = GraphMirror(); mirror.url='redis://127.0.0.1:'+str(endpoint); mirror.prefix=prefix; mirror.client=client
        mirror.last_error=''
        if name != 'new':
            mirror.event = MethodType(baseline_event, mirror)
        if name == 'baseline_without_gc':
            raw_query=mirror.query
            def skip_gc(graph,query,_original=raw_query,**params):
                if 'WHERE NOT' in query:return []
                return _original(graph,query,**params)
            mirror.query=skip_gc
        projectors[name] = mirror
    # Both warm up the same target. All subsequent executions are idempotent
    # updates, and all source/action counts remain equal throughout comparison.
    for name in measurements:
        projectors[name].event(data, 'benchmark_target_event', [])
    for number in range(samples):
        order = tuple(measurements) if number % 2 == 0 else tuple(reversed(measurements))
        for name in order:
            mirror = projectors[name]; original = mirror.query; calls=[]
            def timed(graph, query, **params):
                if name == 'baseline_without_gc' and 'WHERE NOT' in query:return []
                before=time.perf_counter()
                value=original(graph, query, **params)
                calls.append(dict(elapsed_ms=(time.perf_counter()-before)*1000, cleanup='WHERE NOT' in query,
                                  empty_unwind='UNWIND' in query and any(value == [] for value in params.values()),
                                  shape=query[:180]))
                return value
            mirror.query=timed
            try:
                before=time.perf_counter(); mirror.event(data, 'benchmark_target_event', [])
                elapsed=(time.perf_counter()-before)*1000
            finally:
                mirror.query=original
            measurements[name].append(dict(elapsed_ms=elapsed, query_count=len(calls),
                cleanup_ms=sum(call['elapsed_ms'] for call in calls if call['cleanup']),
                empty_queries=sum(call['empty_unwind'] for call in calls), calls=calls))
    result={name:summary(values) for name, values in measurements.items()}
    result['ratio_baseline_to_new']=result['baseline']['median_ms']/result['new']['median_ms']
    result['ratio_baseline_to_without_gc']=result['baseline']['median_ms']/result['baseline_without_gc']['median_ms']
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-root', required=True, type=Path)
    parser.add_argument('--sizes', nargs='+', type=int, default=[1000,10000,100000])
    parser.add_argument('--samples', type=int, default=7)
    parser.add_argument('--output', required=True, type=Path)
    args=parser.parse_args()
    if args.samples < 5 or any(size < 1 or size > 200000 for size in args.sizes):
        raise SystemExit('Use at least five samples and fixture sizes 1..200000')
    output=args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    baseline, source=load_baseline(args.baseline_root.resolve())
    result=dict(fixture='synthetic reachable graph; controlled direct event() calls', baseline=source,
        limitations=['not production 27GB data', 'graph calls only; excludes SQL, leases, acknowledgement and scheduler waits',
                     'index build and fixture generation excluded from steady measurements', 'no independent RSS cap'], cases=[])
    with isolated_falkor(output) as (client, endpoint, config):
        result['module_list']=client.execute_command('MODULE', 'LIST')
        result['query_timeouts']=config
        for size in args.sizes:
            prefix='bench_'+uuid4().hex[:10]; graph=prefix+'_active_global'
            fixture_ms=seed(client, graph, size)
            query='MATCH (e:Event {id:"benchmark_target_event"}) RETURN e.id'
            before=client.execute_command('GRAPH.EXPLAIN', graph, query)
            unindexed=run_condition(client, endpoint, prefix, baseline, args.samples)
            index_ms=add_indexes(client, graph)
            after=client.execute_command('GRAPH.EXPLAIN', graph, query)
            indexed=run_condition(client, endpoint, prefix, baseline, args.samples)
            rows=command(client, graph, 'MATCH (e:Event)-[:HAS_SOURCE]->(a:Article) RETURN count(e),count(a)')[1]
            case=dict(events=size, fixture_ms=fixture_ms, index_build_ms=index_ms, plan_before=before, plan_after=after,
                      unindexed=unindexed, indexed=indexed, reachable_event_article_rows=rows,
                      info_memory=client.info('memory').get('used_memory'))
            assert rows == [[size+1,size+1]], rows
            result['cases'].append(case)
            (output/'projection-benchmark.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps(dict(events=size, no_index_old_ms=unindexed['baseline']['median_ms'],
                no_index_without_gc_ms=unindexed['baseline_without_gc']['median_ms'],
                no_index_new_ms=unindexed['new']['median_ms'], indexed_old_ms=indexed['baseline']['median_ms'],
                indexed_without_gc_ms=indexed['baseline_without_gc']['median_ms'],
                indexed_new_ms=indexed['new']['median_ms'], index_build_ms=index_ms)),flush=True)
            client.execute_command('GRAPH.DELETE',graph)
    print(str(output/'projection-benchmark.json'))


if __name__=='__main__':
    main()
