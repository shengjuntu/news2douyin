"""Exercise NewsNow collection through two disposable local HTTP services.

Uses the real durable TaskWorker, SQLite and a synthetic NewsNow endpoint.
No deployed host, remote news source, LLM or graph service is contacted.
Screenshots are internal QA files under the specified scratch output folder.
"""
from __future__ import annotations
import argparse
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

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'tests'))
import requests
import uvicorn
from fastapi import Response
from sqlalchemy import func
from sqlmodel import Session,select
from newsnow_test_support import newsnow_http
from news2douyin.server.app import create_app
from news2douyin.storage.models import Article,ArticleVersion,Event
from news2douyin.collect import service as collect
from unittest.mock import patch


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    parser.add_argument('--article-guard-only',action='store_true')
    args=parser.parse_args();output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    for flag in ('NEWS2DOUYIN_GKG_STREAM_ENABLED','NEWS2DOUYIN_GKG_PROJECTION_ENABLED',
                 'NEWS2DOUYIN_GDELT_STREAM_ENABLED','NEWS2DOUYIN_GDELT_PROJECTION_ENABLED'):
        os.environ[flag]='0'
    os.environ['NEWS2DOUYIN_FALKOR_URL']=''
    with tempfile.TemporaryDirectory(prefix='newsnow-browser-') as tmp,newsnow_http() as upstream:
        folder=Path(tmp);app=create_app(db_url='sqlite:///'+str(folder/'app.db'),storage_root=str(folder/'runs'))
        guard_article=''
        if args.article_guard_only:
            with Session(app.state.engine) as session:
                collect.create_or_update_profile(session,dict(name='Guard discovery',provider='newsnow',
                    base_url=upstream.base,newsnow_sources=['zhihu'],max_items=1,filter_mode='rules',
                    country='',language='',categories=[],keywords_include=[]))
                collect.run_collection(session,'Guard discovery',storage_root=folder/'runs')
                row=session.exec(select(Article)).one();guard_article=row.article_key;uri=row.url
            with patch.dict(collect.PROVIDERS,worldnewsapi=lambda _config:[dict(url=uri,title='Saved full report',
                    content='Donald Trump announced a tariff decision in this saved full report.',
                    published_at='2026-11-14T07:00:00Z',language='en',source=dict(domain='www.zhihu.com'))]):
                with Session(app.state.engine) as session:
                    collect.create_or_update_profile(session,dict(name='Guard full report',provider='worldnewsapi',
                        filter_mode='rules',categories=[],keywords_include=[]))
                    collect.run_collection(session,'Guard full report',storage_root=folder/'runs')
        graph_calls=[]
        def forbidden(*args,**kwargs):
            graph_calls.append(True);raise AssertionError('NewsNow discovery contacted graph service')
        app.state.desk.graph.query=forbidden;app.state.desk.graph.read=forbidden
        @app.get('/favicon.ico',include_in_schema=False)
        def favicon():return Response(status_code=204)
        @app.get('/qa/config',include_in_schema=False)
        def config():return dict(newsnow_base=upstream.base,guard_article=guard_article)
        @app.get('/qa/state',include_in_schema=False)
        def state():
            with Session(app.state.engine) as session:
                return dict(counts={m.__name__:session.exec(select(func.count()).select_from(m)).one()
                    for m in (Article,ArticleVersion,Event)},articles=[dict(key=row.article_key,title=row.title,
                    published_at=row.published_at,content=row.content) for row in session.exec(select(Article))],
                    upstream_calls=list(upstream.calls),graph_calls=len(graph_calls),
                    manifests=len(list((folder/'runs').rglob('newsnow-snapshots.json'))))
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        base='http://127.0.0.1:'+str(port)
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,lifespan='off',log_level='warning'))
        thread=threading.Thread(target=server.run,daemon=True);thread.start();app.state.worker.start()
        try:
            for _ in range(100):
                try:
                    if requests.get(base+'/api/health',timeout=.2).ok:break
                except requests.RequestException:time.sleep(.05)
            else:raise RuntimeError('Local app did not start')
            env=dict(os.environ,N2D_QA_URL=base,N2D_QA_OUTPUT=str(output))
            result=subprocess.run(['node',str(ROOT/'tools/check_newsnow_browser.cjs')],env=env,timeout=180)
            if result.returncode:raise SystemExit(result.returncode)
            filename='newsnow-article-guard-browser.json' if args.article_guard_only else 'newsnow-browser.json'
            report=json.loads((output/filename).read_text(encoding='utf-8'))
            report.update(version='0.30.0',recorded_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                real_http=True,real_chromium=True,database='disposable SQLite',
                provider='disposable NewsNow-shaped HTTP fixture from documented field shapes',
                production_accessed=False,remote_model_contacted=False,graph_http_calls=len(graph_calls))
            assert not graph_calls
            (output/filename).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            print(json.dumps(report,ensure_ascii=False))
        finally:
            app.state.worker.stop();server.should_exit=True;thread.join(timeout=10);app.state.engine.dispose()


if __name__=='__main__':main()
