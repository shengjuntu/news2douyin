"""Exercise saved article-language/translation controls with HTTP and Chromium.

The browser mocks only the asynchronous translation API: no model or external
network is contacted. Temporary fixture databases and screenshots are scratch QA.
"""
import argparse
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
sys.path.insert(0,str(ROOT/'src'))
import requests
import uvicorn
from fastapi import Response
from sqlmodel import Session
from news2douyin.server.app import create_app
from news2douyin.storage.articles import record_version
from news2douyin.storage.models import Article


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('output',type=Path)
    args=parser.parse_args();output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    os.environ['NEWS2DOUYIN_GKG_STREAM_ENABLED']='0';os.environ['NEWS2DOUYIN_FALKOR_URL']=''
    with tempfile.TemporaryDirectory(prefix='n2d-article-ui-') as temporary:
        folder=Path(temporary)
        app=create_app(db_url='sqlite:///'+str(folder/'fixture.db'),storage_root=str(folder/'runs'))
        with Session(app.state.engine) as s:
            row=Article(article_key='lang-article',provider='fixture',title='Ancien titre',content='Une ancienne version vérifiable.',
                language='fr',country='fr',source_domain='source.example',url='https://source.example/report',published_at='2026-11-14T00:00:00Z')
            s.add(row);s.flush();record_version(s,row)
            row.title='Новый заголовок';row.content='Новая версия.';row.language='ru';s.add(row);s.flush();record_version(s,row)
            row=Article(article_key='lang-metadata',provider='gdelt',title='Notice',content='',language='',url='https://metadata.example/report')
            s.add(row);s.flush();record_version(s,row);s.commit()
        @app.get('/favicon.ico',include_in_schema=False)
        def favicon():return Response(status_code=204)
        with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,lifespan='off',log_level='warning'))
        thread=threading.Thread(target=server.run,daemon=True);thread.start()
        try:
            for _ in range(100):
                try:
                    if requests.get(base+'/api/health',timeout=.2).ok:break
                except requests.RequestException:time.sleep(.05)
            else:raise RuntimeError('Temporary HTTP fixture did not start')
            result=subprocess.run(['node',str(ROOT/'tools/check_article_translation_browser.cjs')],env=dict(os.environ,N2D_QA_URL=base,N2D_QA_OUTPUT=str(output)),timeout=120)
            if result.returncode:raise SystemExit(result.returncode)
            print((output/'article-translation-browser-results.json').read_text())
        finally:server.should_exit=True;thread.join(timeout=10);app.state.engine.dispose()


if __name__=='__main__':main()
