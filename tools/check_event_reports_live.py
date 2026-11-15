"""Real app and browser: source headlines/excerpts replace coded pseudo-titles.

All articles are synthetic. Fetch HTML and translation are deterministic local
stubs; the durable enrichment/translation workers and HTTP APIs are real.
"""
from __future__ import annotations
import argparse
from contextvars import ContextVar
from datetime import datetime,timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'tests'));sys.path.insert(0,str(ROOT/'tools'))
import requests
import uvicorn
from fastapi import Response
from sqlalchemy import event as sql_event,delete
from sqlmodel import Session
from check_attention_live import disposable_url
from sql_readonly_audit import is_read_only_execution
from news2douyin.server.app import create_app
from news2douyin.intake.official_models import OfficialEvent,OfficialMention
from news2douyin.storage.articles import identity_key
from news2douyin.storage.models import Article,ArticleIdentity
from news2douyin.selection.source_models import SourcePreview,SourcePreviewJob
from news2douyin.selection import source_fetch
from news2douyin.translations import service as translations

LOCAL='合成报道：某港口工人举行罢工，货运等待时间延长'
LOCAL_BODY='合成来源描述：码头工人要求改善合同条件，港口部门正在安排临时货运计划。'
CACHED='Informe sintético: el puerto anuncia un plan de carga temporal'
CACHED_BODY='El informe describe reuniones laborales y cambios en los horarios de carga.'
FETCHED='合成补充报道：港口劳资双方恢复谈判'
FETCHED_BODY='合成来源描述：双方恢复会谈，新的排班方案仍待进一步协商。'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    parser.add_argument('--database',choices=['sqlite','postgres'],default='sqlite')
    args=parser.parse_args();output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    for flag in ('NEWS2DOUYIN_GKG_STREAM_ENABLED','NEWS2DOUYIN_GKG_PROJECTION_ENABLED',
                 'NEWS2DOUYIN_GDELT_STREAM_ENABLED','NEWS2DOUYIN_GDELT_PROJECTION_ENABLED','NEWS2DOUYIN_ATTENTION_ENABLED'):
        os.environ[flag]='0'
    os.environ['NEWS2DOUYIN_SOURCE_CONTENT_ENABLED']='1';os.environ['NEWS2DOUYIN_FALKOR_URL']=''
    with tempfile.TemporaryDirectory(prefix='event-reports-browser-') as temporary:
        folder=Path(temporary)
        with disposable_url(args.database,folder) as url:
            app=create_app(db_url=url,storage_root=str(folder/'runs'));engine=app.state.engine
            now=datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z');stamp='20261114120000'
            urls={key:'https://reports.example/'+key for key in ('local','cached','missing')}
            ids=dict(local='100001',cached='100002',missing='100003')
            with Session(engine) as session:
                for index,name in enumerate(urls,1):
                    session.add(OfficialEvent(event_id=ids[name],event_date=now[:10],first_seen=now,last_seen=now,
                        actor1_name='ZUNI',actor2_name='MOROCCO',event_root_code='19',event_code='190',
                        action_country_iso2='ma',action_place='Morocco',source_url=urls[name],
                        feed='events_english',stamp=stamp,row_index=index))
                    session.add(OfficialMention(key='report-mention-'+name,event_id=ids[name],doc_identifier=urls[name],
                        domain='reports.example',first_seen=now,last_seen=now,confidence=90,
                        first_feed='mentions_english',first_stamp=stamp,first_row_index=index,
                        last_feed='mentions_english',last_stamp=stamp,last_row_index=index))
                session.add(Article(article_key='saved-local-report',provider='worldnewsapi',url=urls['local'],
                    canonical_url=urls['local'],title=LOCAL,content=LOCAL_BODY,language='zh',source_domain='reports.example'))
                session.add(ArticleIdentity(identity_key=identity_key({'url':urls['local']}),article_key='saved-local-report'))
                session.add(SourcePreview(key=identity_key({'url':urls['cached']}),source_url=urls['cached'],final_url=urls['cached'],
                    title=CACHED,excerpt=CACHED_BODY,language='es',domain='reports.example',origin='source_page',
                    title_origin='og:title',excerpt_origin='og:description',content_hash=sha256(CACHED.encode()).hexdigest()))
                session.add(SourcePreviewJob(key=identity_key({'url':urls['missing']}),source_url=urls['missing'],
                    state='failed',attempts=1,retry_at=time.time()+600,error_kind='http_error',error='QA synthetic HTTP403'))
                session.commit()
            calls=dict(fetch=0,translation=0,translation_originals=[],enrich_posts=0)
            original_fetch,original_translate=source_fetch.safe_fetch,translations.translate
            def fetch(uri,**options):
                assert uri==urls['missing'],'Unrequested source fetch escaped the fixture'
                calls['fetch']+=1
                body=('<html lang="zh"><head><meta property="og:title" content="'+FETCHED+'"><meta property="og:description" content="'+FETCHED_BODY+'"></head><body><article><h1>'+FETCHED+'</h1><p>'+FETCHED_BODY+'</p></article></body></html>').encode()
                return uri,'text/html; charset=utf-8',body
            def translate(original,target):
                calls['translation']+=1;calls['translation_originals'].append(original)
                return '合成中文译读：港口宣布临时货运计划。来源仍需核查。'
            source_fetch.safe_fetch=fetch;translations.translate=translate
            request_path=ContextVar('event_report_request_path',default=('', ''))
            threads=[]
            def run_source():app.state.source_content.step()
            def run_translation():
                task=app.state.worker.service.claim(30)
                if task:app.state.worker.execute(task)
            @app.middleware('http')
            async def capture(request,call_next):
                token=request_path.set((request.method,request.url.path))
                try:
                    response=await call_next(request)
                    if response.status_code<300 and request.method=='POST':
                        target=None
                        if request.url.path=='/api/gdelt/content/enrich':calls['enrich_posts']+=1;target=run_source
                        elif request.url.path=='/api/translations':target=run_translation
                        if target:
                            thread=threading.Thread(target=target,daemon=True);threads.append(thread);thread.start()
                    return response
                finally:request_path.reset(token)
            executions=[]
            def audit(_connection,_cursor,statement,_parameters,context,_executemany):
                method,path=request_path.get()
                if method=='GET' and (path=='/radar' or path.startswith('/api/gdelt/content') or path.startswith('/api/gdelt/events/')):
                    executions.append(dict(sql=statement,read_only=is_read_only_execution(statement,context)))
            @app.get('/favicon.ico',include_in_schema=False)
            def favicon():return Response(status_code=204)
            @app.get('/qa/config',include_in_schema=False)
            def config():return dict(synthetic=True,ids=ids,local_title=LOCAL,local_excerpt=LOCAL_BODY,
                cached_title=CACHED,cached_excerpt=CACHED_BODY,fetched_title=FETCHED,fetched_excerpt=FETCHED_BODY)
            @app.get('/qa/state',include_in_schema=False)
            def state():return calls
            @app.post('/qa/reset-missing',include_in_schema=False)
            def reset():
                key=identity_key({'url':urls['missing']})
                with Session(engine) as session:
                    session.exec(delete(SourcePreview).where(SourcePreview.key==key))
                    job=session.get(SourcePreviewJob,key);job.state='failed';job.attempts=1;job.retry_at=time.time()+600;job.lease='';job.lease_until=0
                    session.add(job);session.commit()
                return dict(ok=True)
            @app.post('/qa/revise-cached',include_in_schema=False)
            def revise_cached(restore:bool=False):
                with Session(engine) as session:
                    row=session.get(SourcePreview,identity_key({'url':urls['cached']}))
                    row.title=CACHED if restore else CACHED+' (updated)'
                    row.excerpt=CACHED_BODY if restore else CACHED_BODY+' Updated QA source.'
                    row.content_hash=sha256((row.title+'\n'+row.excerpt).encode()).hexdigest()
                    session.add(row);session.commit()
                return dict(ok=True)
            with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
            base='http://127.0.0.1:'+str(port)
            server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,lifespan='off',log_level='warning'))
            thread=threading.Thread(target=server.run,daemon=True);thread.start()
            try:
                for _ in range(100):
                    try:
                        if requests.get(base+'/api/health',timeout=.2).ok:break
                    except requests.RequestException:time.sleep(.05)
                else:raise RuntimeError('Local QA app did not start')
                sql_event.listen(engine,'before_cursor_execute',audit)
                result=subprocess.run(['node',str(ROOT/'tools/check_event_reports_browser.cjs')],
                    env=dict(os.environ,N2D_QA_URL=base,N2D_QA_OUTPUT=str(output)),timeout=180)
                if result.returncode:raise SystemExit(result.returncode)
                for worker in threads:worker.join(timeout=3)
                report=json.loads((output/'event-reports-browser.json').read_text())
                assert executions and all(row['read_only'] for row in executions)
                assert calls['fetch']==2 and calls['enrich_posts']==2
                assert calls['translation']>=1 and all(CACHED in original and CACHED_BODY in original for original in calls['translation_originals'])
                report.update(version='0.30.0',recorded_at=now,database=args.database,
                    postgres_locale=os.getenv('NEWS2DOUYIN_TEST_POSTGRES_LOCALE',''),calls=calls,
                    get_sql_audit=dict(executions=len(executions),writes=0),production_accessed=False,
                    source_fetch='deterministic HTML response supplied to real preview parser/worker; no public site contacted',
                    translation='deterministic model stub on the real durable task worker',screenshots='scratch only')
                (output/'event-reports-browser.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
                print(json.dumps(report,ensure_ascii=False))
            finally:
                sql_event.remove(engine,'before_cursor_execute',audit)
                source_fetch.safe_fetch=original_fetch;translations.translate=original_translate
                server.should_exit=True;thread.join(timeout=10);engine.dispose()


if __name__=='__main__':main()
