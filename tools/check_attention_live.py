"""Verify attention UI against a disposable real app and synthetic NewsNow HTTP.

No private service, real news endpoint, or model is contacted. All fixtures are
labelled synthetic. Screenshots are scratch-only QA output, never release data.
Use the PG launcher and --database postgres for a real disposable PostgreSQL DB.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src')); sys.path.insert(0, str(ROOT/'tests'))
from loguru import logger
logger.remove()
import requests
import uvicorn
from fastapi import Response
from fastapi.testclient import TestClient
from sqlalchemy import event as sql_event, func
from sqlalchemy.engine import make_url
from sqlmodel import Session, select
from attention_test_support import attention_http, profile, collect_observed_round, TARIFF
from news2douyin.attention.models import AttentionInbox, AttentionMeta, AttentionTopic, AttentionItem
from news2douyin.server.app import create_app
from news2douyin.intake.official_models import OfficialEvent, OfficialMention
from news2douyin.storage.models import Article, Event
from sql_readonly_audit import is_read_only_execution


@contextmanager
def disposable_url(mode, folder):
    if mode == 'sqlite':
        yield 'sqlite:///' + str(folder/'app.db')
        return
    import psycopg
    from psycopg import sql
    configured = os.environ.get('NEWS2DOUYIN_TEST_POSTGRES_URL', '')
    if not configured:
        raise RuntimeError('Use tools/run_postgres_test_server.py for --database postgres')
    url = make_url(configured)
    if url.host != '127.0.0.1':
        raise RuntimeError('Browser QA accepts only a disposable loopback PostgreSQL server')
    native = url.set(drivername='postgresql', database='postgres').render_as_string(hide_password=False)
    name = 'n2d_attention_browser_' + uuid4().hex
    with psycopg.connect(native, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        yield url.set(drivername='postgresql+psycopg', database=name).render_as_string(hide_password=False)
    finally:
        with psycopg.connect(native, autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


def drain(app):
    for _ in range(60):
        if not app.state.attention.step():
            return
    raise AssertionError('Synthetic attention queue did not drain')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path)
    parser.add_argument('--database', choices=['sqlite', 'postgres'], default='sqlite')
    args = parser.parse_args(); output = args.output.resolve(); output.mkdir(parents=True, exist_ok=True)
    for flag in ('NEWS2DOUYIN_GKG_STREAM_ENABLED', 'NEWS2DOUYIN_GKG_PROJECTION_ENABLED',
                 'NEWS2DOUYIN_GDELT_STREAM_ENABLED', 'NEWS2DOUYIN_GDELT_PROJECTION_ENABLED'):
        os.environ[flag] = '0'
    os.environ['NEWS2DOUYIN_FALKOR_URL'] = ''
    with tempfile.TemporaryDirectory(prefix='attention-browser-') as temporary, attention_http() as upstream:
        folder = Path(temporary)
        with disposable_url(args.database, folder) as url:
            app = create_app(db_url=url, storage_root=str(folder/'runs'))
            request_path = ContextVar('attention_qa_path', default=('', ''))
            @app.middleware('http')
            async def capture_request(request, call_next):
                token = request_path.set((request.method, request.url.path))
                try: return await call_next(request)
                finally: request_path.reset(token)
            client = TestClient(app)
            name = profile(client, upstream)
            collection_rounds = []
            for index in range(3):
                upstream.set_round(index)
                data = collect_observed_round(app, client, name, upstream); drain(app)
                collection_rounds.append(dict(round=index, task=data['task']['status']))
            topic = next(row for row in client.get('/api/attention/topics?period=all').json()['items'] if row['title'] == TARIFF)
            now = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
            stamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
            with Session(app.state.engine) as session:
                for index, source in enumerate(('zhihu', 'weibo'), 1):
                    ident = 'qa-official-' + str(index)
                    uri = f'https://{source}.example/news/tariff'
                    session.add(OfficialEvent(event_id=ident, event_date=now[:10], first_seen=now,
                        last_seen=now, actor1_name='DONALD TRUMP', actor2_name='CHINA',
                        event_code='172', event_base_code='172', event_root_code='17',
                        action_country_iso2='us', source_url=uri, feed='events_english', stamp=stamp, row_index=index))
                    session.add(OfficialMention(key='qa-mention-' + str(index), event_id=ident,
                        doc_identifier=uri, domain=source+'.example', first_seen=now, last_seen=now,
                        first_feed='mentions_english', first_stamp=stamp, first_row_index=index,
                        last_feed='mentions_english', last_stamp=stamp, last_row_index=index))
                session.commit()
            from news2douyin.attention import matching
            matching.refresh_topic(app.state.engine, topic['key'])
            reads = []; prohibited = []
            def audit(_connection, _cursor, statement, _parameters, context, _executemany):
                method, path = request_path.get()
                if method == 'GET' and (path == '/radar' or path.startswith('/api/attention')):
                    reads.append(dict(sql=statement, read_only=is_read_only_execution(statement, context)))
            def forbidden(*_args, **_kwargs):
                prohibited.append('graph_or_lake')
                raise AssertionError('Attention GET opened a lake or graph service')
            app.state.desk.graph.query = app.state.desk.graph.read = app.state.desk.graph.explain = forbidden
            # The fixture is fully indexed before the browser starts. Any lake open
            # after this point is therefore a regression in an interactive route.
            from news2douyin.attention.lake import AttentionLake
            AttentionLake.__init__ = forbidden
            @app.get('/favicon.ico', include_in_schema=False)
            def favicon(): return Response(status_code=204)
            @app.get('/qa/config', include_in_schema=False)
            def config(): return dict(topic_key=topic['key'], topic_title=TARIFF, synthetic=True)
            @app.get('/qa/state', include_in_schema=False)
            def state():
                with Session(app.state.engine) as session:
                    return dict(counts={model.__name__: session.exec(select(func.count()).select_from(model)).one()
                        for model in (Article, Event, AttentionInbox, AttentionTopic, AttentionItem)},
                        prohibited_calls=len(prohibited), upstream_calls=len(upstream.calls))
            @app.post('/qa/backfill-failure', include_in_schema=False)
            def backfill_failure():
                with Session(app.state.engine) as session:
                    session.merge(AttentionMeta(key='backfill_error', value=json.dumps([
                        dict(run_id=28, error='QA missing historical snapshot, synthetic fixture')], ensure_ascii=False)))
                    session.commit()
                return dict(ok=True)
            with socket.socket() as probe: probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
            base = 'http://127.0.0.1:' + str(port)
            server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, lifespan='off', log_level='warning'))
            thread = threading.Thread(target=server.run, daemon=True); thread.start()
            try:
                for _ in range(100):
                    try:
                        if requests.get(base+'/api/health', timeout=.2).ok: break
                    except requests.RequestException: time.sleep(.05)
                else: raise RuntimeError('Local QA app did not start')
                sql_event.listen(app.state.engine, 'before_cursor_execute', audit)
                env = dict(os.environ, N2D_QA_URL=base, N2D_QA_OUTPUT=str(output))
                result = subprocess.run(['node', str(ROOT/'tools/check_attention_browser.cjs')], env=env, timeout=150)
                if result.returncode: raise SystemExit(result.returncode)
                report = json.loads((output/'attention-browser.json').read_text())
                report.update(version='0.30.0', recorded_at=now, real_http=True, database=args.database,
                    postgres_locale=os.getenv('NEWS2DOUYIN_TEST_POSTGRES_LOCALE', ''),
                    collection_rounds=collection_rounds, fixture='synthetic NewsNow HTTP snapshots, not measured real popularity',
                    production_accessed=False, remote_model_contacted=False, graph_or_lake_interactive_calls=len(prohibited),
                    observation_clock='Synthetic 20-minute intervals; HTTP and the durable TaskWorker are real.',
                    timing_method='Navigation until visible topics and completed status fetch; not paint timing or production performance.')
                assert not prohibited
                assert reads and all(row['read_only'] for row in reads)
                assert not any('count(' in row['sql'].lower().replace(' ', '') for row in reads)
                report['attention_get_sql_audit'] = dict(executions=len(reads), writes=0, count_queries=0)
                (output/'attention-browser.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
                print(json.dumps(report, ensure_ascii=False))
            finally:
                sql_event.remove(app.state.engine, 'before_cursor_execute', audit)
                server.should_exit=True; thread.join(timeout=10); client.close(); app.state.engine.dispose()


if __name__ == '__main__': main()
