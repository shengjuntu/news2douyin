import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlmodel import Session, select

from news2douyin.collect import service as collect, llm_filter
from news2douyin.server.app import create_app
from news2douyin.storage.models import (Article, ArticleEventLink, RunRecord, TaskRecord,
                                      TaskCheckpoint, TaskItem, ScheduleOccurrence)
from news2douyin.tasks.control import TaskCancelled, TaskConflict, TaskLeaseLost
from news2douyin.tasks.service import TaskContext, TaskService, TERMINAL
from news2douyin.tasks.worker import TaskWorker


@pytest.fixture
def tasks(engine):
    with Session(engine) as s:
        collect.create_or_update_profile(s, {'name': 'mock', 'provider': 'mock'})
    return TaskService(engine)


def expire(engine, task_id):
    with Session(engine) as s:
        s.exec(update(TaskRecord).where(TaskRecord.task_id == task_id).values(lease_until=time.time() - 1))
        s.commit()


def wait_terminal(tasks, task_id, timeout=6):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = tasks.get(task_id)
        if task['status'] in TERMINAL:
            return task
        time.sleep(0.025)
    pytest.fail(f'task did not terminate: {tasks.get(task_id)}')


def test_idempotent_submission_and_conflict(tasks, engine):
    first = tasks.submit('mock', idempotency_key='request')
    assert tasks.submit('mock', idempotency_key='request')['task_id'] == first['task_id']
    with pytest.raises(TaskConflict):
        tasks.submit('mock', {'country': 'cn'}, idempotency_key='request')
    assert len(tasks.list()) == 1
    assert len(tasks.events(first['task_id'])['items']) == 1
    assert 'profile_json' not in first and 'lease_owner' not in first


def test_concurrent_idempotency(tasks):
    with ThreadPoolExecutor(2) as pool:
        submissions = list(pool.map(lambda _: tasks.submit('mock', idempotency_key='same'), range(2)))
    assert submissions[0]['task_id'] == submissions[1]['task_id']
    assert len(tasks.list()) == 1


def test_snapshot_does_not_change_with_profile(tasks, engine, tmp_path):
    task = tasks.submit('mock')
    with Session(engine) as s:
        collect.create_or_update_profile(s, {'name': 'mock', 'provider': 'invalid'})
    TaskWorker(engine, tmp_path).execute(tasks.claim())
    assert tasks.get(task['task_id'])['status'] == 'succeeded'


def test_concurrent_claim_single_owner(tasks):
    tasks.submit('mock')
    barrier = threading.Barrier(2)
    def claim(_):
        barrier.wait(timeout=3)
        return tasks.claim()
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(claim, range(2)))
    assert sum(t is not None for t in claims) == 1


def test_queued_cancel_and_retry(tasks, engine, tmp_path):
    task = tasks.submit('mock')
    assert tasks.cancel(task['task_id'])['status'] == 'cancelled'
    assert tasks.claim() is None
    assert tasks.cancel(task['task_id'])['status'] == 'cancelled'
    assert tasks.retry(task['task_id'])['status'] == 'queued'
    with pytest.raises(TaskConflict):
        tasks.retry(task['task_id'])
    TaskWorker(engine, tmp_path).execute(tasks.claim())
    assert tasks.get(task['task_id'])['status'] == 'succeeded'
    assert tasks.cancel(task['task_id'])['status'] == 'succeeded'


def test_running_cancel_stops_before_filter_and_store(tasks, engine, tmp_path, monkeypatch):
    task = tasks.submit('mock')
    def fetch(_):
        assert tasks.cancel(task['task_id'])['status'] == 'cancel_requested'
        return []
    monkeypatch.setitem(collect.PROVIDERS, 'mock', fetch)
    filt = Mock(side_effect=AssertionError('filter must not run'))
    monkeypatch.setattr(collect, 'llm_filter_items', filt)
    TaskWorker(engine, tmp_path).execute(tasks.claim())
    assert tasks.get(task['task_id'])['status'] == 'cancelled'
    with Session(engine) as s:
        assert s.exec(select(RunRecord)).one().status == 'cancelled'
        assert not list(s.exec(select(TaskCheckpoint)))
    filt.assert_not_called()


def test_provider_error_retry_is_explicit(tasks, engine, tmp_path, monkeypatch):
    task = tasks.submit('mock')
    original = collect.PROVIDERS['mock']
    monkeypatch.setitem(collect.PROVIDERS, 'mock', Mock(side_effect=RuntimeError('offline')))
    worker = TaskWorker(engine, tmp_path)
    worker.execute(tasks.claim())
    failed = tasks.get(task['task_id'])
    assert failed['status'] == 'failed' and 'offline' in failed['error_text']
    assert tasks.claim() is None
    tasks.retry(task['task_id'])
    monkeypatch.setitem(collect.PROVIDERS, 'mock', original)
    worker.execute(tasks.claim())
    assert tasks.get(task['task_id'])['status'] == 'succeeded'


def test_expired_owner_cannot_write_or_renew(tasks, engine):
    task = tasks.submit('mock')
    old = tasks.claim()
    expire(engine, task['task_id'])
    assert not tasks.renew(task['task_id'], old.lease_owner, 30)
    tasks.recover()
    new = tasks.claim()
    assert old.lease_owner != new.lease_owner and new.attempts == 2
    context = TaskContext(tasks, old, threading.Event())
    with Session(engine) as s, pytest.raises(TaskLeaseLost):
        context.fence(s)
    with pytest.raises(TaskLeaseLost):
        context.terminate('failed', 'stale')
    assert tasks.get(task['task_id'])['status'] == 'running'


@pytest.mark.parametrize('cancel,expected', [(False, 'failed'), (True, 'cancelled')])
def test_expiration_attempt_limit_and_cancellation(tasks, engine, cancel, expected):
    task = tasks.submit('mock', max_attempts=1)
    tasks.claim()
    if cancel:
        tasks.cancel(task['task_id'])
    expire(engine, task['task_id'])
    tasks.recover()
    assert tasks.get(task['task_id'])['status'] == expected
    assert tasks.claim() is None


def test_resume_reuses_checkpoints_and_atomic_item_ledger(tasks, engine, tmp_path, monkeypatch):
    fetcher = Mock(wraps=collect.PROVIDERS['mock'])
    filterer = Mock(wraps=collect.llm_filter_items)
    monkeypatch.setitem(collect.PROVIDERS, 'mock', fetcher)
    monkeypatch.setattr(collect, 'llm_filter_items', filterer)
    task = tasks.submit('mock')
    claim = tasks.claim()
    context = TaskContext(tasks, claim, threading.Event())
    normal_progress = context.progress
    def crash(stage, current=0, total=0):
        if stage == 'storing' and current == 1:
            expire(engine, task['task_id'])
            raise TaskLeaseLost('simulated crash after first article commit')
        normal_progress(stage, current, total)
    context.progress = crash
    with Session(engine) as s, pytest.raises(TaskLeaseLost):
        collect.run_collection(s, 'mock', storage_root=tmp_path, control=context,
                               profile_snapshot=json.loads(claim.profile_json))
    with Session(engine) as s:
        assert len(list(s.exec(select(Article)))) == 1
        assert len(list(s.exec(select(TaskItem)))) == 1
    tasks.recover()
    TaskWorker(engine, tmp_path).execute(tasks.claim())
    assert tasks.get(task['task_id'])['status'] == 'succeeded'
    assert fetcher.call_count == filterer.call_count == 1
    with Session(engine) as s:
        runs = list(s.exec(select(RunRecord).order_by(RunRecord.id)))
        assert [r.status for r in runs] == ['interrupted', 'succeeded']
        assert len(list(s.exec(select(Article)))) == 2
        assert len(list(s.exec(select(ArticleEventLink)))) == 2
        stats = json.loads(runs[-1].stats_json)
        assert stats['stored_articles'] == stats['events_delta'] == 2
        assert stats['skipped_existing'] == 0
        assert len((Path(runs[-1].storage_path) / 'articles.jsonl').read_text().splitlines()) == 2


def test_heartbeat_keeps_blocking_provider_lease(tasks, engine, tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    def fetch(_):
        started.set()
        assert release.wait(5)
        return []
    monkeypatch.setitem(collect.PROVIDERS, 'mock', fetch)
    task = tasks.submit('mock')
    worker = TaskWorker(engine, tmp_path, lease_seconds=0.9, poll_seconds=0.02)
    worker.start()
    try:
        assert started.wait(3)
        time.sleep(1.2)  # Longer than original lease, but heartbeat should renew.
        assert tasks.recover() == 0
        assert tasks.get(task['task_id'])['attempts'] == 1
        release.set()
        assert wait_terminal(tasks, task['task_id'])['status'] == 'succeeded'
    finally:
        release.set()
        worker.stop()


def test_shutdown_requeues_at_safe_point(tasks, engine, tmp_path, monkeypatch):
    worker = TaskWorker(engine, tmp_path)
    task = tasks.submit('mock')
    def fetch(_):
        worker._stop.set()
        return []
    monkeypatch.setitem(collect.PROVIDERS, 'mock', fetch)
    worker.execute(tasks.claim())
    assert tasks.get(task['task_id'])['status'] == 'queued'
    with Session(engine) as s:
        assert s.exec(select(RunRecord)).one().status == 'interrupted'


def test_llm_cancellation_does_not_become_rule_fallback(article, monkeypatch):
    monkeypatch.setattr(llm_filter, 'endpoint_alive', lambda: True)
    monkeypatch.setattr(llm_filter, '_llm_chat', Mock(side_effect=TaskCancelled('cancel')))
    with pytest.raises(TaskCancelled):
        llm_filter.llm_filter_items([article], {}, check_cancel=lambda: None)


def test_api_async_sse_replay_legacy_and_webui(tmp_path, monkeypatch):
    app = create_app(db_url=f'sqlite:///{tmp_path}/app.db', storage_root=str(tmp_path / 'runs'))
    entered, release = threading.Event(), threading.Event()
    original = collect.PROVIDERS['mock']
    def fetch(profile):
        entered.set()
        assert release.wait(5)
        return original(profile)
    monkeypatch.setitem(collect.PROVIDERS, 'mock', fetch)
    with TestClient(app) as c:
        c.post('/api/profiles', json={'name': 'mock', 'provider': 'mock'})
        response = c.post('/api/tasks/collect', json={'profile_name': 'mock', 'idempotency_key': 'api'})
        assert response.status_code == 202
        task_id = response.json()['task_id']
        assert entered.wait(3) and not release.is_set()
        assert c.get('/tasks/' + task_id).status_code == 200
        assert '后台任务' in c.get('/tasks').text
        assert c.post('/api/tasks/collect', json={'profile_name': 'mock', 'idempotency_key': 'api', 'override': {'country': 'cn'}}).status_code == 409
        release.set()
        task = wait_terminal(app.state.tasks, task_id)
        assert task['status'] == 'succeeded'
        events = c.get(f'/api/tasks/{task_id}/events', params={'limit': 2}).json()
        assert events['has_more'] and len(events['items']) == 2
        replay = c.get(f'/api/tasks/{task_id}/stream', headers={'Last-Event-ID': str(events['next_cursor'])})
        assert replay.status_code == 200 and 'event: end' in replay.text
        ids = [int(line[4:]) for line in replay.text.splitlines() if line.startswith('id: ')]
        assert ids and all(i > events['next_cursor'] for i in ids)
        assert 'succeeded' in replay.text
        assert c.get(f'/api/tasks/{task_id}/stream', headers={'Last-Event-ID': 'invalid'}).status_code == 400
        assert c.get('/api/tasks/missing').status_code == 404
        legacy = c.post('/api/collect/run-now', json={'profile_name': 'mock'})
        assert legacy.status_code == 200 and legacy.json()['stats']['fetched'] == 2
        assert c.post('/api/collect/run-now?wait=false', json={'profile_name': 'mock'}).status_code == 202


def test_api_timeout_returns_durable_task_id(tmp_path, monkeypatch):
    app = create_app(db_url=f'sqlite:///{tmp_path}/app.db', storage_root=str(tmp_path / 'runs'))
    release = threading.Event()
    monkeypatch.setitem(collect.PROVIDERS, 'mock', lambda _: release.wait(4) and [])
    with TestClient(app) as c:
        c.post('/api/profiles', json={'name': 'mock', 'provider': 'mock'})
        response = c.post('/api/collect/run-now?timeout=0.01', json={'profile_name': 'mock'})
        assert response.status_code == 504
        task_id = response.json()['detail']['task_id']
        release.set()
        assert wait_terminal(app.state.tasks, task_id)['status'] == 'succeeded'


def test_task_events_are_scoped_and_paginated(tasks):
    a, b = tasks.submit('mock'), tasks.submit('mock')
    tasks.cancel(a['task_id'])
    page = tasks.events(a['task_id'], limit=1)
    tail = tasks.events(a['task_id'], page['next_cursor'])
    assert page['has_more'] and not tail['has_more']
    assert [e['type'] for e in page['items'] + tail['items']] == ['queued', 'cancelled']
    assert all(e['task']['task_id'] != b['task_id'] for e in page['items'] + tail['items'])


def test_sdk_run_now_preserves_run_result_and_timeout():
    from news2douyin.client_sdk.api import Client
    client = Client()
    client.submit_collection = Mock(return_value={'task_id': 't'})
    client.get_task = Mock(return_value={'task_id': 't', 'status': 'succeeded', 'run_id': 42})
    client.get_run = Mock(return_value={'id': 42, 'status': 'succeeded'})
    assert client.run_now('mock')['id'] == 42
    client.get_task = Mock(return_value={'task_id': 't', 'status': 'running'})
    with pytest.raises(TimeoutError, match='t continues'):
        client.wait_task('t', timeout=0.01, poll_interval=0.005)


def test_failed_item_rolls_back_ledger_and_event(tasks, engine, tmp_path, monkeypatch):
    def failure(session, item, event_key):
        raise RuntimeError('event failure')
    monkeypatch.setattr(collect, '_upsert_event', failure)
    task = tasks.submit('mock')
    TaskWorker(engine, tmp_path).execute(tasks.claim())
    assert tasks.get(task['task_id'])['status'] == 'failed'
    with Session(engine) as s:
        assert not list(s.exec(select(Article)))
        assert not list(s.exec(select(ArticleEventLink)))
        assert not list(s.exec(select(TaskItem)))
        run = s.exec(select(RunRecord)).one()
        assert run.status == 'failed'
        assert json.loads((Path(run.storage_path) / 'meta.json').read_text())['status'] == 'failed'


def test_scheduled_occurrence_and_task_commit_atomically(tasks, engine, monkeypatch):
    from news2douyin.storage.models import CollectJob
    from news2douyin.tasks import service as task_service
    with Session(engine) as s:
        job = CollectJob(name='scheduled', profile_name='mock')
        s.add(job)
        s.commit()
        scheduled = {'job_id': job.id, 'occurrence_key': 'job:minute', 'scheduled_at': '2026-09-29T00:00:00Z'}
    original = task_service.update_schedule
    def fail_after_flush(s, task):
        s.flush()
        raise RuntimeError('database failure before commit')
    monkeypatch.setattr(task_service, 'update_schedule', fail_after_flush)
    with pytest.raises(RuntimeError):
        tasks.submit('mock', scheduled=scheduled)
    with Session(engine) as s:
        assert not list(s.exec(select(TaskRecord)))
        assert not list(s.exec(select(ScheduleOccurrence)))
    monkeypatch.setattr(task_service, 'update_schedule', original)
    assert tasks.submit('mock', scheduled=scheduled)['status'] == 'queued'


def test_scheduled_task_updates_occurrence_and_job(tasks, engine, tmp_path):
    from news2douyin.storage.models import CollectJob
    with Session(engine) as s:
        job = CollectJob(name='scheduled', profile_name='mock')
        s.add(job)
        s.commit()
        job_id = job.id
    task = tasks.submit('mock', scheduled={'job_id': job_id, 'occurrence_key': 'j:m', 'scheduled_at': 'now'})
    TaskWorker(engine, tmp_path).execute(tasks.claim())
    with Session(engine) as s:
        occurrence = s.get(ScheduleOccurrence, 'j:m')
        assert occurrence.status == 'succeeded'
        assert occurrence.run_id == tasks.get(task['task_id'])['run_id']
        assert s.get(CollectJob, job_id).last_status == 'succeeded'
