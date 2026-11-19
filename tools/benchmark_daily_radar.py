"""Measure candidate-to-edition ranking on a deterministic synthetic stream.

Examples:
    python tools/benchmark_daily_radar.py --rows 500000
    python tools/benchmark_daily_radar.py --stage archive --rows 500000 --watches 100

The rank stage times synthetic generation and ranking. The archive stage owns
a temporary GKG lake and loopback PostgreSQL cluster (requires pgserver and
psycopg). It times scanning narrow GKG rows, selection and publication, followed
by a separate frozen snapshot read. Seed creation, network, raw payload decoding,
schema bootstrap and HTTP are outside that measurement. RSS is the Python
process high-water mark and excludes the PostgreSQL server.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from news2douyin.digest.ranking import Candidate, rank_day

# Repeats simulate many outlets covering a few stories as well as a long tail.
KINDS = ('gdelt', 'local', 'newsnow')
PLACES = ('cn', 'br', 'pl', 'za', 'gh', 'cl', 'pt', 'id')
ACTIONS = (
    ('logistics', 'approved a freight timetable'),
    ('economy', 'published a trade agreement'),
    ('transport', 'opened a replacement bridge'),
    ('health', 'adopted a clinic budget'),
    ('industry', 'commissioned a materials plant'),
    ('law', 'enacted a reporting rule'),
)


def fixtures(rows, distribution):
    for index in range(rows):
        if distribution == 'unique' or index % 10 == 9:
            story = 20000 + index
        elif index % 10 < 6:
            story = (index // 10) % 400
        else:
            story = 400 + (index // 10) % 12000
        digest = hashlib.blake2s(str(story).encode(), digest_size=6).hexdigest()
        country = PLACES[story % len(PLACES)]
        channel, action = ACTIONS[story % len(ACTIONS)]
        outlet = (index * 13 + index // 23) % 89
        domain = f'paper-{outlet}.example'
        kind = KINDS[index % len(KINDS)]
        title = f'District {digest} {action} for project {story}'
        yield Candidate(
            key=f'{kind}:{index}', title=title, observed_at='2026-11-14T04:00:00Z',
            source_url=f'https://{domain}/stories/{digest}', source_kind=kind,
            country=country, channel=channel, actors=(f'District {digest}',),
            domains=(domain,), watches=('freight',) if story % 23 == 0 else (),
            attention=(story % 93) / 92 if kind == 'newsnow' else None,
            impact=(story % 11)/10, evidence_key=f'report-{digest}',
            excerpt=title + '. This is a synthetic benchmark fixture.',
        )


def peak_rss():
    peak_rss_mib = None
    try:
        import resource
    except ImportError:
        pass
    else:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        peak_rss_mib = round(peak / (1024**2 if sys.platform == 'darwin' else 1024), 1)
    return peak_rss_mib


def seed_archive(root, day, rows, batches, distribution):
    from news2douyin.intake.lake import Lake

    with Lake(root).connection(create=True) as db:
        # Exercise the exact immutable summary columns scanned by the adapter.
        # Empty/invalid rows simulate two different reasons a lead cannot enter.
        db.execute("""
            INSERT INTO intake_records_v2
                (feed, stamp, row_index, record_id, document_date, url, title,
                 valid, error, observed_at)
            WITH assigned AS (
                SELECT i, i * $batches // $rows AS batch,
                       CASE WHEN $distribution='unique' OR i%10=9 THEN 20000+i
                            WHEN i%10<6 THEN (i//10)%400
                            ELSE 400+((i//10)%12000) END AS story
                FROM range($rows) sample(i)
            ), numbered AS (
                SELECT *, CASE WHEN batch%2=0 THEN 'gkg_english' ELSE 'gkg_translation' END AS feed,
                       strftime($midnight::TIMESTAMP + (batch//2)*INTERVAL '15 MINUTE',
                                '%Y%m%d%H%M%S') AS stamp,
                       row_number() OVER (PARTITION BY batch ORDER BY i) AS row_index,
                       md5(story::VARCHAR) AS ident
                FROM assigned
            )
            SELECT feed, stamp, row_index, 'doc-'||i::VARCHAR, stamp,
                   'https://paper-'||((13*i+i//23)%89)::VARCHAR||'.example/r/'||ident,
                   CASE WHEN i%100=1 THEN '' ELSE
                       'District '||ident[:12]||' published a decision for project '||story::VARCHAR END,
                   i%100<>2, CASE WHEN i%100=2 THEN 'synthetic_invalid' ELSE '' END, $observed
            FROM numbered ORDER BY feed,stamp,row_index
        """, dict(rows=rows, batches=batches, distribution=distribution,
                  midnight=day.isoformat()+' 00:00:00', observed=day.isoformat()+'T04:00:00Z'))
        db.execute("""INSERT INTO intake_archives (feed,stamp,rows,valid_rows,invalid_rows,completed_at)
            SELECT feed,stamp,count(*),count(*) FILTER(WHERE valid),count(*) FILTER(WHERE NOT valid),?
            FROM intake_records_v2 GROUP BY feed,stamp""", [day.isoformat()+'T04:00:00Z'])


def archive_benchmark(args):
    import multiprocessing
    from sqlalchemy import event as sql_event
    from sqlmodel import Session
    from benchmark_topic_projection import isolated_postgres
    from news2douyin.desk.models import Watch
    from news2douyin.digest.service import build_issue, last_closed_day, read_issue
    from news2douyin.storage.db import init_db, make_engine

    day = last_closed_day()
    batches = min(args.rows, 192)
    with TemporaryDirectory(prefix='n2d-daily-benchmark-') as folder:
        root = Path(folder)
        # Isolate synthetic fixture creation from the measured RSS high water.
        seeder = multiprocessing.get_context('spawn').Process(
            target=seed_archive, args=(root, day, args.rows, batches, args.distribution))
        seeder.start()
        seeder.join()
        if seeder.exitcode:
            raise RuntimeError('Synthetic GKG fixture creation failed')
        with isolated_postgres(root, args.locale) as (urls, database):
            engine = make_engine(urls['chunked'])
            try:
                init_db(engine)
                with Session(engine) as session:
                    for n in range(args.watches):
                        # Five scopes occur in the skewed fixture; the rest are unrelated.
                        if n < 5:
                            identifier = hashlib.md5(str(n).encode(), usedforsecurity=False).hexdigest()[:12]
                            scope = f'District {identifier}'
                        else:
                            scope = f'Regional trade group {n}'
                        session.add(Watch(key=f'qa-{n}', name=f'QA Watch {n}', kind='长期事件',
                                          scope=scope, enabled=True, created=day.isoformat()+'T00:00:00Z'))
                    session.commit()
                query_count = 0

                def count(*_):
                    nonlocal query_count
                    query_count += 1

                sql_event.listen(engine, 'before_cursor_execute', count)
                start = time.perf_counter()
                issued = build_issue(engine, day, storage_root=root)
                elapsed = time.perf_counter() - start
                if issued['status'] != 'ready' or len(issued['items']) > 100:
                    raise RuntimeError(f"Invalid edition state: {issued['status']} / {issued.get('error')}")
                stats = issued['stats']
                if stats['sources']['gkg_seen'] != args.rows:
                    raise AssertionError('Archive scan missed rows')
                before = query_count
                with Session(engine) as session:
                    start = time.perf_counter()
                    saved = read_issue(session, day)
                    frozen_ms = (time.perf_counter() - start) * 1000
                if saved != issued:
                    raise AssertionError('Snapshot differed from published edition')
                print(json.dumps(dict(synthetic=True, stage='archived_summaries_to_frozen_edition',
                    rows=args.rows, distribution=args.distribution, enabled_watches=args.watches,
                    selected_hints=sum(bool(item.get('focus_hints')) for item in issued['items']),
                    database=database['version'], archives=batches, seconds=round(elapsed, 3),
                    peak_process_rss_mib=peak_rss(), selected=len(issued['items']), stats=stats,
                    pg_build_statements=before, pg_read_statements=query_count-before,
                    pg_read_ms=round(frozen_ms, 3)), ensure_ascii=False))
            finally:
                engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=('rank', 'archive'), default='rank')
    parser.add_argument('--watches', type=int, default=100, help='Enabled watches in the isolated archive fixture')
    parser.add_argument('--locale', default='C', help='Temporary PostgreSQL cluster locale')
    parser.add_argument('--rows', type=int, default=500000)
    parser.add_argument('--distribution', choices=('skewed', 'unique'), default='skewed')
    parser.add_argument('--mode', choices=('balanced', 'editorial', 'breadth'), default='balanced')
    args = parser.parse_args()
    if args.rows < 1:
        parser.error('--rows must be positive')
    if args.watches < 0:
        parser.error('--watches must be nonnegative')
    if args.stage == 'archive':
        if args.mode != 'balanced':
            parser.error('Archive editions use the balanced mode')
        archive_benchmark(args)
        return
    started = time.perf_counter()
    result = rank_day(fixtures(args.rows, args.distribution), mode=args.mode)
    seconds = time.perf_counter() - started
    items = result['items']
    if len(items) > 100 or len({item['key'] for item in items}) != len(items):
        raise AssertionError('Edition exceeded its unique reading budget')
    print(json.dumps(dict(synthetic=True, stage='candidate_generation_and_rank_day',
        rows=args.rows, distribution=args.distribution, mode=args.mode,
        selected=len(items), seconds=round(seconds, 3), rows_per_second=round(args.rows / seconds),
        peak_process_rss_mib=peak_rss(), stats=result['stats']), ensure_ascii=False))


if __name__ == '__main__':
    main()
