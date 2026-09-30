"""Installed 0.10.1 daily tasks and real 0.10.0 database upgrade. Run with python -I."""
import argparse
import json
import runpy
import tempfile
from importlib.metadata import version
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from sqlmodel import Session
from news2douyin.editorial import daily,daily_tasks as gen
from news2douyin.server.app import create_app
from news2douyin.storage.models import VideoProduction,VideoWork
from news2douyin.tasks.service import TaskService
from news2douyin.tasks.worker import TaskWorker
from news2douyin.video import workbench as wb


def execute(app,root):
    worker=TaskWorker(app.state.engine,root);task=worker.service.claim();assert task
    worker.execute(task);assert worker.service.get(task.task_id)['status']=='succeeded'


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--upgrade-fixture',default='');args=parser.parse_args()
    assert version('news2douyin')=='0.10.1';checks=[]
    seed=runpy.run_path(str(Path(__file__).with_name('smoke_daily_tasks.py')))['seed']
    with tempfile.TemporaryDirectory(prefix='daily-tasks-wheel-') as directory:
        root=Path(directory);url=f'sqlite:///{root}/app.db';runs=root/'runs'
        app=create_app(db_url=url,storage_root=str(runs));seed(app.state.engine);client=TestClient(app)
        for key in ['demo0','demo1']:
            picks=client.post('/api/daily/selections',json=dict(article_key=key,day='2026-09-30')).json()
        request=dict(selection_keys=[p['selection_key'] for p in picks],mode='basic')
        result=client.post('/api/daily/script-tasks',json=request);assert result.status_code==202
        ids=[r['task']['task_id'] for r in result.json()['items']]
        for path in ['/daily?day=2026-09-30','/scripts','/tasks/'+ids[0]]:assert client.get(path).status_code==200
        assert client.get('/api/daily/script-tasks/'+ids[0]).json()['source']['revision']==1
        checks.append('installed_batch_api_templates_and_frozen_source')
        app.state.engine.dispose();app=create_app(db_url=url,storage_root=str(runs));client=TestClient(app)
        for _ in ids:execute(app,runs)
        drafts=[gen.detail(app.state.engine,tid) for tid in ids]
        assert all(d['script_url'] for d in drafts)
        for d in drafts:
            with Session(app.state.engine) as s:assert wb.script_detail(s,d['package_key'])['document']['source_keys']==[d['source']['article_key']]
        checks.append('installed_restart_recovers_batch_and_source_bound_drafts')
        again=client.post('/api/daily/script-tasks',json=request);assert again.status_code==200
        assert [r['task']['task_id'] for r in again.json()['items']]==ids
        assert client.post('/api/daily/selections/'+picks[0]['selection_key']+'/build',json={}).json()['reused']
        checks.append('installed_idempotency_and_legacy_build_compatibility')
        with Session(app.state.engine) as s:third=next(p for p in daily.choose(s,'demo2',day='2026-09-30') if p['article_key']=='demo2')
        tid=gen.submit_many(app.state.engine,dict(selection_keys=[third['selection_key']]))['items'][0]['task']['task_id']
        worker=TaskWorker(app.state.engine,runs)
        with patch.object(wb,'initialize_script',side_effect=OSError('installed checkpoint fixture')):worker.execute(worker.service.claim())
        assert worker.service.get(tid)['status']=='failed'
        app.state.engine.dispose();app=create_app(db_url=url,storage_root=str(runs))
        TaskService(app.state.engine).retry(tid)
        with patch.object(daily,'draft_document',side_effect=AssertionError('must reuse saved draft')):execute(app,runs)
        assert gen.detail(app.state.engine,tid)['script_url']
        checks.append('installed_restart_retry_reuses_saved_document')
        with Session(app.state.engine) as s:last=next(p for p in daily.choose(s,'demo3',day='2026-09-30') if p['article_key']=='demo3')
        tid=gen.submit_many(app.state.engine,dict(selection_keys=[last['selection_key']]))['items'][0]['task']['task_id']
        with Session(app.state.engine) as s:daily.choose(s,'demo3',day='2026-09-30',active=False);daily.choose(s,'demo3',day='2026-09-30')
        client=TestClient(app);assert client.post('/api/tasks/'+tid+'/retry').status_code==409
        assert not gen.detail(app.state.engine,tid)['current']
        checks.append('installed_selection_removal_preserves_history_and_blocks_old_retry')
        app.state.engine.dispose()
    if args.upgrade_fixture:
        root=Path(args.upgrade_fixture);expected=json.loads((root/'expected.json').read_text());assert expected['from_version']=='0.10.0'
        app=create_app(db_url=f'sqlite:///{root}/old.db',storage_root=str(root/'runs'));client=TestClient(app)
        with Session(app.state.engine) as s:
            assert wb.script_detail(s,expected['script']['package_key'])==expected['script']
            picks=daily.list_picks(s,'2026-09-30');assert [{k:v for k,v in p.items() if k!='generation'} for p in picks]==expected['picks']
            assert s.get(VideoProduction,expected['video']).model_dump()==expected['production']
            assert s.get(VideoWork,expected['video']).model_dump()==expected['work']
        execute(app,root/'runs')
        assert client.get('/api/video/tasks/'+expected['video']+'/files/video.mp4',headers={'Range':'bytes=0-63'}).status_code==206
        with Session(app.state.engine) as s:
            assert s.get(VideoProduction,expected['video']).spec_json==expected['production']['spec_json']
            assert s.get(VideoWork,expected['video']).model_dump()==expected['work']
        checks.append('upgrade_preserves_approved_script_and_work_metadata_resumes_original_video')
        assert gen.list_generations(app.state.engine)==[]
        unbuilt=next(p for p in picks if not p['package_key'])
        result=client.post('/api/daily/selections/'+unbuilt['selection_key']+'/script-task',json={});assert result.status_code==202
        execute(app,root/'runs')
        with Session(app.state.engine) as s:
            assert all(p['package_key'] for p in daily.list_picks(s,'2026-09-30'))
            assert wb.script_detail(s,expected['script']['package_key'])==expected['script']
        checks.append('upgrade_adds_daily_tasks_without_rewriting_existing_picks_or_script')
        app.state.engine.dispose()
    print(json.dumps(dict(status='passed',version=version('news2douyin'),passed=len(checks),checks=checks)))


if __name__=='__main__':main()
