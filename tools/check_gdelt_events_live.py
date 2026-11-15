"""Check official-event discovery UI over isolated loopback HTTP and Chromium.

No collection/background workers or graph server run. Fixtures are synthetic.
Screenshots are scratch QA only, never part of the source archive.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
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
from sqlmodel import Session,select
from news2douyin.server.app import create_app
from news2douyin.intake.official_models import OfficialEvent,OfficialMention
from news2douyin.storage.models import Article,Event
from news2douyin.desk.models import RadarEntry
from news2douyin.workbench.models import ContentTask


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output',type=Path)
    args=parser.parse_args();output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    os.environ['NEWS2DOUYIN_GKG_STREAM_ENABLED']='0'
    os.environ['NEWS2DOUYIN_FALKOR_URL']=''
    with tempfile.TemporaryDirectory(prefix='n2d-gdelt-browser-') as temporary:
        folder=Path(temporary)
        app=create_app(db_url='sqlite:///'+str(folder/'fixture.db'),storage_root=str(folder/'runs'))
        now=datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
        stamp=datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
        with Session(app.state.engine) as s:
            s.add(OfficialEvent(event_id='123456',event_date=now[:10],first_seen=now,last_seen=now,
                actor1_name='甲国',actor2_name='乙国',event_code='072',event_base_code='072',event_root_code='07',
                action_country='CH',action_country_iso2='cn',action_place='示例城市',initial_mentions=100,
                feed='events_english',stamp=stamp,row_index=1))
            s.add(OfficialEvent(event_id='987654',event_date=now[:10],first_seen=now,last_seen=now,
                actor1_name='丙国',actor2_name='丁国',event_code='040',event_base_code='040',event_root_code='04',
                action_country='US',action_country_iso2='us',action_place='另一示例城市',source_url='https://fallback.example/report',
                feed='events_english',stamp=stamp,row_index=2))
            s.add(OfficialMention(key='browser-mention',event_id='123456',doc_identifier='https://one.example/report',
                domain='one.example',first_seen=now,last_seen=now,confidence=85,
                first_feed='mentions_english',first_stamp=stamp,first_row_index=1,
                last_feed='mentions_english',last_stamp=stamp,last_row_index=1))
            s.commit()
        graph_calls=[]
        def forbidden(*_args,**_kwargs):
            graph_calls.append(True);raise AssertionError('Discovery HTTP contacted graph')
        app.state.desk.graph.query=app.state.desk.graph.read=app.state.desk.graph.explain=forbidden
        @app.get('/favicon.ico',include_in_schema=False)
        def favicon():return Response(status_code=204)
        @app.get('/qa/gdelt-counts')
        def counts():
            from sqlalchemy import func
            with Session(app.state.engine) as s:
                return {model.__name__:s.exec(select(func.count()).select_from(model)).one()
                        for model in (Article,Event,RadarEntry,ContentTask)}
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,lifespan='off',log_level='warning'))
        thread=threading.Thread(target=server.run,daemon=True);thread.start()
        try:
            for _ in range(100):
                try:
                    if requests.get(base+'/api/health',timeout=.2).ok:break
                except requests.RequestException:time.sleep(.05)
            else:raise RuntimeError('Disposable HTTP fixture did not start')
            env=dict(os.environ,N2D_QA_URL=base,N2D_QA_OUTPUT=str(output))
            completed=subprocess.run(['node',str(ROOT/'tools/check_gdelt_events_browser.cjs')],env=env,timeout=120)
            if completed.returncode:raise SystemExit(completed.returncode)
            assert not graph_calls
            report=json.loads((output/'gdelt-events-browser-results.json').read_text())
            report.update(real_http=True,real_chromium=True,database='disposable SQLite',graph_http_calls=0,
                background_workers=False,fixtures='Two synthetic official events and one source mention')
            (output/'gdelt-events-browser-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
            print(json.dumps(report,ensure_ascii=False))
        finally:
            server.should_exit=True;thread.join(timeout=10);app.state.engine.dispose()


if __name__=='__main__':main()
