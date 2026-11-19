"""Reproducible, offline SQLite dashboard benchmark, independent of live data.

Examples (run with the project virtualenv's Python)::

  python tools/benchmark_read_queries.py generate --db /tmp/news-bench.db --rows 560000
  python tools/benchmark_read_queries.py compare --db /tmp/news-bench.db \
    --baseline-source /path/to/release023/source --output /tmp/comparison.json
  python tools/benchmark_read_queries.py run --db /tmp/news-bench.db --source . \
    --paths /radar?period=all /admin /api/system/status --output /tmp/current.json

Every route/source combination runs in a new process against its own temporary
SQLite backup. Startup and request time are reported separately; workers never
start and socket connections are blocked. Payloads are stored as plain JSON/text,
not compressed. This is synthetic workload evidence, not a measurement of a
production database, hardware, cache state, or 27 GB installation.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import hashlib
from html import unescape
import json
import math
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import traceback


DEFAULT_PATHS = ['/radar?period=all', '/admin', '/api/system/status']
DEFAULT_SOURCE = Path(__file__).resolve().parents[1]
WIDE_COLUMNS = ('article.content', 'article.raw_json', 'article.normalized_content',
    'articleversion.payload_json', 'event.summary', 'radarentry.material', 'radarentry.sources')


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + '\n')
    temporary.replace(path)


def block_network():
    def denied(*args, **kwargs):
        raise RuntimeError('network disabled by offline benchmark')
    socket.create_connection = denied
    socket.socket.connect = denied
    socket.socket.connect_ex = denied
    for name, value in {'NEWS2DOUYIN_GKG_STREAM_ENABLED': '0',
        'NEWS2DOUYIN_GKG_PROJECTION_ENABLED': '0', 'NEWS2DOUYIN_FALKOR_URL': ''}.items():
        os.environ[name] = value


def use_source(source):
    source = Path(source).resolve()
    if not (source / 'src/news2douyin/server/app.py').is_file():
        raise ValueError('source must contain src/news2douyin/server/app.py')
    sys.path.insert(0, str(source / 'src'))
    return source


def source_fingerprint(source):
    digest = hashlib.sha256()
    for path in sorted((Path(source) / 'src/news2douyin').rglob('*.py')):
        digest.update(str(path.relative_to(source)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def query_indexes(connection):
    result = {}
    for table in ('article', 'articleeventlink', 'radarentry', 'articleversion'):
        result[table] = [dict(name=row[1], columns=[field[2] for field in connection.execute(
            'PRAGMA index_info("' + row[1].replace('"', '""') + '")')])
            for row in connection.execute('PRAGMA index_list("' + table + '")')]
    return result


def generate_fixture(db, *, rows=560000, versions_per_article=1, payload_bytes=4096,
                     article_body_bytes=4096, batch_size=500, source=DEFAULT_SOURCE):
    """Build offline synthetic common-schema data without ORM per-row inflation."""
    if min(rows, versions_per_article, batch_size) < 1 or min(payload_bytes, article_body_bytes) < 0:
        raise ValueError('rows, versions_per_article, batch_size must be positive; byte sizes nonnegative')
    db = Path(db).resolve()
    if db.exists():
        raise FileExistsError('refusing to replace an existing fixture: ' + str(db))
    use_source(source)
    block_network()
    generator_fingerprint = source_fingerprint(Path(source).resolve())
    from news2douyin.storage.db import init_db, make_engine
    from news2douyin.storage.models import Article, ArticleVersion, ArticleEventLink, Event
    from news2douyin.desk.models import RadarEntry, GkgSource
    from news2douyin.extraction.models import TextMention, VersionReading
    from news2douyin.knowledge.models import GraphMetric
    db.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    engine = make_engine('sqlite:///' + str(db))
    init_db(engine)
    engine.dispose()
    articles = math.ceil(rows / versions_per_article)
    spec = dict(kind='synthetic-uncompressed-sqlite', rows=rows,
        versions_per_article=versions_per_article, articles=articles, events=articles,
        minimum_version_payload_bytes=payload_bytes, article_body_bytes=article_body_bytes,
        search_index='not populated; only unfiltered dashboard routes are benchmarked',
        background_workers=False, external_network=False,
        limitations='Not live data or the reported 27 GB production workload; OS cache is not reset.')
    fixture_id = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:20]
    stamp = '2026-11-04T10:00:00Z'
    filler = ('Evidence paragraph: independently attributed synthetic report. ' *
        (article_body_bytes // 60 + 2))[:article_body_bytes]
    providers = ('gkg', 'gdelt', 'worldnewsapi', 'industry.site', 'mock', '')
    buffers = {}
    def add(table, values):
        buffers.setdefault(table, []).append(values)
    with closing(sqlite3.connect(db)) as connection:
        connection.execute('PRAGMA journal_mode=DELETE')
        connection.execute('PRAGMA synchronous=OFF')
        # Bulk fixture generation bypasses queues and FTS maintenance. Page
        # startup reinstalls product triggers; no text-query benchmark is claimed.
        triggers = connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'").fetchall()
        for (name,) in triggers:
            connection.execute('DROP TRIGGER "' + name.replace('"', '""') + '"')
        def flush():
            for table, values in buffers.items():
                if not values:
                    continue
                columns = list(values[0])
                fields = ','.join('"' + value + '"' for value in columns)
                marks = ','.join('?' for _ in columns)
                connection.executemany(f'INSERT INTO "{table}" ({fields}) VALUES ({marks})',
                    ([row[column] for column in columns] for row in values))
            buffers.clear()
            connection.commit()
        version_id = 0
        for index in range(articles):
            key, event = f'bench_a_{index:09d}', f'bench_e_{index:09d}'
            title = f'Synthetic report {index}: diplomatic meeting and military developments'
            uri = f'https://synthetic.invalid/reports/{index}'
            provider = providers[index % len(providers)]
            article = Article(article_key=key, provider=provider, url=uri, canonical_url=uri,
                title=title, content=filler, normalized_content=filler.lower(),
                normalized_title=title.lower(), raw_json=json.dumps({'synthetic': True, 'sample': filler[:512]}),
                source_domain='synthetic.invalid', published_at=stamp, fetched_at=stamp,
                created_at=stamp, country='us', language='en').model_dump(exclude={'id'})
            add('article', article)
            add('event', Event(event_key=event, event_title=title, topic='global',
                summary=filler[:512], article_count=1, first_seen_at=stamp,
                last_seen_at=stamp, created_at=stamp).model_dump(exclude={'id'}))
            add('articleeventlink', ArticleEventLink(article_key=key, event_key=event).model_dump(exclude={'id'}))
            add('radarentry', RadarEntry(key=event, title=title, seen=stamp, priority=index % 4,
                band='其他进展', reason='Synthetic benchmark fixture', evidence='1 report',
                headings=title.lower(), material=filler[:512], geography='[]', sources='[]').model_dump())
            revisions = min(versions_per_article, rows - version_id)
            for revision in range(1, revisions + 1):
                version_id += 1
                payload = json.dumps(dict(provider=provider, url=uri, title=title, content=filler,
                    published_at=stamp, country='us', language='en', synthetic_revision=revision))
                # Spaces remain legal JSON whitespace and are stored uncompressed.
                payload += ' ' * max(0, payload_bytes - len(payload.encode()))
                add('articleversion', ArticleVersion(id=version_id, article_key=key, revision=revision,
                    content_hash=hashlib.sha256((key + ':' + str(revision)).encode()).hexdigest(),
                    payload_json=payload, origin='benchmark', observed_at=stamp).model_dump())
                add('versionreading', VersionReading(version_id=version_id,
                    state=('ready', 'baseline', 'model_queued', 'error')[index % 4]).model_dump())
            if index % 3 == 0:
                add('gkgsource', GkgSource(article_key=key, record_id='20261104100000-' + str(index),
                    attributes=json.dumps({'persons': ['Synthetic Person'], 'themes': ['DIPLOMACY']})).model_dump())
            if index % 5 == 0:
                add('textmention', TextMention(id='bench_m_' + str(index), version_id=version_id,
                    article=key, revision=revisions, signature='synthetic',
                    data=json.dumps({'kind': 'meeting' if index % 2 else 'attack',
                        'status': 'reported', 'arguments': [], 'synthetic': True})).model_dump())
            if index % 10 == 0:
                add('graphmetric', GraphMetric(event=event, generation='benchmark', score=index % 100,
                    community='synthetic', data='{}').model_dump())
            if (index + 1) % batch_size == 0:
                flush()
        flush()
        connection.execute('ANALYZE')
        connection.commit()
        counts = {table: connection.execute('SELECT count(*) FROM "' + table + '"').fetchone()[0]
            for table in ('article', 'articleversion', 'event', 'articleeventlink', 'radarentry',
                'versionreading', 'textmention', 'gkgsource', 'graphmetric')}
        indexes = query_indexes(connection)
    result = dict(spec, fixture_id=fixture_id, db=str(db), counts=counts,
        generator_source_fingerprint=generator_fingerprint, query_indexes=indexes,
        articleversion_indexes=indexes['articleversion'],
        bytes=db.stat().st_size, generation_ms=(time.perf_counter() - started) * 1000)
    write_json(str(db) + '.fixture.json', result)
    return result


def sql_flags(statement):
    normalized = re.sub(r'\s+', ' ', statement.replace('"', '')).strip().lower()
    projection = normalized.split(' from ', 1)[0]
    wide = [column for column in WIDE_COLUMNS if column in projection]
    return dict(wide_columns=wide,
        unbounded_entity_load=bool(wide and ' where ' not in normalized and ' limit ' not in normalized),
        # A facet query can mention a correlated version lookup while grouping
        # event memberships. Only a GROUP BY on the version log is a hit.
        version_group_by=bool(re.search(r'\bgroup by\s+articleversion\.', normalized)),
        count_query=bool(re.search(r'\bcount\s*\(', normalized)))


def rss_mb():
    try:
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return value / (1024 * 1024 if sys.platform == 'darwin' else 1024)
    except (ImportError, AttributeError):
        return None


def worker(job):
    """Internal process entrypoint: no product module is imported in parent."""
    use_source(job['source'])
    block_network()
    if job.get('memory_limit_mb'):
        import resource
        size = job['memory_limit_mb'] * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (size, size))
    from sqlalchemy import event
    from fastapi.testclient import TestClient
    from news2douyin.server import webui
    from news2douyin.server.app import create_app
    from news2douyin.storage.sql import install_sql_functions
    webui._llm_alive = lambda: False
    result = dict(source=str(Path(job['source']).resolve()), path=job['path'], runs=[],
        worker_pid=os.getpid(), memory_limit_mb=job.get('memory_limit_mb', 0),
        source_fingerprint=source_fingerprint(Path(job['source']).resolve()),
        warnings=['Driver execution time excludes result fetching and ORM materialization.',
            'Process RSS includes application startup; no OS cache flush is performed.'])
    write_json(job['output'], result)
    app = None
    try:
        started = time.perf_counter()
        app = create_app(db_url='sqlite:///' + job['db'], storage_root=job['storage'])
        result['startup_ms'] = (time.perf_counter() - started) * 1000
        engine = app.state.engine
        current = []
        def before(conn, cursor, statement, parameters, context, executemany):
            context._benchmark_started = time.perf_counter()
        def after(conn, cursor, statement, parameters, context, executemany):
            current.append(dict(sql=statement, parameters=parameters,
                execute_ms=(time.perf_counter() - context._benchmark_started) * 1000,
                **sql_flags(statement)))
        event.listen(engine, 'before_cursor_execute', before)
        event.listen(engine, 'after_cursor_execute', after)
        client = TestClient(app)  # Deliberately do not run lifespan/background workers.
        try:
            for repetition in range(job['repeats']):
                current.clear()
                began = time.perf_counter()
                response = client.get(job['path'])
                elapsed = (time.perf_counter() - began) * 1000
                statements = list(current)
                groups = Counter(re.sub(r'\s+', ' ', s['sql']).strip() for s in statements)
                record = dict(repetition=repetition, status_code=response.status_code,
                    response_headers={name: response.headers[name] for name in ('content-type', 'cache-control') if name in response.headers},
                    wall_ms=elapsed, sql_count=len(statements),
                    sql_execute_ms=sum(s['execute_ms'] for s in statements), peak_rss_mb=rss_mb(),
                    response_bytes=len(response.content),
                    count_queries=sum(s['count_query'] for s in statements),
                    version_group_by_queries=sum(s['version_group_by'] for s in statements),
                    wide_column_queries=sum(bool(s['wide_columns']) for s in statements),
                    unbounded_entity_loads=sum(s['unbounded_entity_load'] for s in statements),
                    repeated_sql=[dict(sql=k, occurrences=v) for k, v in groups.items() if v > 1],
                    statements=statements)
                if job['path'].split('?', 1)[0] in {'/api/system/status', '/api/radar/facets'} and response.status_code == 200:
                    record['response_json'] = response.json()
                if job['path'].split('?', 1)[0] == '/radar' and response.status_code == 200:
                    html = response.text
                    state = re.search(r'data-facet-state=["\']([^"\']*)', html)
                    url = re.search(r'data-radar-facet-url=["\']([^"\']*)', html)
                    stats = {name: unescape(value).strip() for name, value in re.findall(
                        r'<strong\b[^>]*data-radar-stat=["\']([^"\']+)["\'][^>]*>(.*?)</strong>', html,
                        flags=re.DOTALL)}
                    record['response_html_evidence'] = dict(facet_state=state.group(1) if state else None,
                        facet_url=unescape(url.group(1)) if url else None, statistics=stats)
                result['runs'].append(record)
                write_json(job['output'], result)
                if response.status_code != 200:
                    result['error'] = 'HTTP ' + str(response.status_code)
                    break
        finally:
            client.close()
            event.remove(engine, 'before_cursor_execute', before)
            event.remove(engine, 'after_cursor_execute', after)
        if job.get('explain', True) and result['runs']:
            # Explain once after measurement, including registered SQLite JSON/date
            # functions. It is not included in request SQL counts or timing.
            plans = []
            with engine.connect() as connection:
                install_sql_functions(connection)
                seen = set()
                for statement in result['runs'][0]['statements']:
                    sql = statement['sql']
                    if sql in seen or not sql.lstrip().upper().startswith(('SELECT', 'WITH')):
                        continue
                    seen.add(sql)
                    try:
                        plan = connection.exec_driver_sql('EXPLAIN QUERY PLAN ' + sql,
                            tuple(statement['parameters'])).all()
                        plans.append(dict(sql=sql, plan=[list(row) for row in plan]))
                    except Exception as exc:
                        plans.append(dict(sql=sql, error=type(exc).__name__ + ': ' + str(exc)))
            result['explain'] = plans
        if result['runs']:
            result['median'] = {name: statistics.median(row[name] for row in result['runs'])
                for name in ('wall_ms', 'sql_count', 'sql_execute_ms', 'count_queries',
                    'version_group_by_queries', 'wide_column_queries', 'unbounded_entity_loads')}
    except BaseException as exc:
        result['error'] = type(exc).__name__ + ': ' + str(exc)
        result['traceback'] = traceback.format_exc(limit=10)
        result['peak_rss_mb'] = rss_mb()
    finally:
        if app is not None:
            app.state.engine.dispose()
        write_json(job['output'], result)
    return 1 if result.get('error') else 0


def benchmark(db, variants, *, paths=DEFAULT_PATHS, repeats=3, timeout=180,
              memory_limit_mb=0, explain=True, workdir=None, workload_note=''):
    db = Path(db).resolve()
    if not db.is_file():
        raise FileNotFoundError(db)
    if repeats < 1:
        raise ValueError('repeats must be positive')
    original = (db.stat().st_size, db.stat().st_mtime_ns)
    metadata = Path(str(db) + '.fixture.json')
    report = dict(kind='synthetic-read-query-benchmark', fixture=str(db), fixture_bytes=original[0],
        fixture_metadata=json.loads(metadata.read_text()) if metadata.is_file() else None,
        assumptions='Fresh process and independent SQLite backup per source/route. Warm OS cache, no workers/network.',
        results=[])
    if workload_note:
        report['workload_note'] = workload_note
    with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as inspect:
        report['fixture_query_indexes'] = query_indexes(inspect)
        report['fixture_articleversion_indexes'] = report['fixture_query_indexes']['articleversion']
    # /tmp is often a small RAM filesystem; default beside the fixture, so a
    # multi-GB independent copy stays on the dataset's disk.
    copy_parent = Path(workdir).resolve() if workdir else db.parent
    if shutil.disk_usage(copy_parent).free < original[0] * 1.1:
        raise RuntimeError('not enough disk space for an independent fixture copy in ' + str(copy_parent))
    with tempfile.TemporaryDirectory(prefix='n2d-read-bench-', dir=copy_parent) as temporary:
        folder = Path(temporary)
        for label, source in variants:
            for index, path in enumerate(paths):
                print(f'Benchmarking {label} {path} with an independent database copy', file=sys.stderr, flush=True)
                clone = folder / 'copy.db'
                began = time.perf_counter()
                # SQLite backup gives a consistent snapshot, including WAL if present.
                # Source is opened read-only and is never passed to the application.
                with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as source_db:
                    with closing(sqlite3.connect(clone)) as destination:
                        source_db.backup(destination)
                copied_ms = (time.perf_counter() - began) * 1000
                output = folder / f'{label}-{index}.json'
                job = dict(source=str(Path(source).resolve()), path=path, db=str(clone),
                    storage=str(folder / 'runs'), output=str(output), repeats=repeats,
                    memory_limit_mb=memory_limit_mb, explain=explain)
                job_path = folder / 'job.json'
                write_json(job_path, job)
                try:
                    completed = subprocess.run([sys.executable, str(Path(__file__).resolve()),
                        '_worker', '--job', str(job_path)], capture_output=True, text=True, timeout=timeout)
                    result = json.loads(output.read_text()) if output.is_file() else dict(
                        source=job['source'], path=path, error='worker exited before writing a result')
                    result.update(worker_returncode=completed.returncode,
                        worker_stderr=completed.stderr[-5000:])
                    if completed.returncode and not result.get('error'):
                        result['error'] = 'worker exited with status ' + str(completed.returncode)
                except subprocess.TimeoutExpired as exc:
                    result = json.loads(output.read_text()) if output.is_file() else dict(source=job['source'], path=path)
                    result['error'] = f'worker exceeded {timeout}s (startup plus measured requests and EXPLAIN)'
                    result['worker_stderr'] = str(exc.stderr or '')[-5000:]
                result.update(label=label, copied_ms=copied_ms)
                report['results'].append(result)
                print(f'Finished {label} {path}: ' + (result.get('error') or 'ok'), file=sys.stderr, flush=True)
                for suffix in ('', '-wal', '-shm'):
                    Path(str(clone) + suffix).unlink(missing_ok=True)
    report['fixture_unchanged'] = (db.stat().st_size, db.stat().st_mtime_ns) == original
    if len(variants) == 2:
        report['comparisons'] = []
        for path in paths:
            pair = [row for row in report['results'] if row['path'] == path]
            old, new = pair
            item = dict(path=path, baseline=old['label'], current=new['label'])
            if old.get('median') and new.get('median'):
                ratio_key = 'request_latency_ratio' if workload_note else 'wall_speedup'
                item[ratio_key] = old['median']['wall_ms'] / max(new['median']['wall_ms'], .001)
                if workload_note:
                    item['workload_note'] = workload_note
                item['sql_count_change'] = [old['median']['sql_count'], new['median']['sql_count']]
                item['version_group_by_change'] = [old['median']['version_group_by_queries'], new['median']['version_group_by_queries']]
                item['unbounded_entity_load_change'] = [old['median']['unbounded_entity_loads'], new['median']['unbounded_entity_loads']]
            report['comparisons'].append(item)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    generate = commands.add_parser('generate', help='Create a new uncompressed synthetic fixture')
    generate.add_argument('--db', type=Path, required=True)
    generate.add_argument('--rows', type=int, default=560000, help='Exact articleversion count, not article count')
    generate.add_argument('--versions-per-article', type=int, default=1)
    generate.add_argument('--payload-bytes', type=int, default=4096)
    generate.add_argument('--article-body-bytes', type=int, default=4096)
    generate.add_argument('--batch-size', type=int, default=500)
    generate.add_argument('--source', type=Path, default=DEFAULT_SOURCE)
    for name in ('run', 'compare'):
        command = commands.add_parser(name)
        command.add_argument('--db', type=Path, required=True)
        command.add_argument('--source', type=Path, default=DEFAULT_SOURCE,
            help='Current implementation source root')
        command.add_argument('--label', default='current',
            help='Label for --source, useful when collecting baseline and current stages separately')
        if name == 'compare':
            command.add_argument('--baseline-source', type=Path, required=True)
        command.add_argument('--paths', nargs='+', default=DEFAULT_PATHS)
        command.add_argument('--repeats', type=int, default=3)
        command.add_argument('--timeout', type=float, default=180)
        command.add_argument('--memory-limit-mb', type=int, default=0,
            help='Optional worker virtual-address-space limit; 0 means no explicit limit')
        command.add_argument('--no-explain', action='store_true')
        command.add_argument('--workdir', type=Path)
        command.add_argument('--output', type=Path, required=True)
        command.add_argument('--workload-note', default='',
            help='Explain changed response scope; compare labels ratios as request latency, not equivalent-work speedup')
    internal = commands.add_parser('_worker', help=argparse.SUPPRESS)
    internal.add_argument('--job', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == '_worker':
        return worker(json.loads(args.job.read_text()))
    if args.command == 'generate':
        result = generate_fixture(args.db, rows=args.rows, versions_per_article=args.versions_per_article,
            payload_bytes=args.payload_bytes, article_body_bytes=args.article_body_bytes,
            batch_size=args.batch_size, source=args.source)
        print(json.dumps(result, indent=2))
        return 0
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', args.label):
        parser.error('--label must contain 1–40 letters, digits, underscores or hyphens')
    variants = [(args.label, args.source)]
    if args.command == 'compare':
        variants.insert(0, ('baseline', args.baseline_source))
    result = benchmark(args.db, variants, paths=args.paths, repeats=args.repeats,
        timeout=args.timeout, memory_limit_mb=args.memory_limit_mb, explain=not args.no_explain,
        workdir=args.workdir, workload_note=args.workload_note)
    write_json(args.output, result)
    print(json.dumps(dict(output=str(args.output), fixture_unchanged=result['fixture_unchanged'],
        summaries=[dict(label=row['label'], path=row['path'], median=row.get('median'),
            error=row.get('error')) for row in result['results']], comparisons=result.get('comparisons')), indent=2))
    return 1 if any(row.get('error') for row in result['results']) else 0


if __name__ == '__main__':
    raise SystemExit(main())
