"""Verify the installed wheel using local HTTP fixtures and real video checks."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
import json
import os
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session, select
from news2douyin.server.app import create_app
from news2douyin.storage.models import CollectProfile, TaskRecord


def main():
    assert tuple(map(int,version('news2douyin').split('.'))) >= (0,10,3)
    checks=[];received=[];mode=['listed']
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            received.append((self.path,self.headers.get('Authorization')))
            if mode[0]=='redirect':
                self.send_response(302);self.send_header('Location','/should-not-follow');self.end_headers();return
            code=401 if mode[0]=='auth_error' else 200
            self.send_response(code);self.send_header('Content-Type','application/json');self.end_headers()
            self.wfile.write(json.dumps({'error':'private-upstream-token'} if code==401 else {'data':[{'id':'local-fixture'}]}).encode())
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='setup-wheel-') as folder, patch.dict(os.environ,{
                'NEWS2DOUYIN_ENV_FILE':str(Path(folder)/'absent.env'),'NEWS2DOUYIN_TIMEZONE':'Asia/Shanghai',
                'OPENAI_BASE_URL':f'http://127.0.0.1:{server.server_port}/v1',
                'OPENAI_API_KEY':'private-local-key','MODEL':'local-fixture','API_KEY':''}):
            app=create_app(db_url=f'sqlite:///{folder}/app.db',storage_root=str(Path(folder)/'runs'))
            with TestClient(app) as client:
                assert client.get('/setup').status_code==200
                assert '首次运行检查' in client.get('/daily').text
                checks.append('installed_templates_and_entry_links')
                assert client.post('/api/profiles',json={'name':'demo','provider':'mock'}).status_code==200
                with Session(app.state.engine) as s:before=[x.model_dump() for x in s.exec(select(CollectProfile))]
                data=client.get('/api/setup/check').json()
                assert data['database_ok'] and data['worker_running'] and data['scheduler_running']
                assert data['storage']['permissions_ok'] and data['summary']['demo_profiles']==1
                assert received==[]
                with Session(app.state.engine) as s:
                    assert before==[x.model_dump() for x in s.exec(select(CollectProfile))]
                    assert not s.exec(select(TaskRecord)).all()
                checks.append('configuration_read_does_not_contact_model_or_mutate_data')
                response=client.post('/api/setup/model-probe');assert response.json()['state']=='listed'
                assert received==[('/v1/models','Bearer private-local-key')]
                assert 'private-local-key' not in response.text
                checks.append('explicit_models_get_uses_configured_endpoint_and_auth')
                mode[0]='redirect';received.clear()
                assert client.post('/api/setup/model-probe').json()['state']=='redirect'
                assert received==[('/v1/models','Bearer private-local-key')]
                checks.append('http_redirect_is_not_followed')
                mode[0]='auth_error'
                response=client.post('/api/setup/model-probe');assert response.json()['state']=='auth_error'
                assert 'private-upstream-token' not in response.text
                checks.append('upstream_error_body_and_credentials_are_not_exposed')
                video=client.post('/api/setup/video-check').json()
                assert video['ok'] and video['ready'],video
                assert all(c['ok'] for c in video['checks'] if c['key'] not in {'espeak','edge'})
                checks.append('actual_ffmpeg_ffprobe_pillow_and_chinese_font_checks')
            app.state.engine.dispose()
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
    print(json.dumps({'status':'passed','checks':checks}))


if __name__=='__main__':main()
