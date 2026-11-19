"""Cross-module upgrade/recovery acceptance, with no production services.

The fixtures preserve an old title index, replay real ZIPs through the lake,
advance public worker functions and audit the actual intake HTTP read path.
"""
from hashlib import sha256
import base64
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sql_event, func, inspect, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlmodel import Session, select

from news2douyin.intake import projection, service as intake
from news2douyin.intake.lake import Lake
from news2douyin.intake.models import IntakeBatch, FeedState
from news2douyin.intake.official_models import OfficialEvent
from news2douyin.server.app import create_app
from news2douyin.storage.db import init_db, make_engine
from news2douyin.storage.models import Article, ArticleVersion, Event
from sql_readonly_audit import is_read_only_execution
from test_postgres_integration import postgres_url
from test_intake_projection import archive as archive_gkg, line as gkg_line
from test_official_events import event_line, zipped
from test_intake_sample_window import sampler

STAMP = '20261114041500'
LATER = '20261114043000'


@pytest.fixture(params=['sqlite', 'postgres'])
def stability_case(request, tmp_path, monkeypatch):
    for name in ('NEWS2DOUYIN_GKG_STREAM_ENABLED', 'NEWS2DOUYIN_GKG_PROJECTION_ENABLED',
                 'NEWS2DOUYIN_GDELT_STREAM_ENABLED', 'NEWS2DOUYIN_GDELT_PROJECTION_ENABLED'):
        monkeypatch.setenv(name, '0')
    monkeypatch.setenv('NEWS2DOUYIN_GKG_AUTO_EVENTS', '0')
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', '')
    url = request.getfixturevalue('postgres_url') if request.param == 'postgres' else 'sqlite:///' + str(tmp_path/'case.db')
    root = tmp_path/'runs'
    app = create_app(db_url=url, storage_root=str(root))
    client = TestClient(app)  # No lifespan: never start remote/background workers.
    try:
        yield app, client, root
    finally:
        client.close(); app.state.engine.dispose()


def count(session, model):
    return session.exec(select(func.count()).select_from(model)).one()


def stage_event(engine, root, stamp=STAMP):
    path = root/'official-event.zip'; path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zipped([event_line(added=stamp)], feed='events_english', stamp=stamp))
    stored = Lake(root).ingest_archive('events_english', stamp, path)
    key = 'events_english_' + stamp
    with Session(engine) as s:
        s.add(IntakeBatch(key=key, feed='events_english', stamp=stamp, state='archived', **stored)); s.commit()
    return key


def drain(engine, root, now=1001):
    for _ in range(30):
        if not projection.project_step(engine, root, limit=10, time_budget_ms=60000, now=now): return
    raise AssertionError('Tiny stability fixture did not reach an idle/retry boundary')


def long_title():
    # High-entropy CJK avoids PostgreSQL TOAST compressing a repeated character
    # below its B-tree entry limit, while staying inside the document contract.
    return '长标题回归 ' + ''.join(chr(0x4E00 + int.from_bytes(sha256(str(n).encode()).digest()[:2], 'big') % 20000)
                             for n in range(3000))


def test_real_old_pg_title_index_upgrade_and_gkg_zero_updates_linked_event(postgres_url, tmp_path, monkeypatch):
    from news2douyin.candidates.promotion import approve_collected_article

    monkeypatch.setenv('NEWS2DOUYIN_GKG_AUTO_EVENTS', '0')
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', '')
    root = tmp_path/'runs'; engine = make_engine(postgres_url); init_db(engine)
    payload = dict(provider='gkg', record_id='legacy-link', uri='https://media.example/article/1',
                   title='Short old title', content='', observed_at='2026-11-14T04:00:00Z')
    selected = projection.import_document(engine, payload)
    with Session(engine) as session:
        old_article = session.exec(select(Article)).one()
        approve_collected_article(session,old_article,event_key='legacy-event',decision='legacy_auto',
                                  reason='existing_event_fixture')
        session.commit()
    with engine.begin() as connection:
        connection.execute(text('CREATE INDEX ix_event_event_title ON event (event_title)'))
    value = long_title()
    with pytest.raises(DBAPIError) as rejected:
        with Session(engine) as session:
            row = session.exec(select(Event)).one(); row.event_title=value; session.add(row); session.commit()
    assert getattr(rejected.value.orig, 'sqlstate', '') == '54000'
    engine.dispose()

    # The normal application constructor performs the upgrade before any worker
    # writes. It must preserve existing sources and the old local event link.
    app = create_app(db_url=postgres_url, storage_root=str(root)); client = TestClient(app)
    try:
        assert 'ix_event_event_title' not in {r['name'] for r in inspect(app.state.engine).get_indexes('event')}
        key = archive_gkg(app.state.engine, root, [gkg_line(STAMP, 1, value)], stamp=STAMP)
        assert projection.project_step(app.state.engine, root, limit=1, time_budget_ms=60000)
        with Session(app.state.engine) as session:
            assert count(session, Event)==1
            article=session.exec(select(Article)).one(); event=session.exec(select(Event)).one()
            assert article.article_key==selected['article_key'] and article.title==value
            assert event.event_title==value
            assert session.get(IntakeBatch,key).projected_offset==1
        init_db(app.state.engine)
        assert 'ix_event_event_title' not in {r['name'] for r in inspect(app.state.engine).get_indexes('event')}
        assert client.get('/articles').status_code==200
    finally:
        client.close(); app.state.engine.dispose()


def test_poison_batch_preserves_gap_but_other_kind_and_later_batch_continue(stability_case, monkeypatch):
    app, client, root=stability_case; engine=app.state.engine
    bad=archive_gkg(engine,root,[gkg_line(STAMP,1,'First good row'),gkg_line(STAMP,2,'QA poison row'),
        gkg_line(STAMP,3,'Third good row')],stamp=STAMP)
    later=archive_gkg(engine,root,[gkg_line(LATER,4,'Later independent batch')],stamp=LATER)
    official=stage_event(engine,root)
    original=projection._upsert
    def poison(s,document,**kwargs):
        if document.title=='QA poison row':raise ValueError('QA deterministic source failure')
        return original(s,document,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(projection,'_upsert',poison)
        with pytest.raises(ValueError,match='deterministic source'):
            projection.project_step(engine,root,limit=3,time_budget_ms=60000,now=1000)
        with Session(engine) as s:
            assert s.get(IntakeBatch,bad).projected_offset==0
            assert count(s,Article)==count(s,projection.IntakeProjectionReceipt)==0
            failure=s.get(projection.IntakeProjectionFailure,bad)
            assert failure.state=='blocked' and failure.retry_after>1000 and failure.phase
            assert failure.row_index==2
        drain(engine,root)
    with Session(engine) as s:
        assert s.get(IntakeBatch,bad).projected_offset==0
        assert s.get(IntakeBatch,later).projected_offset==1
        assert s.get(IntakeBatch,official).projected_offset==1
        assert count(s,Article)==1 and count(s,OfficialEvent)==1
    state=projection.projection_status(engine)
    assert state['waiting']==3 and state['unresolved_batches']==1
    assert state['processed_rows']==2
    assert Lake(root).rows('gkg_english',STAMP,after=0,limit=10)[1]['title']=='QA poison row'
    init_db(engine)
    with Session(engine) as s:assert s.get(projection.IntakeProjectionFailure,bad).state=='blocked'
    result=projection.retry_projection(engine,bad)
    assert result['queued']
    drain(engine,root)
    with Session(engine) as s:
        assert s.get(IntakeBatch,bad).projected_offset==3
        assert count(s,Article)==count(s,ArticleVersion)==4
        assert count(s,projection.IntakeProjectionReceipt)==5
        before=[(r.article_key,r.content_hash) for r in s.exec(select(ArticleVersion).order_by(ArticleVersion.id))]
        batch=s.get(IntakeBatch,bad);batch.projected_offset=0;s.add(batch);s.commit()
    drain(engine,root)
    with Session(engine) as s:
        assert count(s,projection.IntakeProjectionReceipt)==5
        assert [(r.article_key,r.content_hash) for r in s.exec(select(ArticleVersion).order_by(ArticleVersion.id))]==before


def test_database_outage_does_not_become_a_bad_source_or_error_write_loop(stability_case, monkeypatch):
    app,_client,root=stability_case;engine=app.state.engine
    key=archive_gkg(engine,root,[gkg_line(STAMP,1,'Ordinary headline')],stamp=STAMP)
    def unavailable(*args,**kwargs):raise OperationalError('sensitive SQL',{},Exception('password=never-log-this'))
    with monkeypatch.context() as patch:
        patch.setattr(projection,'_upsert',unavailable)
        with pytest.raises(OperationalError):projection.project_step(engine,root,limit=1,time_budget_ms=60000,now=1000)
    with Session(engine) as s:
        assert s.get(IntakeBatch,key).projected_offset==0
        assert s.get(projection.IntakeProjectionFailure,key) is None
        assert count(s,Article)==count(s,projection.IntakeProjectionReceipt)==0
    drain(engine,root)
    with Session(engine) as s:assert s.get(IntakeBatch,key).projected_offset==1


def test_intake_status_is_sql_readonly_and_never_scans_receipts_or_versions(stability_case, monkeypatch):
    app,client,root=stability_case;engine=app.state.engine
    archive_gkg(engine,root,[gkg_line(STAMP,1,'Ordinary headline')],stamp=STAMP)
    drain(engine,root)
    calls=[]
    def forbidden(*args,**kwargs):calls.append('external');raise AssertionError('Status read opened archive/network/graph')
    import requests
    monkeypatch.setattr(requests,'get',forbidden)
    for method in ('__init__','rows','search','archive_reference'):
        monkeypatch.setattr(Lake,method,forbidden)
    for method in ('query','read','explain'):
        monkeypatch.setattr(app.state.desk.graph,method,forbidden)
    executions=[]
    def audit(_connection,_cursor,statement,_parameters,context,_executemany):
        executions.append((statement,is_read_only_execution(statement,context)))
    sql_event.listen(engine,'before_cursor_execute',audit)
    try:
        for path in ('/intake','/api/intake/status'):
            response=client.get(path);assert response.status_code==200,response.text
        assert executions and all(read for _,read in executions)
        assert not any('intakeprojectionreceipt' in sql.lower() or 'versionreading' in sql.lower() for sql,_ in executions),executions
        assert calls==[]
    finally:sql_event.remove(engine,'before_cursor_execute',audit)


def test_diagnostic_and_projection_retry_are_explicit_same_origin_actions(stability_case,monkeypatch):
    app,client,root=stability_case;engine=app.state.engine
    key=archive_gkg(engine,root,[gkg_line(STAMP,1,'Retry me')],stamp=STAMP)
    def poison(*args,**kwargs):raise ValueError('QA retryable source failure')
    with monkeypatch.context() as patch:
        patch.setattr(projection,'_upsert',poison)
        with pytest.raises(ValueError):projection.project_step(engine,root,limit=1,now=1000)
    probe_calls=[]
    def probe(*args,**kwargs):
        probe_calls.append((args,kwargs))
        return dict(feed='events_english',manifest=dict(status='ok'),target=dict(status='unavailable'),latest=dict(status='ok'))
    monkeypatch.setattr(intake,'probe',probe)
    with Session(engine) as s:
        before=[r.model_dump() for r in s.exec(select(FeedState).order_by(FeedState.key))]
        retry_after=s.get(projection.IntakeProjectionFailure,key).retry_after
    foreign=dict(Origin='https://untrusted.example')
    assert client.post('/api/intake/feeds/events_english/probe',json={},headers=foreign).status_code==403
    assert client.post('/api/intake/projection/'+key+'/retry',json={},headers=foreign).status_code==403
    assert probe_calls==[]
    with Session(engine) as s:assert s.get(projection.IntakeProjectionFailure,key).retry_after==retry_after
    local=dict(Origin='http://testserver')
    result=client.post('/api/intake/feeds/events_english/probe',json={},headers=local)
    assert result.status_code==200,result.text
    assert len(probe_calls)==1
    with Session(engine) as s:
        assert [r.model_dump() for r in s.exec(select(FeedState).order_by(FeedState.key))]==before
    retried=client.post('/api/intake/projection/'+key+'/retry',json={},headers=local)
    assert retried.status_code==200 and retried.json()['queued'],retried.text
    with Session(engine) as s:
        assert s.get(IntakeBatch,key).projected_offset==0
        assert count(s,projection.IntakeProjectionReceipt)==0
    drain(engine,root)
    with Session(engine) as s:assert s.get(IntakeBatch,key).projected_offset==1


def test_real_pg_long_canonical_url_is_a_visible_gap_not_a_global_projection_stop(postgres_url,tmp_path,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_GKG_AUTO_EVENTS','0')
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL','')
    root=tmp_path/'runs';app=create_app(db_url=postgres_url,storage_root=str(root));engine=app.state.engine
    try:
        # canonical_url's legacy equality index is intentionally retained in
        # this release. Exercise an actual PostgreSQL data failure, rather than
        # assuming every SQLAlchemy error is an outage or faking a ValueError.
        token=''.join(base64.urlsafe_b64encode(sha256(str(n).encode()).digest()).decode().rstrip('=') for n in range(150))
        uri='https://media.example/'+token
        assert 6000<len(uri)<8000
        bad=archive_gkg(engine,root,[gkg_line(STAMP,1,'Long source address',url=uri)],stamp=STAMP)
        good=stage_event(engine,root)
        with pytest.raises(DBAPIError) as rejected:
            projection.project_step(engine,root,limit=1,time_budget_ms=60000,now=1000)
        assert getattr(rejected.value.orig,'sqlstate','')=='54000'
        with Session(engine) as s:
            failure=s.get(projection.IntakeProjectionFailure,bad)
            assert failure.state=='blocked' and failure.row_index==1
            assert '54000' in failure.error
            assert s.get(IntakeBatch,bad).projected_offset==0
            assert count(s,Article)==0
        drain(engine,root)
        with Session(engine) as s:
            assert s.get(IntakeBatch,good).projected_offset==1
            assert s.get(IntakeBatch,bad).projected_offset==0
            assert count(s,OfficialEvent)==1
        assert Lake(root).rows('gkg_english',STAMP,limit=1)[0]['url']==uri
        # Retrying the unchanged input must not silently truncate or consume it.
        projection.retry_projection(engine,bad)
        with pytest.raises(DBAPIError):projection.project_step(engine,root,limit=1,now=1002)
        with Session(engine) as s:
            assert s.get(IntakeBatch,bad).projected_offset==0
            assert s.get(projection.IntakeProjectionFailure,bad).attempts==2
            assert count(s,projection.IntakeProjectionReceipt)==1  # The independent official row only.
    finally:engine.dispose()


def test_upgraded_unknown_totals_still_expose_measurable_per_feed_window_progress(stability_case):
    app,client,root=stability_case;engine=app.state.engine
    archive_gkg(engine,root,[gkg_line(STAMP,n,'Window source '+str(n)) for n in range(1,4)],stamp=STAMP)
    # Emulate a pre-meter deployment. No classified history scan is permitted;
    # the durable source offsets remain a complete progress measure.
    with Session(engine) as s:
        s.add(projection.IntakeProjectionReceipt(key='old-external-receipt',feed='external:legacy',stamp='',status='projected'))
        s.commit()
    before=client.get('/api/intake/status');assert before.status_code==200
    assert projection.project_step(engine,root,limit=1,time_budget_ms=60000,now=1000)
    after=client.get('/api/intake/status');assert after.status_code==200
    first,last=before.json(),after.json()
    assert first['projection']['projected'] is None and last['projection']['projected'] is None
    assert last['projection']['since_upgrade']['projected']==1
    report=sampler().summarize([dict(elapsed_seconds=0,data=first),dict(elapsed_seconds=2,data=last)])
    assert report['processed']['delta']==1 and report['processed']['per_second']==.5
    assert report['waiting_first']==3 and report['waiting_last']==2
    gkg=next(row for row in report['per_feed'] if row['feed']=='gkg_english')
    assert gkg['processed']['delta']==1
    assert gkg['scope']=='since_upgrade_only'


def test_actual_probe_module_entrypoint_reports_missing_readonly_database_without_creating_it(tmp_path):
    missing=tmp_path/'not-created'/'app.db'
    env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
    result=subprocess.run([sys.executable,'-m','news2douyin.intake.fetch_diagnostics',
        '--feed','events_english','--db-url','sqlite:///'+str(missing)],
        cwd=tmp_path,env=env,capture_output=True,text=True,timeout=15)
    assert result.returncode==2,result.stdout+result.stderr
    assert json.loads(result.stdout)['error_kind']=='diagnostic_unavailable'
    assert not missing.parent.exists()
