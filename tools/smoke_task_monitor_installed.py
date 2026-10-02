"""Installed task monitor plus a real 0.10.1 database continuation."""
import argparse
import json
import tempfile
import time
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlmodel import Session,select
from news2douyin.server.app import create_app
from news2douyin.storage.models import TaskRecord,TaskEvent,DailyScriptGeneration
from news2douyin.tasks.worker import TaskWorker
from news2douyin.tasks.service import TaskService
from news2douyin.editorial import daily,daily_tasks
from news2douyin.collect import llm_filter,service as collect
from news2douyin.video import workbench as wb


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--upgrade-fixture',default='');args=parser.parse_args()
    assert version('news2douyin')=='0.10.2';checks=[]
    with tempfile.TemporaryDirectory(prefix='monitor-wheel-') as directory:
        root=Path(directory);app=create_app(db_url=f'sqlite:///{root}/app.db',storage_root=str(root/'runs'));client=TestClient(app)
        with Session(app.state.engine) as s:
            for i in range(110):s.add(TaskRecord(task_id=f't{i:03}',profile_name=f'安装验证{i}',profile_json='{}',request_hash='private',queued_at=time.time()-110+i,trigger_type='video' if i%2 else 'manual'))
            s.commit()
        assert isinstance(client.get('/api/tasks').json(),list)
        data=client.get('/api/task-queue?status=queued&kind=video&offset=50&limit=5').json()
        assert data['total']==55 and len(data['items'])==5 and data['items'][-1]['queue_position']==110
        checks.append('installed_full_queue_rank_with_filtered_pagination')
        assert client.get('/api/tasks/t005/queue-status').json()['queue_position']==6
        assert client.get('/api/task-queue?date_from=bad').status_code==422
        assert client.get('/api/task-queue?query=t109').json()['total']==1
        for path in ['/tasks','/tasks/t005']:
            response=client.get(path);assert response.status_code==200 and 'N2DTasks' in response.text
        checks.append('installed_history_queries_detail_api_and_shared_template')
        with Session(app.state.engine) as s:before=[t.model_dump() for t in s.exec(select(TaskRecord).order_by(TaskRecord.task_id))]
        client.get('/api/task-queue');client.get('/api/tasks/t005/queue-status')
        with Session(app.state.engine) as s:assert before==[t.model_dump() for t in s.exec(select(TaskRecord).order_by(TaskRecord.task_id))]
        checks.append('installed_monitor_preserves_task_records_and_old_api_shape')
        client.post('/api/tasks/t000/cancel');client.post('/api/tasks/t000/retry')
        assert client.get('/api/tasks/t001/queue-status').json()['queue_position']==1
        assert client.get('/api/tasks/t000/queue-status').json()['queue_position']==110
        assert client.get('/api/task-queue').json()['summary']['local_worker_running'] is False
        checks.append('installed_actions_reflect_real_queue_time_without_reordering')
        app.state.engine.dispose()
    if args.upgrade_fixture:
        root=Path(args.upgrade_fixture);expected=json.loads((root/'expected.json').read_text());assert expected['from_version']=='0.10.1'
        app=create_app(db_url=f'sqlite:///{root}/old.db',storage_root=str(root/'runs'));client=TestClient(app)
        assert client.get('/api/task-queue').json()['summary']['counts']['total']==3
        assert client.get('/api/tasks/'+expected['queued']+'/queue-status').json()['queue_position']==1
        with Session(app.state.engine) as s:
            assert expected['tasks']==[r.model_dump() for r in s.exec(select(TaskRecord).order_by(TaskRecord.task_id))]
            assert expected['events']==[r.model_dump() for r in s.exec(select(TaskEvent).order_by(TaskEvent.id))]
            assert expected['generations']==[r.model_dump() for r in s.exec(select(DailyScriptGeneration).order_by(DailyScriptGeneration.task_id))]
            assert wb.script_detail(s,expected['script']['package_key'])==expected['script']
        checks.append('upgrade_readonly_monitor_preserves_original_tasks_events_inputs_and_draft')
        assert client.post('/api/tasks/'+expected['failed']+'/retry').status_code==202
        assert client.get('/api/tasks/'+expected['failed']+'/queue-status').json()['queue_position']==2
        worker=TaskWorker(app.state.engine,root/'runs')
        with patch.dict(collect.PROVIDERS,{'mock':lambda _:[]}),patch.object(llm_filter,'endpoint_alive',return_value=False):worker.execute(worker.service.claim())
        with patch.object(daily,'draft_document',side_effect=AssertionError('must resume saved draft')):worker.execute(worker.service.claim())
        assert worker.service.get(expected['queued'])['status']=='succeeded'
        assert worker.service.get(expected['failed'])['status']=='succeeded'
        result=client.get('/api/tasks/'+expected['failed']+'/queue-status').json();assert result['result_url'].startswith('/scripts/') and result['queue_position'] is None
        with Session(app.state.engine) as s:assert wb.script_detail(s,expected['script']['package_key'])==expected['script']
        checks.append('upgrade_old_queue_runs_and_failed_daily_task_resumes_cached_document')
        app.state.engine.dispose()
    print(json.dumps(dict(status='passed',version=version('news2douyin'),passed=len(checks),checks=checks)))


if __name__=='__main__':main()
