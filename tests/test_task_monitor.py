import copy
import json
import time
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.tasks import monitor
from news2douyin.tasks.service import TaskService
from news2douyin.storage.models import (TaskRecord,TaskEvent,ScriptGeneration,DailyScriptGeneration,
    VideoProduction,DailySelection)
from news2douyin.storage.articles import lock_news
from news2douyin.server.app import create_app

NOW=datetime(2026,9,30,12,tzinfo=timezone.utc).timestamp()


def row(key, **kw):
    data=dict(task_id=key,profile_name='任务 '+key,profile_json='{"api_key":"private-token","date_str":"2026-09-30","timezone":"Asia/Shanghai"}',
              request_hash='private-hash',created_at='2026-09-30T10:00:00Z',queued_at=NOW-100)
    return TaskRecord(**(data|kw))


@pytest.fixture
def populated(engine,monkeypatch):
    monkeypatch.setattr(monitor.time,'time',lambda:NOW)
    with Session(engine) as s:
        s.add_all([row('wait-a',queued_at=NOW-120),row('wait-b',queued_at=NOW-120,trigger_type='daily_script'),
          row('wait-c',queued_at=NOW-60,trigger_type='video'),
          row('live',status='running',trigger_type='script',started_at='2026-09-30T11:59:00Z',lease_until=NOW+20,lease_owner='private-owner',stage='script_writing'),
          row('expired',status='cancel_requested',started_at='2026-09-30T11:58:00Z',lease_until=NOW-1),
          row('done',status='succeeded',finished_at='2026-09-30T10:20:00Z',trigger_type='schedule'),
          row('failure',status='failed',trigger_type='profile_test',finished_at='2026-09-30T10:02:00Z'),
          row('cancelled',status='cancelled',finished_at='2026-09-30T10:03:00Z')])
        s.commit()
    return engine


def test_global_rank_before_filters_and_pagination(populated):
    page=monitor.task_page(populated,status='queued',limit=1,offset=1)
    assert page['total']==3 and page['items'][0]['task']['task_id']=='wait-b'
    assert page['items'][0]['queue_position']==2 and page['order']=='queue'
    video=monitor.task_page(populated,kind='video',query='wait-c');assert video['total']==1 and video['items'][0]['queue_position']==3
    assert monitor.task_position(populated,'wait-a')['queue_position']==1
    assert monitor.task_position(populated,'live')['queue_position'] is None


def test_counts_running_recovery_and_safe_public_fields(populated):
    page=monitor.task_page(populated,status='failed',local_worker_running=False)
    summary=page['summary'];assert summary['counts']==dict(queued=3,running=1,cancel_requested=1,succeeded=1,failed=1,cancelled=1,active=5,total=8)
    assert summary['executing_count']==2 and summary['needs_recovery_count']==1 and summary['local_worker_running'] is False
    assert len(summary['executing'])==2 and page['total']==1
    encoded=json.dumps(page);assert 'private-token' not in encoded and 'private-owner' not in encoded and 'private-hash' not in encoded
    assert monitor.task_position(populated,'expired')['timing']['needs_recovery']
    assert not monitor.task_position(populated,'live')['timing']['needs_recovery']


def test_timing_distinguishes_queue_current_attempt_and_lifetime(populated):
    waiting=monitor.task_position(populated,'wait-a')['timing'];assert waiting['queued_seconds']==120 and waiting['execution_seconds'] is None
    running=monitor.task_position(populated,'live')['timing'];assert running['execution_seconds']==60 and running['total_seconds']==7200
    done=monitor.task_position(populated,'done')['timing'];assert done['total_seconds']==1200 and done['queued_seconds'] is None and done['execution_seconds'] is None
    cancelled=monitor.timing(row('old',status='cancelled',queued_at=NOW-10,started_at='2026-09-29T10:00:00Z',finished_at='2026-09-30T10:01:00Z'),NOW)
    assert cancelled['execution_seconds'] is None and cancelled['total_seconds']==60
    malformed=monitor.timing(row('bad',created_at='broken',queued_at=0),NOW);assert malformed['total_seconds'] is None and malformed['queued_seconds'] is None
    assert monitor.timing(row('future',queued_at=NOW+10),NOW)['queued_seconds']==0


def test_cancel_and_retry_changes_queue_order_without_priority_mutation(populated):
    service=TaskService(populated);service.cancel('wait-a')
    assert monitor.task_position(populated,'wait-b')['queue_position']==1
    service.retry('wait-a')
    retry=monitor.task_position(populated,'wait-a');assert retry['queue_position']==3 and retry['timing']['queued_seconds']==0
    assert service.claim().task_id=='wait-b'
    assert monitor.task_position(populated,'wait-c')['queue_position']==1


def test_filters_kind_status_literal_query_and_more_than_one_hundred(engine):
    with Session(engine) as s:
        for i in range(125):s.add(row(f'row{i:03}',profile_name=f'普通标题 {i}',trigger_type='manual'))
        s.add(row('literal',profile_name='实际含有 %_\\ 字符',trigger_type='profile_test'))
        s.commit()
    assert monitor.task_page(engine,kind='collect',limit=25,offset=100)['total']==125
    assert len(monitor.task_page(engine,kind='collect',limit=25,offset=100)['items'])==25
    page=monitor.task_page(engine,query='%_\\');assert page['total']==1 and page['items'][0]['task']['task_id']=='literal'
    assert monitor.task_page(engine,kind='trial')['total']==1
    assert monitor.task_page(engine,query='row124')['total']==1


def test_date_timezone_boundary_before_pagination(engine):
    with Session(engine) as s:
        s.add_all([row('before',created_at='2026-09-29T15:59:59Z'),row('first',created_at='2026-09-29T16:00:00Z'),
                   row('last',created_at='2026-09-30T23:59:59+08:00'),row('after',created_at='2026-09-30T16:00:00Z')]);s.commit()
    args=dict(date_from='2026-09-30',date_to='2026-09-30',timezone='Asia/Shanghai',limit=1)
    assert monitor.task_page(engine,**args)['total']==2
    assert monitor.task_page(engine,**args,offset=1)['items'][0]['task']['task_id']=='first'
    assert monitor.task_page(engine,**(args|dict(timezone='UTC')))['total']==2


def test_monitor_does_not_recover_or_mutate_expired_tasks(populated):
    with Session(populated) as s:before=[t.model_dump() for t in s.exec(select(TaskRecord).order_by(TaskRecord.task_id))]
    monitor.task_page(populated);monitor.task_position(populated,'expired')
    with Session(populated) as s:
        assert [t.model_dump() for t in s.exec(select(TaskRecord).order_by(TaskRecord.task_id))]==before
        assert not s.exec(select(TaskEvent)).all()


def test_result_links_are_type_specific_and_do_not_read_media(populated):
    with Session(populated) as s:
        s.add(ScriptGeneration(task_id='live',event_key='topic',input_hash='x',spec_json='not read',package_key='pkg_evidence'))
        s.add(DailyScriptGeneration(task_id='wait-b',selection_key='selection',event_key='topic',input_hash='x',spec_json='not read',package_key='pkg_daily'))
        s.add(VideoProduction(task_id='wait-c',package_key='pkg_daily',revision=1,export_key='fixture-export',input_hash='x',spec_json='not read'))
        s.commit()
    assert monitor.task_position(populated,'wait-b')['result_url']=='/scripts/pkg_daily'
    assert monitor.task_position(populated,'live')['result_url']=='/scripts/pkg_evidence'
    assert monitor.task_position(populated,'wait-c')['result_url']=='/videos/wait-c'
    assert monitor.task_position(populated,'failure')['diagnostics_url']=='/tasks/failure/diagnostics'
    assert monitor.task_position(populated,'failure')['result_url'] is None
    assert 'task_id=done' in monitor.task_position(populated,'done')['result_url']
    assert monitor.task_position(populated,'live')['diagnostics_url'] is None


def test_query_and_missing_task_validation(engine):
    for kw in [dict(kind='invalid'),dict(status='invalid'),dict(query='x'*201),dict(offset=-1),dict(limit=101),
               dict(date_from='2026-10-02',date_to='2026-10-01'),dict(timezone='invalid')]:
        with pytest.raises(ValueError):monitor.task_page(engine,**kw)
    with pytest.raises(KeyError):monitor.task_position(engine,'missing')


def test_http_old_api_shape_and_new_pages(tmp_path):
    app=create_app(db_url=f'sqlite:///{tmp_path}/app.db',storage_root=str(tmp_path/'runs'));client=TestClient(app)
    with Session(app.state.engine) as s:s.add(row('one'));s.commit()
    assert isinstance(client.get('/api/tasks').json(),list)
    assert 'queue_position' not in client.get('/api/tasks/one').json()
    page=client.get('/api/task-queue?status=queued').json();assert page['total']==1 and not page['summary']['local_worker_running']
    assert client.get('/api/tasks/one/queue-status').json()['queue_position']==1
    for path in ['/api/task-queue?status=oops','/api/task-queue?limit=0','/api/task-queue?date_from=oops','/api/task-queue?query='+'x'*201]:assert client.get(path).status_code==422
    assert client.get('/api/tasks/missing/queue-status').status_code==404
    assert '任务中心' in client.get('/tasks').text and '排队与耗时' in client.get('/tasks/one').text
    app.state.engine.dispose()
