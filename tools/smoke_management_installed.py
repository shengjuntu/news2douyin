"""Run with python -I against the installed wheel; optionally check a 0.6 DB."""
import argparse
import json
from pathlib import Path
import tempfile
import time
from importlib.metadata import version
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session, select
from news2douyin.collect import llm_filter
from news2douyin.server import webui
from news2douyin.server.app import create_app
from news2douyin.storage.models import CollectJob, DailySelection, Article, TaskRecord
from news2douyin.video.workbench import script_detail


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--upgrade-fixture', default='')
    args = parser.parse_args()
    assert tuple(int(v) for v in version('news2douyin').split('.')[:3]) >= (0,7,0)
    checks = []
    with tempfile.TemporaryDirectory(prefix='management-wheel-') as folder:
        with patch.object(llm_filter, 'endpoint_alive', return_value=False), patch.object(webui, '_llm_alive', return_value=False):
            app = create_app(db_url=f'sqlite:///{folder}/app.db', storage_root=str(Path(folder)/'runs'))
            with TestClient(app) as client:
                assert client.post('/api/profiles', json={'name':'中文策略', 'provider':'mock','extra':{'filter_mode':'rules'}}).status_code == 200
                task = client.post('/api/profiles/中文策略/test', json={}).json()
                task_id = task['task_id']
                for _ in range(200):
                    task = client.get('/api/tasks/'+task_id).json()
                    if task['status'] in {'succeeded','failed','cancelled'}:
                        break
                    time.sleep(.025)
                assert task['status'] == 'succeeded'
                diag = client.get('/api/tasks/'+task_id+'/diagnostics').json()
                assert diag['stats']['fetched'] == 2 and diag['stats']['dry_run']
                assert client.get('/api/articles?paginated=true').json()['total'] == 0
                assert client.get('/tasks/'+task_id+'/diagnostics').status_code == 200
                checks.append('installed_trial_diagnostics_no_news_writes')
                job = client.post('/api/jobs',json={'name':'计划','profile_name':'中文策略','cron_expr':'0 9 * * 1-5','timezone':'Asia/Shanghai'}).json()
                assert len(job['next_runs']) == 3
                assert client.post('/api/jobs/preview',json={'cron_expr':'0 9 * * *','timezone':'UTC'}).status_code == 200
                checks.append('installed_schedule_preview')
                client.post('/api/profiles/中文策略/disable')
                assert not client.get('/api/jobs').json()[0]['enabled']
                assert client.post('/api/profiles/中文策略/test',json={}).status_code == 409
                assert client.delete('/api/profiles/中文策略').status_code == 409
                checks.append('installed_lifecycle_guards')
                assert client.delete('/api/jobs/'+str(job['id'])).status_code == 200
                assert client.delete('/api/profiles/中文策略').status_code == 200
                assert client.get('/api/tasks/'+task_id+'/diagnostics').json()['stats']['dry_run']
                assert client.get('/profiles').status_code == 200
                checks.append('installed_templates_and_preserved_history')
            app.state.engine.dispose()
    if args.upgrade_fixture:
        root = Path(args.upgrade_fixture)
        expected = json.loads((root/'expected.json').read_text())
        assert expected['from_version'] == '0.6.0'
        app = create_app(db_url='sqlite:///'+str(root/'old.db'), storage_root=str(root/'runs'))
        client = TestClient(app)
        profile = client.get('/api/profiles/legacy_mock').json()
        assert profile['enabled'] and profile['provider'] == 'mock'
        assert client.get('/api/jobs').json()[0]['name'] == 'legacy_job'
        checks.append('upgrade_0_6_profile_and_schedule_preserved')
        with Session(app.state.engine) as s:
            pick = s.get(DailySelection, expected['selection_key'])
            assert pick.package_key == expected['package_key']
            assert s.exec(select(Article).where(Article.article_key == expected['article_key'])).first()
            assert s.get(TaskRecord, expected['task_id']).status == 'succeeded'
            detail = script_detail(s, expected['package_key'])
            assert detail['sources'][0]['article_key'] == expected['article_key']
        checks.append('upgrade_0_6_news_selection_script_and_task_preserved')
        assert '历史运行' in client.get('/api/tasks/'+expected['task_id']+'/diagnostics').json()['summary']
        assert client.get('/scripts/'+expected['package_key']).status_code == 200
        checks.append('upgrade_0_6_history_and_script_readable')
        app.state.engine.dispose()
    print(json.dumps({'status':'passed','version':version('news2douyin'),'passed':len(checks),'checks':checks}))


if __name__ == '__main__':
    main()
