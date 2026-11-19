"""Time the v0.32 batched source-evidence queries in a disposable PostgreSQL DB.

Usage: python tools/benchmark_official_coverage.py --rows 500000 --events 5000
Requires the test pgserver distribution. Does not take a production database URL.
"""
import argparse
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from time import perf_counter

from sqlalchemy import event, text
from sqlmodel import Session

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from benchmark_topic_projection import isolated_postgres
from news2douyin.intake.models import IntakeBatch
from news2douyin.intake.official_models import OfficialMention
from news2douyin.selection.official_evidence import coverage_by_event
from news2douyin.storage.db import init_db, make_engine

START = '2026-05-02T00:00:00Z'
END = '2026-05-03T00:00:00Z'
OLD = '2026-05-01T10:00:00Z'
NEW = '2026-05-02T10:00:00Z'


def seed(engine, rows, events):
    feed = 'mentions_english'
    with Session(engine) as session:
        for at, stamp in [(OLD, '20260501100000'), (NEW, '20260502100000')]:
            session.add(IntakeBatch(key=f'{feed}_{stamp}', feed=feed, stamp=stamp,
                                    completed_at=at, received_at=at, state='archived'))
        session.commit()
    example = OfficialMention(key='', event_id='', doc_identifier='', first_feed=feed,
        first_stamp='', first_row_index=1, last_feed=feed, last_stamp='', last_row_index=1)
    defaults = example.model_dump()
    columns = {name: ':' + name for name in defaults}
    # Two generations of source documents for each official ID, 8 domains.
    columns.update(key="md5('row-' || n::text)", event_id='((n % :events) + 1)::text',
        doc_identifier="('https://d' || ((n / :events) % 8)::text || '.example/' || n::text)",
        domain="('d' || ((n / :events) % 8)::text || '.example')",
        first_row_index='n+1', last_row_index='n+1')
    stamp = "CASE WHEN n/:events % 2=0 THEN '20260501100000' ELSE '20260502100000' END"
    at = "CASE WHEN n/:events % 2=0 THEN :old ELSE :new END"
    columns.update(first_seen=at, last_seen=at, updated_at=at, first_stamp=stamp, last_stamp=stamp)
    statement = ('INSERT INTO officialmention (' + ','.join(columns) + ') SELECT ' +
                 ','.join(columns.values()) + ' FROM generate_series(0,:nrows-1) AS sample(n)')
    with engine.begin() as connection:
        connection.execute(text(statement), dict(defaults, nrows=rows, events=events, old=OLD, new=NEW))
        connection.execute(text('ANALYZE officialmention'))
        connection.execute(text('ANALYZE intakebatch'))


def run(args):
    with TemporaryDirectory() as folder, isolated_postgres(Path(folder), args.locale) as (urls, db):
        engine = make_engine(urls['chunked'])
        try:
            init_db(engine)
            seed(engine, args.rows, args.events)
            statements = 0
            def count(*_):
                nonlocal statements
                statements += 1
            event.listen(engine, 'before_cursor_execute', count)
            with Session(engine) as session:
                started = perf_counter()
                coverage = coverage_by_event(session, [str(k + 1) for k in range(args.events)], START, END)
                seconds = perf_counter() - started
            docs = sum(row['documents'] for row in coverage.values())
            assert docs == args.rows, (docs, args.rows)
            assert sum(row['new_documents'] for row in coverage.values()) == sum(
                min(args.events, args.rows - offset) for offset in range(args.events, args.rows, args.events*2))
            print(json.dumps(dict(database=db['version'], input_documents=docs, events=args.events,
                timed_operation='bulk official evidence aggregation', queries=statements,
                seconds=round(seconds, 3), synthetic=True)))
        finally:
            engine.dispose()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows', type=int, default=500000)
    parser.add_argument('--events', type=int, default=5000)
    parser.add_argument('--locale', default='C')
    args = parser.parse_args()
    if not 1 <= args.events <= args.rows:
        parser.error('choose 1 <= events <= rows')
    run(args)
