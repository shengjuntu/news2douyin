"""Installed-wheel smoke with a real local NewsNow-shaped HTTP fixture.

Run with python -I after installing the wheel. No production or remote news
service is contacted, and the temporary database and raw directory are removed.
"""
import importlib.metadata
import json
import os
from pathlib import Path
import tempfile
from threading import Thread
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from urllib.parse import parse_qs,urlsplit

for key in ('NEWS2DOUYIN_GKG_STREAM_ENABLED','NEWS2DOUYIN_GKG_PROJECTION_ENABLED',
            'NEWS2DOUYIN_GDELT_STREAM_ENABLED','NEWS2DOUYIN_GDELT_PROJECTION_ENABLED'):
    os.environ[key]='0'
os.environ['NEWS2DOUYIN_FALKOR_URL']=''
os.environ['NEWS2DOUYIN_SOURCE_CONTENT_ENABLED']='0'

from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlmodel import Session,select
from news2douyin.server.app import create_app
from news2douyin.storage.models import Article,ArticleVersion,Event,TaskCheckpoint


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*_):pass
    def do_GET(self):
        source=parse_qs(urlsplit(self.path).query).get('id',[''])[0]
        if urlsplit(self.path).path!='/api/s' or source not in ('zhihu','cls'):
            self.send_response(404);self.end_headers();return
        item=dict(id='saved-'+source,title='中文热点线索 '+source,
                  url='https://fixture.example/'+source,extra={'hover':'这是来源摘要，尚未取得原文。'})
        if source=='cls':item['pubDate']=1794638758000
        body=json.dumps(dict(status='cache',id=source,updatedTime=1794638758000,items=[item]),ensure_ascii=False).encode()
        self.send_response(200);self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)


def main():
    assert importlib.metadata.version('news2douyin')=='0.59.0'
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='n2d-newsnow-wheel-') as temporary:
            root=Path(temporary)
            app=create_app(db_url='sqlite:///'+str(root/'app.db'),storage_root=str(root/'runs'))
            client=TestClient(app)  # No lifespan: only execute our claimed tasks.
            try:
                profile=dict(name='wheel-newsnow',provider='newsnow',extra=dict(
                    base_url='http://127.0.0.1:'+str(server.server_port),newsnow_sources=['zhihu','cls']))
                response=client.post('/api/profiles',json=profile)
                assert response.status_code==200,response.text
                assert 'NewsNow' in client.get('/profiles').text
                for _ in range(2):
                    queued=client.post('/api/tasks/collect',json={'profile_name':'wheel-newsnow'})
                    assert queued.status_code==202,queued.text
                    task=app.state.worker.service.claim(30);assert task is not None
                    app.state.worker.execute(task)
                    assert client.get('/api/tasks/'+task.task_id).json()['status']=='succeeded'
                with Session(app.state.engine) as session:
                    assert session.exec(select(func.count()).select_from(Article)).one()==2
                    assert session.exec(select(func.count()).select_from(ArticleVersion)).one()==2
                    assert session.exec(select(func.count()).select_from(Event)).one()==0
                    row=session.exec(select(Article).where(Article.url=='https://fixture.example/zhihu')).one()
                    assert row.published_at==''
                    detail=client.get('/articles/'+row.article_key)
                    assert detail.status_code==200 and '未取得原文' in detail.text
                    checkpoints=session.exec(select(TaskCheckpoint).where(TaskCheckpoint.checkpoint_key.endswith(':raw'))).all()
                    assert len(checkpoints)==2
                    assert all(json.loads(row.payload_json)['schema']=='newsnow_stage_v1' for row in checkpoints)
                assert len(list((root/'runs/newsnow/snapshots').glob('*.json')))==2
                assert len(list((root/'runs').glob('*/run_*/newsnow-snapshots.json')))==2
                from news2douyin.attention.models import AttentionInbox,AttentionTopic
                for _ in range(30):
                    if not app.state.attention.step():break
                else:raise AssertionError('Attention queue did not drain')
                with Session(app.state.engine) as session:
                    assert {row.state for row in session.exec(select(AttentionInbox))}=={'done'}
                    assert session.exec(select(AttentionTopic.key).limit(1)).first()
                page=client.get('/radar?source=attention&period=all')
                assert page.status_code==200 and '中文平台关注' in page.text
                assert client.get('/research-assets/attention.js').status_code==200
                items=client.get('/api/attention/topics?period=all').json()['items']
                assert items and all(row['rise_score'] in (None,0) for row in items)
                topic=items[0]['key']
                made=client.post('/api/attention/topics/'+topic+'/select',json=dict(request_id='wheel-attention-selection'))
                assert made.status_code==200,made.text
                assert client.get(made.json()['url']).status_code==200
                sources=client.get('/api/topics/'+made.json()['key']).json()['sources']
                assert sources and all(row['requires_original_text'] and row['attention_topic']==topic for row in sources)
            finally:client.close();app.state.engine.dispose()
    finally:server.shutdown();server.server_close();thread.join(timeout=3)
    print(json.dumps(dict(status='passed',version='0.59.0',checks=[
        'packaged_provider_and_templates','real_local_http_json','configured_sources',
        'durable_collection_worker','article_identity_and_version_dedup','metadata_without_event',
        'unknown_publication_time','content_addressed_snapshots','small_frozen_checkpoints',
        'durable_attention_inbox_and_worker','packaged_attention_ui','cached_no_fake_rise',
        'attention_selection_to_workbench','frozen_original_text_required'],
        production_accessed=False)))


if __name__=='__main__':main()
