"""Exercise maintenance UI over loopback HTTP and real Chromium.

All data is a disposable SQLite QA fixture. Application lifespan is disabled,
so no collectors, model providers, background workers or graph server run.
Graph methods deliberately fail if an HTTP handler attempts graph I/O.
Screenshots stay in the requested scratch output folder, never the source ZIP.
Set N2D_QA_CHROMIUM or N2D_QA_CHROMIUM_MODULE for the browser executable.
"""
from __future__ import annotations

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

from news2douyin.desk.graph import GraphMirror
from news2douyin.desk import graph_gc
from news2douyin.desk.graph_focus_models import GraphFocusState
from news2douyin.desk.graph_maintenance_models import GraphGcState
from news2douyin.desk.models import GraphPending
from news2douyin.server.app import create_app


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path,help='Scratch screenshots and browser results')
    args=parser.parse_args()
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    os.environ['NEWS2DOUYIN_GRAPH_GC_ENABLED']='1'
    os.environ['NEWS2DOUYIN_GKG_STREAM_ENABLED']='0'
    with tempfile.TemporaryDirectory(prefix='n2d-maintenance-browser-') as temporary:
        folder=Path(temporary)
        app=create_app(db_url='sqlite:///'+str(folder/'fixture.db'),storage_root=str(folder/'runs'))
        mirror=app.state.desk.graph
        mirror.url='redis://qa-unused.invalid'
        mirror.metrics.started-=300
        for _ in range(40):mirror.metrics.record('event',20,acknowledged=True,graph_ms=12,graph_calls=3)
        for _ in range(4):mirror.metrics.record('watch',100000,partial=True,graph_ms=90000,graph_calls=40)
        graph_calls=[]
        def forbidden(*_args,**_kwargs):
            graph_calls.append('forbidden_graph_io')
            raise AssertionError('Read/control HTTP request attempted graph I/O')
        mirror.query=mirror.read=mirror.explain=forbidden
        with Session(app.state.engine) as session:
            session.add(GraphGcState(key='browser-gc',namespace=mirror.prefix,graph='active_global',
                label='Article',cursor=942,through_key=1654,cycle=3,status='sweeping',phase='batch',
                scanned=500,deleted=7,cycle_scanned=25,cycle_deleted=2,next_due=0))
            session.add(GraphFocusState(namespace=mirror.prefix,watch='w_'+'a'*32,phase='events',
                scanned=400,changed=7,removed=0,batches=12))
            session.add(GraphPending(key='event/browser-fixture',token='new',written='old'))
            session.commit()
            checkpoint=session.get(GraphGcState,'browser-gc').model_dump()

        @app.get('/qa/maintenance-checkpoint')
        def qa_checkpoint():
            with Session(app.state.engine) as session:
                row=session.get(GraphGcState,'browser-gc')
                pending=session.get(GraphPending,'event/browser-fixture')
                return dict(checkpoint=row.model_dump(),pending=pending.model_dump(),graph_calls=len(graph_calls))

        @app.get('/favicon.ico',include_in_schema=False)
        def favicon():return Response(status_code=204)

        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        url=f'http://127.0.0.1:{port}'
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,lifespan='off',log_level='warning'))
        thread=threading.Thread(target=server.run,daemon=True);thread.start()
        try:
            for _ in range(100):
                try:
                    if requests.get(url+'/api/health',timeout=.2).ok:break
                except requests.RequestException:time.sleep(.05)
            else:raise RuntimeError('Disposable QA HTTP server did not start')
            env=dict(os.environ,N2D_QA_URL=url,N2D_QA_OUTPUT=str(output))
            result=subprocess.run(['node',str(ROOT/'tools/check_graph_maintenance_browser.cjs')],env=env,timeout=120)
            if result.returncode:raise SystemExit(result.returncode)
            with Session(app.state.engine) as session:
                assert session.get(GraphGcState,'browser-gc').model_dump()==checkpoint
                assert session.get(GraphPending,'event/browser-fixture').written=='old'
            other=GraphMirror();other.url=mirror.url;other.prefix=mirror.prefix
            assert graph_gc.control(app.state.engine,other)['paused'] is False
            assert not graph_calls
            report=json.loads((output/'graph-maintenance-browser-results.json').read_text())
            report.update(real_http=True,real_chromium=True,database='disposable SQLite',
                background_workers=False,graph_server=False,graph_http_calls=0,
                checkpoint_preserved=True,pending_projection_preserved=True,
                performance_fixture='event 20ms; topic batches 100000ms; synthetic telemetry, not a benchmark')
            (output/'graph-maintenance-browser-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps(report,ensure_ascii=False))
        finally:
            server.should_exit=True;thread.join(timeout=10)
            app.state.engine.dispose()


if __name__=='__main__':main()
