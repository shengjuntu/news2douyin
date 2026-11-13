"""Real PostgreSQL + DuckDB + FalkorDB with the application's background threads.

Opt in with NEWS2DOUYIN_TEST_POSTGRES_URL (the QA server wrapper supplies it).
Only upstream HTTP is replaced: fixtures retain duplicate, untitled and invalid
rows. Every test owns a random PostgreSQL database and a fresh local graph.
"""
from collections import Counter
import hashlib
import io
import os
from pathlib import Path
import socket
import subprocess
from threading import Lock,get_ident
import time
from uuid import uuid4
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sql_event,func
from sqlalchemy.engine import make_url
from sqlmodel import Session,select

from news2douyin.desk.models import DeskDirty,GraphPending,RadarEntry,WatchEvent
from news2douyin.extraction import reader
from news2douyin.extraction.models import TextMention,VersionReading
from news2douyin.intake import service
from news2douyin.intake.lake import Lake
from news2douyin.intake.models import FeedState,IntakeBatch
from news2douyin.intake.projection import IntakeProjectionReceipt
from news2douyin.server.app import create_app
from news2douyin.storage.models import Article,ArticleVersion,Event


@pytest.fixture
def pipeline_postgres_url():
    configured=os.getenv('NEWS2DOUYIN_TEST_POSTGRES_URL','').strip()
    if not configured:pytest.skip('real PostgreSQL pipeline requires NEWS2DOUYIN_TEST_POSTGRES_URL')
    psycopg=pytest.importorskip('psycopg')
    from psycopg import sql
    url=make_url(configured)
    if url.get_backend_name()!='postgresql':pytest.fail('PostgreSQL QA URL required')
    name='n2d_pipeline_test_'+uuid4().hex
    native=url.set(drivername='postgresql',database='postgres').render_as_string(hide_password=False)
    with psycopg.connect(native,autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:yield url.set(drivername='postgresql+psycopg',database=name).render_as_string(hide_password=False)
    finally:
        with psycopg.connect(native,autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


@pytest.fixture
def pipeline_falkor(tmp_path,monkeypatch):
    redislite=pytest.importorskip('redislite')
    from redis import Redis
    binary=Path(redislite.__file__).parent/'bin'
    if not all((binary/name).is_file() for name in ('redis-server','falkordb.so')):
        pytest.skip('QA FalkorDB server binaries are unavailable')
    with socket.socket() as probe:
        probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    folder=tmp_path/'falkor';folder.mkdir()
    log=(folder/'server.log').open('w')
    process=subprocess.Popen([str(binary/'redis-server'),'--loadmodule',str(binary/'falkordb.so'),
        '--port',str(port),'--bind','127.0.0.1','--dir',str(folder),'--save','','--appendonly','no'],
        stdout=log,stderr=subprocess.STDOUT)
    client=Redis(host='127.0.0.1',port=port,decode_responses=True,socket_connect_timeout=.2,socket_timeout=5)
    try:
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            try:
                if client.ping():break
            except Exception:time.sleep(.05)
        else:pytest.fail((folder/'server.log').read_text())
        prefix='pipeline_'+uuid4().hex[:12]
        monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL',f'redis://127.0.0.1:{port}')
        monkeypatch.setenv('NEWS2DOUYIN_GRAPH_PREFIX',prefix)
        yield client,prefix
    finally:
        client.close();process.terminate()
        try:process.wait(timeout=5)
        except subprocess.TimeoutExpired:process.kill();process.wait()
        log.close()


def fixture_line(stamp,index,*,translation=False,title=True,url=''):
    row=['']*27
    row[:5]=[stamp+('-T' if translation else '-')+str(index),stamp,'1','qa.example',
        url or 'https://qa.example/'+('translation' if translation else 'english')+'/'+str(index)]
    row[7]='DIPLOMACY';row[9]='1#France#FR###';row[11]='donald trump;volodymyr zelensky'
    row[13]='Fixture organization'
    if title:row[26]='<PAGE_TITLE>Donald Trump signed the treaty in Paris yesterday. Report '+str(index)+'</PAGE_TITLE>'
    return '\t'.join(row)+'\n'


def fixture_zip(stamp,rows,*,translation=False):
    data=io.BytesIO()
    with ZipFile(data,'w') as bundle:
        bundle.writestr(stamp+('.translation' if translation else '')+'.gkg.csv',''.join(rows))
    blob=data.getvalue()
    return blob,dict(stamp=stamp,bytes=len(blob),md5=hashlib.md5(blob).hexdigest())


def test_background_pipeline_keeps_read_pages_available_and_external_io_outside_pg_transactions(
    pipeline_postgres_url,pipeline_falkor,tmp_path,monkeypatch,
):
    from redis import Redis
    graph,prefix=pipeline_falkor
    stamp='20261104120000'
    fixtures={
        'gkg_english':fixture_zip(stamp,[fixture_line(stamp,n,title=n!=17) for n in range(1,33)]+[
            fixture_line(stamp,33,url='https://qa.example/english/1'),
            fixture_line(stamp,34,url='citation:fixture-source'),
            'malformed retained raw line\n']),
        'gkg_translation':fixture_zip(stamp,[fixture_line(stamp,n,translation=True) for n in range(1,4)],translation=True),
    }
    monkeypatch.setenv('NEWS2DOUYIN_GKG_STREAM_ENABLED','1')
    monkeypatch.setenv('NEWS2DOUYIN_GKG_PROJECTION_ENABLED','1')
    monkeypatch.setenv('NEWS2DOUYIN_GKG_PROJECTION_CHUNK','6')
    monkeypatch.setenv('NEWS2DOUYIN_GKG_PROJECTION_BUDGET_MS','100')
    monkeypatch.setenv('NEWS2DOUYIN_EVENT_READER','rules')
    storage=tmp_path/'runs'
    app=create_app(db_url=pipeline_postgres_url,storage_root=str(storage))
    engine=app.state.engine
    active={};mutex=Lock();violations=[];boundaries=Counter()

    def began(connection):
        with mutex:active[id(connection)]=get_ident()
    def ended(connection):
        with mutex:active.pop(id(connection),None)
    sql_event.listen(engine,'begin',began)
    sql_event.listen(engine,'commit',ended)
    sql_event.listen(engine,'rollback',ended)

    def outside_transaction(label):
        with mutex:
            boundaries[label]+=1
            if get_ident() in active.values():violations.append(label)
        assert not violations,'external I/O retained a PostgreSQL transaction: '+repr(violations)

    def discover(feed):
        outside_transaction('upstream_manifest')
        return dict(fixtures[feed][1])
    def download(feed,batch,destination,*args):
        outside_transaction('upstream_archive')
        assert batch==stamp
        target=Path(destination);target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(fixtures[feed][0])
    monkeypatch.setattr(service,'discover',discover)
    monkeypatch.setattr(service,'download',download)
    ingest=Lake.ingest_archive;rows=Lake.rows;extract=reader.extract;redis_command=Redis.execute_command
    def tracked_ingest(self,*args,**kwargs):
        outside_transaction('duckdb_archive');return ingest(self,*args,**kwargs)
    def tracked_rows(self,*args,**kwargs):
        outside_transaction('duckdb_projection_read');return rows(self,*args,**kwargs)
    def tracked_extract(*args,**kwargs):
        outside_transaction('event_extraction');return extract(*args,**kwargs)
    def tracked_command(self,*args,**kwargs):
        outside_transaction('falkordb_command');return redis_command(self,*args,**kwargs)
    monkeypatch.setattr(Lake,'ingest_archive',tracked_ingest)
    monkeypatch.setattr(Lake,'rows',tracked_rows)
    monkeypatch.setattr(reader,'extract',tracked_extract)
    monkeypatch.setattr(Redis,'execute_command',tracked_command)

    observed_pending=False;read_rounds=0;progress=[];status={}
    try:
        with TestClient(app) as client:
            made=client.post('/api/focus',json=dict(name='特朗普持续跟踪',kind='人物',scope='Donald Trump',
                question='What changed?',auto_research=False))
            assert made.status_code==201,made.text
            watch=made.json()['key']
            deadline=time.monotonic()+90
            while time.monotonic()<deadline:
                response=client.get('/api/intake/status')
                assert response.status_code==200,response.text
                status=response.json()
                assert status['database_ok']
                waiting=status['projection']['waiting']
                progress.append(status['counters']['processed_rows'])
                observed_pending=observed_pending or waiting>0
                for path in ('/radar','/focus','/articles','/admin'):
                    page=client.get(path)
                    assert page.status_code==200,(path,page.status_code,page.text[:500])
                read_rounds+=1
                assert not violations
                if status['counters']['rows']==38 and waiting==0:
                    with Session(engine) as session:
                        pending=session.exec(select(func.count()).select_from(GraphPending).where(GraphPending.token!=GraphPending.written)).one()
                        dirty=session.exec(select(func.count()).select_from(DeskDirty)).one()
                        cards=session.exec(select(func.count()).select_from(RadarEntry)).one()
                        ready=session.exec(select(func.count()).select_from(VersionReading).where(VersionReading.state=='ready')).one()
                        focus=session.exec(select(func.count()).select_from(WatchEvent).where(WatchEvent.watch==watch)).one()
                    if not pending and not dirty and cards==34 and ready==34 and focus:
                        counts=graph.execute_command('GRAPH.RO_QUERY',prefix+'_active_global',
                            'MATCH (e:Event) RETURN count(e)')[1]
                        if counts==[[34]]:break
                time.sleep(.1)
            else:pytest.fail('background pipeline did not settle: '+repr(status))
            assert app.state.intake.running and app.state.intake_projection.running
            assert not app.state.intake.last_error and not app.state.intake_projection.last_error
            assert not app.state.desk.graph.last_error
            assert observed_pending and read_rounds>=3
            assert progress==sorted(progress) and progress[-1]==38
            assert status['projection']['projected']==36 and status['projection']['skipped']==2
            assert status['counters']['processed_rows']==38 and not status['projection']['waiting']
            with Session(engine) as session:
                batches=session.exec(select(IntakeBatch)).all()
                assert len(batches)==2 and all(b.state=='archived' and b.projected_offset==b.rows for b in batches)
                assert all(f.last_complete==stamp and f.next_batch=='20261104121500' for f in session.exec(select(FeedState)))
                for model,total in ((IntakeProjectionReceipt,38),(Article,35),(ArticleVersion,35),(Event,34)):
                    assert session.exec(select(func.count()).select_from(model)).one()==total
                assert session.exec(select(func.count()).select_from(TextMention)).one()>0
                assert session.exec(select(func.count()).select_from(VersionReading).where(VersionReading.state=='metadata_only')).one()==1
            assert Lake(storage).search()['total']==38
            english=Lake(storage).rows('gkg_english',stamp,limit=100)
            assert len(english)==35 and english[16]['title']==''
            assert english[-1]['raw_line']=='malformed retained raw line'
            assert english[0]['url']==english[32]['url']
            assert graph.exists(prefix+'_active_global') and graph.exists(prefix+'_focus_'+watch)
            assert graph.execute_command('GRAPH.RO_QUERY',prefix+'_active_global',
                'MATCH (m:EventMention) RETURN count(m)')[1][0][0]>0
            assert graph.execute_command('GRAPH.RO_QUERY',prefix+'_focus_'+watch,
                'MATCH (:Question)-[:HAS_MATERIAL]->(e:Event) RETURN count(e)')[1][0][0]>0
            for label in ('upstream_manifest','upstream_archive','duckdb_archive','duckdb_projection_read','event_extraction','falkordb_command'):
                assert boundaries[label]>0,label
            assert not violations
    finally:
        sql_event.remove(engine,'begin',began)
        sql_event.remove(engine,'commit',ended)
        sql_event.remove(engine,'rollback',ended)
        engine.dispose()
