"""Installed-wheel stability acceptance. Uses temporary synthetic data only.

Run with python -I after installing the wheel. No upstream, model or production
service is contacted, and normal request handlers may not open the lake.
"""
import importlib.metadata
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
from unittest.mock import patch
from zipfile import ZipFile

for name in ('NEWS2DOUYIN_GKG_STREAM_ENABLED','NEWS2DOUYIN_GKG_PROJECTION_ENABLED',
             'NEWS2DOUYIN_GDELT_STREAM_ENABLED','NEWS2DOUYIN_GDELT_PROJECTION_ENABLED'):
    os.environ[name]='0'
os.environ['NEWS2DOUYIN_FALKOR_URL']=''
os.environ['NEWS2DOUYIN_SOURCE_CONTENT_ENABLED']='0'
os.environ['NEWS2DOUYIN_GKG_AUTO_EVENTS']='0'

from fastapi.testclient import TestClient
from sqlalchemy import event,inspect,text
from sqlmodel import Session,select
from news2douyin.server.app import create_app
from news2douyin.storage.db import init_db
from news2douyin.storage.models import Article
from news2douyin.intake.lake import Lake
from news2douyin.intake.models import IntakeBatch,FeedState
from news2douyin.intake import service,projection
from news2douyin.intake.fetch_diagnostics import snapshot,save


def main():
    assert importlib.metadata.version('news2douyin')=='0.40.2'
    cli=subprocess.run([sys.executable,'-I','-m','news2douyin.intake.fetch_diagnostics','--help'],capture_output=True,text=True)
    assert cli.returncode==0 and '--feed' in cli.stdout,cli.stderr or cli.stdout
    with tempfile.TemporaryDirectory(prefix='n2d-stability-wheel-') as temporary:
        folder=Path(temporary);root=folder/'runs'
        app=create_app(db_url='sqlite:///'+str(folder/'app.db'),storage_root=str(root));engine=app.state.engine
        client=TestClient(app)
        try:
            with engine.begin() as c:
                c.execute(text('CREATE INDEX ix_event_event_title ON event (event_title)'))
                c.execute(text('CREATE INDEX ix_article_source_domain ON article (source_domain)'))
            init_db(engine);init_db(engine)
            assert 'ix_event_event_title' not in {r['name'] for r in inspect(engine).get_indexes('event')}
            assert 'ix_article_source_domain' not in {r['name'] for r in inspect(engine).get_indexes('article')}
            assert 'ix_article_canonical_url' in {r['name'] for r in inspect(engine).get_indexes('article')}
            old='20261114000000';later='20261114001500';bad='gkg_english_'+old
            row=['']*27;row[0]=later+'-1';row[1]=later;row[2]='1';row[3]='fixture.example'
            row[4]='https://fixture.example/new-report';row[26]='<PAGE_TITLE>合成验收材料</PAGE_TITLE>'
            root.mkdir(parents=True,exist_ok=True);path=root/'fixture.zip'
            with ZipFile(path,'w') as z:z.writestr(later+'.gkg.csv','\t'.join(row)+'\n')
            details=Lake(root).ingest_archive('gkg_english',later,path)
            with Session(engine) as s:
                s.add(IntakeBatch(key=bad,feed='gkg_english',stamp=old,state='archived',rows=1))
                s.add(IntakeBatch(key='gkg_english_'+later,feed='gkg_english',stamp=later,state='archived',**details))
                state=s.get(FeedState,'events_english');state.lease='wheel-fixture-token';s.add(state)
                assert save(s,'events_english',snapshot('events_english',attempt_id='wheel-fixture-token',status='failed',phase='download',
                    request_url='https://data.gdeltproject.org/gdeltv2/'+old+'.export.CSV.zip',
                    target_stamp=old,http_status=404,error_kind='batch_not_found_unverified'))
                state.lease='';s.add(state)
                s.commit()
            try:projection.project_step(engine,str(root),now=1000)
            except (RuntimeError,KeyError):pass
            else:raise AssertionError('Missing archived input was silently acknowledged')
            assert projection.project_step(engine,str(root),now=1001)
            with Session(engine) as s:
                assert s.get(IntakeBatch,bad).projected_offset==0
                assert s.get(projection.IntakeProjectionFailure,bad).state=='blocked'
                assert s.exec(select(Article.title)).one()=='合成验收材料'
            statements=[]
            def audit(_connection,_cursor,statement,*args):statements.append(statement.lower())
            event.listen(engine,'before_cursor_execute',audit)
            try:
                with patch.object(Lake,'__init__',side_effect=AssertionError('GET opened the lake')),patch.object(service.requests,'get',side_effect=AssertionError('GET accessed upstream')):
                    state=client.get('/api/intake/status');assert state.status_code==200,state.text
                    assert client.get('/intake').status_code==200
                data=state.json()
                assert data['projection']['processed_rows']==1 and data['projection']['waiting']==1
                assert data['projection']['blocked_batches']==1
                feed=next(r for r in data['feeds'] if r['key']=='events_english')
                assert feed['fetch_diagnostics']['http_status']==404
                assert not any('count(' in sql.replace(' ','') and ('intakeprojectionreceipt' in sql or 'versionreading' in sql) for sql in statements)
            finally:event.remove(engine,'before_cursor_execute',audit)
            response=client.post('/api/intake/projection/'+bad+'/retry',json={})
            assert response.status_code==200,response.text
            with Session(engine) as s:
                assert s.get(IntakeBatch,bad).projected_offset==0
                assert s.get(projection.IntakeProjectionFailure,bad).retry_after==0
            assert client.get('/research-assets/intake.js').status_code==200
            assert client.get('/articles').status_code==200
        finally:client.close();engine.dispose()
    print(json.dumps(dict(status='passed',version='0.40.2',checks=[
        'packaged_new_modules_and_assets','diagnostic_cli_entrypoint','legacy_text_index_migration_idempotent','canonical_index_preserved',
        'missing_archive_cursor_not_advanced','independent_batch_progress','persistent_blocked_state',
        'durable_fetch_diagnostics','intake_get_no_lake_or_network','no_receipt_or_reading_count',
        'explicit_retry_preserves_gap','articles_route'],production_accessed=False,remote_model_contacted=False)))


if __name__=='__main__':main()
