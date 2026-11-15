"""Exercise intake diagnostics against a disposable app and synthetic SQL state.

No private host, official GDELT service, graph endpoint or model is contacted.
The explicit source-probe result is a deterministic local stub; dedicated
transport tests separately verify official-request behavior.
"""
from __future__ import annotations
import argparse
from contextvars import ContextVar
from datetime import datetime,timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'tests'))
import requests
import uvicorn
from fastapi import Response
from fastapi.testclient import TestClient
from sqlalchemy import event as sql_event
from sqlmodel import Session
from check_attention_live import disposable_url
from sql_readonly_audit import is_read_only_execution
from news2douyin.intake import service
from news2douyin.intake.fetch_diagnostics import IntakeFetchDiagnostic,snapshot
from news2douyin.intake.models import FeedState,IntakeBatch
from news2douyin.intake.projection import IntakeProjectionFailure,IntakeProjectionMeter,IntakeProjectionCounter
from news2douyin.intake.lake import Lake
from news2douyin.server.app import create_app


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    parser.add_argument('--database',choices=['sqlite','postgres'],default='sqlite')
    args=parser.parse_args();output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    for flag in ('NEWS2DOUYIN_GKG_STREAM_ENABLED','NEWS2DOUYIN_GKG_PROJECTION_ENABLED',
                 'NEWS2DOUYIN_GDELT_STREAM_ENABLED','NEWS2DOUYIN_GDELT_PROJECTION_ENABLED',
                 'NEWS2DOUYIN_ATTENTION_ENABLED'):
        os.environ[flag]='0'
    os.environ['NEWS2DOUYIN_FALKOR_URL']=''
    with tempfile.TemporaryDirectory(prefix='intake-stability-ui-') as temporary:
        with disposable_url(args.database,Path(temporary)) as url:
            app=create_app(db_url=url,storage_root=str(Path(temporary)/'runs'))
            app.state.intake=SimpleNamespace(running=True,last_error='GDELT HTTP 404')
            app.state.intake_projection=SimpleNamespace(running=True,last_error='',last_chunks={})
            stamp='20261114090000';target='20261114091500';latest='20261114100000'
            batch_key='gkg_english_'+stamp
            now=datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
            with Session(app.state.engine) as session:
                for feed in service.FEEDS:
                    row=session.get(FeedState,feed);row.enabled=True;row.latest_available=latest
                    row.next_batch=target;row.updated_at=now
                    if feed=='events_english':row.last_error='GDELT HTTP 404';row.retry_after=time.time()+600
                    else:row.last_complete=stamp
                    session.add(row)
                session.add(IntakeBatch(key=batch_key,feed='gkg_english',stamp=stamp,state='archived',
                    rows=1000,valid_rows=999,invalid_rows=1,projected_offset=37,sha256='a'*64))
                session.add(IntakeBatch(key='mentions_english_'+stamp,feed='mentions_english',stamp=stamp,state='archived',
                    rows=100,valid_rows=100,invalid_rows=0,projected_offset=100,sha256='b'*64))
                session.add(IntakeProjectionMeter(history_known=False))
                session.add(IntakeProjectionCounter(key='gkg_english',projected=5,skipped=2,
                    last_chunk_json=json.dumps(dict(at=now,offset_before=30,offset_after=37,
                        elapsed_ms=45.2,phase_ms=dict(select=1.2,lake_read=12.8,prepare=2.5,sql_lock=4.1,sql_apply=24.6,commit=None)))))
                session.add(IntakeProjectionFailure(key=batch_key,feed='gkg_english',stamp=stamp,offset=37,
                    phase='lake_read',error_type='ValueError',error='QA合成：归档校验不一致，原档与断点保留。',
                    error_context_json=json.dumps(dict(read_start=38,read_end=62,row_is_exact=False)),
                    attempts=3,retry_after=time.time()+900))
                detail=snapshot('events_english',status='failed',phase='download',
                    request_url=service.url_for('events_english',target),manifest_url=service.BASE+'lastupdate.txt',
                    manifest_listed_url=service.url_for('events_english',latest).replace('https://','http://'),
                    target_stamp=target,latest_stamp=latest,http_status=404,
                    error_kind='batch_not_found_unverified',message='QA合成：当前目标返回404，未断定永久缺档。',
                    completed_at=now,expected_md5='c'*32,expected_bytes=50000)
                session.add(IntakeFetchDiagnostic(feed='events_english',detail_json=json.dumps(detail)))
                session.commit()
            calls=dict(probe=0,raw_search=0,forbidden=0)
            def probe(_engine,feed):
                calls['probe']+=1
                return dict(feed=feed,target_stamp=target,latest_stamp=latest,completed_at=now,
                    message='QA合成检查完成：目标404、最新批次200；断点未移动。',cursor_unchanged=True,
                    manifest=dict(status='succeeded',http_status=200,request_url=service.BASE+'lastupdate.txt'),
                    target=dict(status='failed',http_status=404,request_url=service.url_for(feed,target),stamp=target,
                        error_kind='batch_not_found_unverified',message='目标响应404，原因待核'),
                    latest=dict(status='succeeded',http_status=200,request_url=service.url_for(feed,latest),stamp=latest))
            original_probe=service.probe;service.probe=probe
            def forbidden(*args,**kwargs):calls['forbidden']+=1;raise AssertionError('Unexpected archive/network access')
            def search(_self,**kwargs):
                calls['raw_search']+=1
                return dict(items=[],total=0,offset=kwargs['offset'],limit=kwargs['limit'])
            original_search,original_rows,original_connection=Lake.search,Lake.rows,Lake.connection
            Lake.search=search;Lake.rows=Lake.connection=forbidden
            path_context=ContextVar('intake_stability_path',default=('', ''))
            @app.middleware('http')
            async def capture(request,call_next):
                token=path_context.set((request.method,request.url.path))
                try:return await call_next(request)
                finally:path_context.reset(token)
            reads=[]
            def audit(_connection,_cursor,statement,_parameters,context,_executemany):
                method,path=path_context.get()
                if method=='GET' and path in ('/intake','/api/intake/status'):
                    reads.append(dict(sql=statement,read_only=is_read_only_execution(statement,context)))
            @app.get('/favicon.ico',include_in_schema=False)
            def favicon():return Response(status_code=204)
            @app.get('/qa/config',include_in_schema=False)
            def config():return dict(synthetic=True,batch_key=batch_key)
            @app.get('/qa/state',include_in_schema=False)
            def state():
                with Session(app.state.engine) as session:
                    batch=session.get(IntakeBatch,batch_key);failure=session.get(IntakeProjectionFailure,batch_key)
                    feed=session.get(FeedState,'events_english')
                    return dict(calls=calls,batch_offset=batch.projected_offset,retry_after=failure.retry_after,
                        receiver_cursor=feed.next_batch,feed_enabled=feed.enabled)
            with socket.socket() as probe_socket:probe_socket.bind(('127.0.0.1',0));port=probe_socket.getsockname()[1]
            base='http://127.0.0.1:'+str(port)
            server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,lifespan='off',log_level='warning'))
            thread=threading.Thread(target=server.run,daemon=True);thread.start()
            try:
                for _ in range(100):
                    try:
                        if requests.get(base+'/api/health',timeout=.2).ok:break
                    except requests.RequestException:time.sleep(.05)
                else:raise RuntimeError('Local QA app did not start')
                sql_event.listen(app.state.engine,'before_cursor_execute',audit)
                env=dict(os.environ,N2D_QA_URL=base,N2D_QA_OUTPUT=str(output))
                result=subprocess.run(['node',str(ROOT/'tools/check_intake_stability_browser.cjs')],env=env,timeout=180)
                if result.returncode:raise SystemExit(result.returncode)
                report=json.loads((output/'intake-stability-browser.json').read_text())
                assert reads and all(row['read_only'] for row in reads)
                expensive=[row for row in reads if 'versionreading' in row['sql'].lower() or
                    ('intakeprojectionreceipt' in row['sql'].lower() and ('count(' in row['sql'].lower().replace(' ','') or 'group by' in row['sql'].lower()))]
                assert not expensive
                assert calls['forbidden']==0
                report.update(version='0.29.1',database=args.database,
                    postgres_locale=os.getenv('NEWS2DOUYIN_TEST_POSTGRES_LOCALE',''),
                    recorded_at=now,synthetic=True,source_probe='deterministic local stub; official network not contacted',
                    production_accessed=False,remote_model_contacted=False,forbidden_calls=calls['forbidden'],
                    status_sql_audit=dict(executions=len(reads),writes=0,expensive_history_aggregations=0),
                    screenshots='Scratch only; not part of source archive')
                (output/'intake-stability-browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
                print(json.dumps(report,ensure_ascii=False))
            finally:
                sql_event.remove(app.state.engine,'before_cursor_execute',audit)
                service.probe=original_probe;Lake.search, Lake.rows,Lake.connection=original_search,original_rows,original_connection
                server.should_exit=True;thread.join(timeout=10);app.state.engine.dispose()


if __name__=='__main__':main()
