"""Run with python -I after installing the wheel, outside the source tree."""
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
from fastapi.testclient import TestClient
from news2douyin.collect import llm_filter
from news2douyin.server import webui
from news2douyin.server.app import create_app


def main():
    with tempfile.TemporaryDirectory(prefix='daily-wheel-') as folder:
        with patch.object(llm_filter, 'endpoint_alive', return_value=False), patch.object(webui, '_llm_alive', return_value=False):
            app = create_app(db_url=f'sqlite:///{folder}/app.db', storage_root=str(Path(folder)/'runs'))
            with TestClient(app) as client:
                for path in ['/', '/daily', '/profiles', '/admin']:
                    assert client.get(path).status_code == 200
                assert client.post('/api/profiles', json={'name':'demo', 'provider':'mock'}).status_code == 200
                assert client.post('/api/collect/run-now', json={'profile_name':'demo'}).status_code == 200
                news = client.get('/api/articles?time_field=collected&period=today&paginated=true').json()
                assert news['total'] == 2
                pick = client.post('/api/daily/selections', json={'article_key':news['items'][0]['article_key']}).json()[0]
                generated = client.post('/api/daily/selections/'+pick['selection_key']+'/build', json={'mode':'basic'})
                assert generated.status_code == 200
                key = generated.json()['package_key']
                assert client.get('/scripts/'+key).status_code == 200
                assert client.get('/daily').status_code == 200
                assert client.get('/articles?period=today').status_code == 200
                detail = client.get('/api/scripts/'+key).json()
                assert detail['document']['visual_notes']
                assert detail['sources'][0]['article_key'] == news['items'][0]['article_key']
                assert client.get('/api/daily/selections').json()[0]['package_key'] == key
        app.state.engine.dispose()
    print(json.dumps({'status':'passed','checks':['daily_templates','profile_management_api','collect_date_query','daily_selection','source_bound_draft','saved_pick_package']}))


if __name__ == '__main__':
    main()
