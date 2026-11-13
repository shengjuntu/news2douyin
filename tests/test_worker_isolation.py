"""Read paths never drain queues; bounded workers preserve concurrent invalidation."""
from concurrent.futures import ThreadPoolExecutor
import json
import time
from threading import Barrier, Event as Signal, Lock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sql_event, update
from sqlmodel import Session, select

from news2douyin.storage.db import make_engine, init_db
from news2douyin.storage.articles import record_version
from news2douyin.storage.models import Article, ArticleVersion, ArticleEventLink, Event
from news2douyin.desk import service as desk
from news2douyin.desk.models import DeskDirty, RadarEntry, GraphPending, GraphLease
from news2douyin.desk.graph import GraphMirror
from news2douyin.desk.leases import acquire, LeaseLost
from news2douyin.extraction import service as extraction
from news2douyin.extraction.models import VersionReading, TextMention
from news2douyin.server.app import create_app
from test_postgres_integration import postgres_url
from sql_readonly_audit import is_read_only_execution


@pytest.fixture(params=['sqlite','postgres'])
def isolated_engine(request,tmp_path):
    url=request.getfixturevalue('postgres_url') if request.param=='postgres' else 'sqlite:///'+str(tmp_path/'workers.db')
    engine=make_engine(url);init_db(engine)
    try:yield engine
    finally:engine.dispose()


def seed(engine,count=1):
    with Session(engine) as s:
        for n in range(count):
            key='worker_article_'+str(n)
            a=Article(article_key=key,title='Article '+str(n),content='Alice visited London yesterday.',published_at='2026-11-04T00:00:00Z')
            s.add(a);record_version(s,a)
            s.add(Event(event_key='worker_event_'+str(n),event_title='Event '+str(n)))
            s.add(ArticleEventLink(event_key='worker_event_'+str(n),article_key=key))
        s.commit()


def test_radar_and_focus_get_are_select_only_with_pending_work(tmp_path,isolated_engine):
    app=create_app(db_url=isolated_engine.url.render_as_string(hide_password=False),storage_root=str(tmp_path/'runs'))
    seed(app.state.engine)
    client=TestClient(app)
    watch=client.post('/api/focus',json={'name':'Readonly','scope':'Alice','kind':'人物'}).json()['key']
    statements=[]
    def record(conn,cursor,statement,parameters,context,executemany):
        statements.append((statement.strip(),is_read_only_execution(statement,context)))
    sql_event.listen(app.state.engine,'before_cursor_execute',record)
    try:
        for path in ('/radar','/focus','/focus/'+watch,'/api/radar/view','/api/knowledge/graph','/api/desk/status'):
            response=client.get(path);assert response.status_code==200,(path,response.text)
    finally:sql_event.remove(app.state.engine,'before_cursor_execute',record)
    assert statements
    assert all(readonly for _,readonly in statements),[sql for sql,readonly in statements if not readonly]
    with Session(app.state.engine) as s:
        assert s.get(DeskDirty,'worker_event_0') is not None
        assert s.get(RadarEntry,'worker_event_0') is None
    app.state.engine.dispose()


def test_parallel_drainers_process_disjoint_queue_items(isolated_engine,monkeypatch):
    seed(isolated_engine,12)
    processed=[];guard=Lock();original=desk.snapshot
    def snapshot(s,event):
        with guard:processed.append(event.event_key)
        return original(s,event)
    monkeypatch.setattr(desk,'snapshot',snapshot)
    barrier=Barrier(4)
    def worker(_):barrier.wait();return desk.drain(isolated_engine,limit=3)
    with ThreadPoolExecutor(4) as pool:counts=list(pool.map(worker,range(4)))
    assert sum(counts)==12 and len(processed)==len(set(processed))==12
    with Session(isolated_engine) as s:
        assert not s.exec(select(DeskDirty)).all()
        assert len(s.exec(select(RadarEntry)).all())==12
        assert len(s.exec(select(GraphPending)).all())==12


def test_invalidation_during_snapshot_is_requeued(isolated_engine,monkeypatch):
    seed(isolated_engine)
    captured=Signal();resume=Signal();original=desk.snapshot;calls=[]
    def snapshot(s,event):
        card=original(s,event)
        if not calls:
            calls.append(event.event_key);captured.set();assert resume.wait(10)
        return card
    monkeypatch.setattr(desk,'snapshot',snapshot)
    def update_article():
        with Session(isolated_engine) as s:
            article=s.exec(select(Article)).one();article.title='Corrected after snapshot'
            s.add(article);record_version(s,article);s.commit()
    with ThreadPoolExecutor(2) as pool:
        consuming=pool.submit(desk.drain,isolated_engine,1)
        assert captured.wait(10)
        producing=pool.submit(update_article)
        # Give the producer a chance to reach the dirty-row conflict.
        time.sleep(.1)
        resume.set();assert consuming.result(timeout=10)==1;producing.result(timeout=10)
    with Session(isolated_engine) as s:assert s.get(DeskDirty,'worker_event_0') is not None
    assert desk.drain(isolated_engine,1)==1
    with Session(isolated_engine) as s:
        assert 'Corrected after snapshot' in s.get(RadarEntry,'worker_event_0').sources
        assert s.get(DeskDirty,'worker_event_0') is None


def test_parallel_extraction_claims_do_not_duplicate_versions(isolated_engine):
    seed(isolated_engine,16)
    barrier=Barrier(4)
    def worker(_):
        barrier.wait();return [extraction.pick(isolated_engine,False)[0].id for _ in range(4)]
    with ThreadPoolExecutor(4) as pool:claims=sum(list(pool.map(worker,range(4))),[])
    assert len(claims)==len(set(claims))==16
    with Session(isolated_engine) as s:
        assert all(row.state=='rule_running' and row.lease for row in s.exec(select(VersionReading)))


def test_expired_extractor_cannot_replace_newer_result(isolated_engine,monkeypatch):
    seed(isolated_engine)
    waiting=Signal();resume=Signal();guard=Lock();calls=[];original=extraction.reader.extract
    def read(*args):
        with guard:first=not calls;calls.append(1)
        if first:
            waiting.set();assert resume.wait(10);return [],0
        return original(*args)
    monkeypatch.setattr(extraction.reader,'extract',read)
    with ThreadPoolExecutor(1) as pool:
        old=pool.submit(extraction.step,isolated_engine)
        assert waiting.wait(10)
        with Session(isolated_engine) as s:
            row=s.exec(select(VersionReading)).one();row.until=time.time()-1;s.add(row);s.commit()
        assert extraction.step(isolated_engine)
        resume.set();assert old.result(timeout=10)
    with Session(isolated_engine) as s:
        assert s.exec(select(TextMention)).all()
        assert s.exec(select(VersionReading)).one().state=='ready'


def test_reclaimed_graph_lease_rejects_old_acknowledgement(isolated_engine,monkeypatch):
    with Session(isolated_engine) as s:
        desk.enqueue_graph(s,'event/missing');s.commit()
    graph=GraphMirror();graph.url='redis://test.invalid';graph.bootstrapped=True
    graph._last_bootstrap=time.monotonic()
    monkeypatch.setattr(graph,'prepare_indexes',lambda *args:True)
    def graph_write(*args):
        lease=graph._projection_lease
        # While external I/O is in progress, another independent SQL write succeeds.
        with Session(isolated_engine) as s:
            s.add(Article(article_key='independent',title='Independent write'))
            s.exec(update(GraphLease).where(GraphLease.key==lease.key).values(until=time.time()-1));s.commit()
        with acquire(isolated_engine,lease.key) as replacement:
            assert replacement is not None and replacement.owner!=lease.owner
    monkeypatch.setattr(graph,'event',graph_write)
    with pytest.raises(LeaseLost):graph.step(isolated_engine)
    with Session(isolated_engine) as s:
        pending=s.get(GraphPending,'event/missing');assert pending.written!=pending.token
        assert s.exec(select(Article).where(Article.article_key=='independent')).one()


def test_parallel_graph_enqueue_preserves_latest_token(isolated_engine):
    with Session(isolated_engine) as s:
        desk.enqueue_graph(s,'event/e');s.commit()
        row=s.get(GraphPending,'event/e');row.written=row.token;s.add(row);s.commit()
        written=row.written
    def enqueue(_):
        with Session(isolated_engine) as s:desk.enqueue_graph(s,'event/e');s.commit()
    with ThreadPoolExecutor(4) as pool:list(pool.map(enqueue,range(8)))
    with Session(isolated_engine) as s:
        row=s.get(GraphPending,'event/e');assert row.written==written and row.token!=written


def test_graph_workers_serialize_external_writes_and_keep_new_token(isolated_engine,monkeypatch):
    with Session(isolated_engine) as s:desk.enqueue_graph(s,'event/e');s.commit()
    first,second=GraphMirror(),GraphMirror()
    for graph in (first,second):
        graph.url='redis://test.invalid';graph.bootstrapped=True
        graph._last_bootstrap=time.monotonic()
        monkeypatch.setattr(graph,'prepare_indexes',lambda *args:True)
    waiting=Signal();resume=Signal();second_calls=[]
    def write(*args):waiting.set();assert resume.wait(10)
    monkeypatch.setattr(first,'event',write)
    monkeypatch.setattr(second,'event',lambda *args:second_calls.append(1))
    with ThreadPoolExecutor(1) as pool:
        in_flight=pool.submit(first.step,isolated_engine)
        assert waiting.wait(10)
        assert second.step(isolated_engine) is False and not second_calls
        with Session(isolated_engine) as s:
            desk.enqueue_graph(s,'event/e');s.commit();latest=s.get(GraphPending,'event/e').token
        resume.set();assert in_flight.result(timeout=10)
    with Session(isolated_engine) as s:
        row=s.get(GraphPending,'event/e');assert row.token==latest and row.written!=latest
