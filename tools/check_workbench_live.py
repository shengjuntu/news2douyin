"""Local integration runner. Real FalkorDB/browser; seeded research is explicitly a fixture."""
import json
import os
import socket
import subprocess
import sys
import threading
import time
import tempfile
import sqlite3
from pathlib import Path
import uvicorn
import requests

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
sys.path.insert(0,str(ROOT/'tests'))
from test_content_foundation import app_at,article,process,drain_graph,report_fixture
from news2douyin.desk.service import save_watch
from news2douyin.desk.rules import Scope,PRESETS
from news2douyin.server.app import create_app
from sqlalchemy import text
from sqlmodel import Session


def port():
    with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]


def main():
    os.environ['NEWS2DOUYIN_GKG_STREAM_ENABLED']='0'
    import redislite
    from redis import Redis
    out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
    temporary=tempfile.TemporaryDirectory(prefix='n2d-browser-')
    dbdir=Path(temporary.name)
    graph_port=port();http_port=port()
    binary=Path(redislite.__file__).parent/'bin'
    with (out/'falkor.log').open('w') as log:
        graph=subprocess.Popen([str(binary/'redis-server'),'--loadmodule',str(binary/'falkordb.so'),'--port',str(graph_port),
            '--bind','127.0.0.1','--dir',str(dbdir),'--save','','--appendonly','no'],stdout=log,stderr=subprocess.STDOUT)
        server=None
        try:
            client=Redis(port=graph_port,socket_connect_timeout=.2)
            for _ in range(100):
                try:
                    if client.ping():break
                except Exception:time.sleep(.05)
            os.environ['NEWS2DOUYIN_FALKOR_URL']=f'redis://127.0.0.1:{graph_port}'
            os.environ['NEWS2DOUYIN_GRAPH_PREFIX']='browser_qa'
            test_pg=os.getenv('NEWS2DOUYIN_TEST_POSTGRES_URL','')
            app=create_app(db_url=test_pg,storage_root=str(dbdir/'runs')) if test_pg else app_at(dbdir)
            samples=[('a','Donald Trump signed the treaty in Paris yesterday.','演示材料：特朗普签署条约'),
                ('b','Donald Trump signed the treaty in Paris yesterday.','演示材料：另一来源报道签署'),
                ('c','Donald Trump will visit Kyiv tomorrow.','演示材料：访问计划与援助议题'),
                ('d','Alice visited London yesterday.','演示材料：另一场外交访问')]
            for key,body,title in samples:article(app,key,body,title)
            process(app)
            with Session(app.state.engine) as s:
                focus=save_watch(s,Scope(**PRESETS[0])).key
            drain_graph(app)
            @app.get('/qa/focus')
            def get_focus():return {'key':focus}
            @app.post('/qa/complete-research/{key}')
            def seed_report(key:str):report_fixture(app,key);return {'fixture':True}
            server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=http_port,log_level='warning'))
            thread=threading.Thread(target=server.run,daemon=True);thread.start()
            for _ in range(100):
                try:
                    if requests.get(f'http://127.0.0.1:{http_port}/api/health',timeout=.3).ok:break
                except Exception:time.sleep(.05)
            env=dict(os.environ,N2D_QA_URL=f'http://127.0.0.1:{http_port}',N2D_QA_OUTPUT=str(out))
            result=subprocess.run(['node',str(ROOT/'tools/check_workbench_browser.cjs')],env=env,timeout=150)
            if result.returncode:raise SystemExit(result.returncode)
            if test_pg:
                with app.state.engine.connect() as connection:
                    assert connection.execute(text('SELECT 1')).scalar()==1
                print('REAL_POSTGRES_FALKOR_AND_BROWSER_PASS; research text was a controlled fixture')
            else:
                with sqlite3.connect(dbdir/'news.db') as connection:
                    assert connection.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
                print('REAL_FALKOR_AND_BROWSER_PASS; SQLite integrity ok; research text was a controlled fixture')
        finally:
            if server:
                server.should_exit=True
                thread.join(timeout=30)
            graph.terminate()
            try:graph.wait(timeout=5)
            except subprocess.TimeoutExpired:graph.kill();graph.wait()
            temporary.cleanup()


if __name__=='__main__':main()
