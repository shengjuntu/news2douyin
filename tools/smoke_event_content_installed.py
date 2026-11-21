"""Installed-wheel source-content journey with a synthetic local HTTP publisher.

The controlled transport replaces only the public-page fetch in this smoke;
public DNS/IP/TLS/deadline behavior has separate real transport tests. No model
or external news service is contacted. Run with python -I after wheel install.
"""
import importlib.metadata
import json
import os
from pathlib import Path
import tempfile
from threading import Thread
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch

for key in ('NEWS2DOUYIN_GKG_STREAM_ENABLED','NEWS2DOUYIN_GKG_PROJECTION_ENABLED',
            'NEWS2DOUYIN_GDELT_STREAM_ENABLED','NEWS2DOUYIN_GDELT_PROJECTION_ENABLED',
            'NEWS2DOUYIN_SOURCE_CONTENT_ENABLED'):
    os.environ[key]='0'
os.environ['NEWS2DOUYIN_FALKOR_URL']=''
from fastapi.testclient import TestClient
from sqlmodel import Session
import requests
from news2douyin.server.app import create_app
from news2douyin.storage.models import Article,ArticleIdentity,utc_now_iso
from news2douyin.storage.articles import identity_key
from news2douyin.intake.official_models import OfficialEvent
from news2douyin.selection import source_fetch
from news2douyin.selection.source_models import SourcePreview
from news2douyin.translations import service as translations

LOCAL='https://publisher.example/local';REMOTE='https://publisher.example/remote'
TITLE='Parliament approves a new transport budget'
EXCERPT='The plan includes new rail services and will be reviewed next month.'
BODY=('<html lang="en"><head><meta property="og:title" content="'+TITLE+'">'
      '<meta name="description" content="'+EXCERPT+'"></head><body><h1>'+TITLE+'</h1><p>'+EXCERPT+'</p></body></html>').encode()

class Publisher(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Content-Length',str(len(BODY)));self.end_headers();self.wfile.write(BODY)


def main():
    assert importlib.metadata.version('news2douyin')=='0.48.0'
    publisher=ThreadingHTTPServer(('127.0.0.1',0),Publisher)
    thread=Thread(target=publisher.serve_forever,daemon=True);thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='source-content-wheel-') as temporary:
            root=Path(temporary);app=create_app(db_url='sqlite:///'+str(root/'app.db'),storage_root=str(root/'runs'))
            engine=app.state.engine;client=TestClient(app)
            try:
                with Session(engine) as s:
                    s.add(Article(article_key='local',title='Local reporters explain the budget',content='The council will debate rail funding.',url=LOCAL,language='en'))
                    s.add(ArticleIdentity(identity_key=identity_key({'url':LOCAL}),article_key='local'))
                    for key,url in (('30001',LOCAL),('30002',REMOTE)):
                        s.add(OfficialEvent(event_id=key,event_date='2026-11-14',last_seen=utc_now_iso(),
                            actor1_name='ALPHA',actor2_name='BETA',event_root_code='04',event_code='043',
                            source_url=url,feed='events_english',stamp='20261114000000',row_index=1))
                    s.commit()
                initial=client.get('/radar?source=gdelt&period=all');assert initial.status_code==200
                assert 'Local reporters explain the budget' in initial.text and 'The council will debate rail funding.' in initial.text
                assert '报道内容待补充' in initial.text
                assert client.post('/api/gdelt/content/enrich',json={'event_ids':['30002']},headers={'Origin':'https://foreign.example'}).status_code==403
                app.state.source_content.enabled=True
                queued=client.post('/api/gdelt/content/enrich',json={'event_ids':['30002']})
                assert queued.status_code==202,queued.text
                calls=[]
                def fetch(url,**kwargs):
                    assert url==REMOTE and kwargs==dict(timeout_seconds=35,max_bytes=512*1024)
                    assert engine.pool.checkedout()==0,'Network held a transaction connection'
                    response=requests.get('http://127.0.0.1:'+str(publisher.server_port)+'/story',timeout=5)
                    response.raise_for_status();calls.append(url)
                    return url,response.headers['Content-Type'],response.content
                with patch.object(source_fetch,'safe_fetch',fetch):
                    assert app.state.source_content.step()
                assert calls==[REMOTE]
                with patch.object(source_fetch,'fetch_preview',side_effect=AssertionError('GET triggered source fetch')):
                    card=client.get('/api/gdelt/content/cards?event_ids=30002').json()['items'][0]
                    detail=client.get('/api/gdelt/events/30002').json()
                    assert client.get('/api/gdelt/content/status').status_code==200
                assert card['display_title']==TITLE and card['excerpt']==EXCERPT
                assert card['coded_title']!=TITLE and card['report_preview']['translation']['kind']=='source_preview'
                preview=detail['event']['report_preview']
                request=client.post('/api/translations',json=preview['translation'])
                assert request.status_code==200,request.text
                translated=request.json();assert TITLE in translated['original'] and EXCERPT in translated['original']
                assert '主体：' not in translated['original']
                with patch.object(translations,'translate',lambda original,target:'议会批准交通预算。'):
                    task=app.state.worker.service.claim();assert task is not None
                    app.state.worker.execute(task)
                assert client.get('/api/translations/'+translated['id']).json()['status']=='succeeded'
                selected=client.post('/api/gdelt/events/30002/select',json={'request_id':'installed-source-content'})
                assert selected.status_code==200,selected.text
                work=client.get('/api/topics/'+selected.json()['key']).json()
                assert work['title']==TITLE and not work['materials']['can_write_from_sources']
                frozen=json.dumps(work['sources'],sort_keys=True)
                with Session(engine) as s:
                    row=s.get(SourcePreview,identity_key({'url':REMOTE}));row.title='A later changed headline';s.add(row);s.commit()
                assert json.dumps(client.get('/api/topics/'+selected.json()['key']).json()['sources'],sort_keys=True)==frozen
                assert client.get(selected.json()['url']).status_code==200
            finally:client.close();engine.dispose()
    finally:publisher.shutdown();publisher.server_close();thread.join(timeout=2)
    print(json.dumps(dict(status='passed',version='0.48.0',checks=[
        'packaged_source_modules_and_ui','local_identity_headline_and_excerpt','missing_content_explicit',
        'same_origin_enrich','durable_background_source_worker','real_local_http_preview_extraction',
        'network_without_sql_connection','read_only_content_poll','real_report_translation_input',
        'translation_worker','real_title_selection','frozen_lead_materials'],
        production_accessed=False,remote_model_contacted=False,fixture='synthetic local HTTP with controlled source transport')))


if __name__=='__main__':main()
