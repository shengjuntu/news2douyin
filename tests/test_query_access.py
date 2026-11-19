from concurrent.futures import ThreadPoolExecutor
import json

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text
from sqlmodel import select

from news2douyin.server.app import create_app
from news2douyin.storage.access import QueryObserver, snapshot
from news2douyin.storage.db import make_engine
from news2douyin.storage.queries import count_rows, count_keys, project_keys, page_rows
from news2douyin.storage.models import Article, ArticleEventLink
from news2douyin.storage.query_indexes import NAME, READ_INDEXES, ensure_latest_index, ensure_read_indexes, main as index_main
from sqlmodel import Session


def test_key_count_keeps_predicates_and_distinct_without_wide_payload(engine):
    with Session(engine) as s:
        s.add_all([Article(article_key='a',title='One',content='private '*1000),
                   Article(article_key='b',title='Two'),
                   ArticleEventLink(article_key='a',event_key='e1'),
                   ArticleEventLink(article_key='a',event_key='e2')])
        s.commit()
        query=select(Article).join(ArticleEventLink,ArticleEventLink.article_key==Article.article_key)
        assert count_keys(s,query,Article.id)==2
        assert count_keys(s,query.distinct(),Article.id)==1
        assert count_rows(s,Article,Article.article_key=='b')==1
        sql=str(project_keys(query,Article.id))
        assert 'article.content' not in sql and 'article.raw_json' not in sql
        assert len(page_rows(s,select(Article),limit=1))==1
        with pytest.raises(ValueError):page_rows(s,select(Article),limit=501)


def test_observer_records_errors_and_names_without_sql_or_values(tmp_path):
    e=make_engine('sqlite:///'+str(tmp_path/'telemetry.db'))
    try:
        with e.connect() as c:
            c.execute(text('SELECT :secret').execution_options(n2d_query_name='test.secret'),{'secret':'sensitive-password-body'})
            with pytest.raises(Exception):
                c.execute(text('SELECT * FROM nonexistent').execution_options(n2d_query_name='test.failure'))
        data=snapshot(e);encoded=json.dumps(data)
        assert 'sensitive-password-body' not in encoded and 'SELECT' not in encoded
        assert 'nonexistent' not in encoded
        assert data['totals']['calls']==2 and data['totals']['errors']==1
        assert {row['name'] for row in data['operations']}=={'test.secret','test.failure'}
    finally:e.dispose()


def test_observer_is_bounded_thread_safe_and_totals_cover_eviction():
    observer=QueryObserver(capacity=8)
    def record(index):
        for _ in range(10):observer.record('test.'+str(index),1.5,False)
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(record,range(20)))
    data=observer.snapshot()
    assert len(data['operations'])==8
    assert data['totals']['calls']==200 and data['totals']['total_ms']==300


def test_existing_query_index_is_explicit_idempotent_and_plan_opens_no_database(engine,tmp_path,capsys):
    assert index_main(['--db-url','sqlite:///'+str(tmp_path/'not-created.db')])==0
    assert not (tmp_path/'not-created.db').exists()
    assert json.loads(capsys.readouterr().out)['status']=='plan'
    with engine.begin() as c:
        c.execute(text('DROP INDEX '+NAME))
    assert ensure_latest_index(engine)['created']
    assert not ensure_latest_index(engine)['created']
    with engine.begin() as c:
        c.execute(text('DROP INDEX '+NAME))
        c.execute(text('CREATE INDEX '+NAME+' ON articleversion(revision)'))
    with pytest.raises(RuntimeError,match='different definition'):
        ensure_latest_index(engine)


def test_managed_covering_indexes_can_be_explicitly_built_and_analyzed(engine):
    with engine.begin() as c:
        for _,name,_ in READ_INDEXES:
            c.execute(text('DROP INDEX '+name))
    result=ensure_read_indexes(engine,analyze=True)
    assert len(result['indexes'])==len(READ_INDEXES) and all(row['created'] for row in result['indexes'])
    assert len(result['analyzed'])==len(READ_INDEXES)
    assert not any(row['created'] for row in ensure_read_indexes(engine)['indexes'])
    with engine.begin() as c:
        c.execute(text('DROP INDEX '+NAME))
        c.execute(text('CREATE INDEX '+NAME+' ON articleversion(article_key,id) WHERE revision=1'))
    with pytest.raises(RuntimeError,match='different definition'):
        ensure_latest_index(engine)


def test_diagnostics_are_query_free_and_request_headers_are_opt_in(tmp_path,monkeypatch):
    app=create_app(db_url='sqlite:///'+str(tmp_path/'app.db'),storage_root=str(tmp_path/'runs'))
    client=TestClient(app)
    try:
        assert 'x-db-query-count' not in client.get('/api/system/status').headers
        monkeypatch.setenv('NEWS2DOUYIN_DB_QUERY_HEADERS','1')
        response=client.get('/api/system/status')
        assert int(response.headers['X-DB-Query-Count'])>=4
        assert float(response.headers['X-DB-Time-Ms'])>=0
        before=snapshot(app.state.engine)['totals']['calls']
        report=client.get('/api/database/queries')
        assert report.status_code==200 and report.json()['scope']=='current_process'
        assert report.json()['database']['dialect']=='sqlite'
        assert report.json()['pool']['size']==5
        assert report.headers['X-DB-Query-Count']=='0'
        assert snapshot(app.state.engine)['totals']['calls']==before
    finally:client.close();app.state.engine.dispose()
