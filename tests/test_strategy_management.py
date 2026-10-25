"""Strategy lifecycle and trial/schedule regressions, without external services."""
import json
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.collect import service as collect, llm_filter
from news2douyin.collect.diagnostics import task_diagnostics, summary
from news2douyin.collect.management import (save_job, delete_job, delete_profile,
    set_profile_enabled, set_job_enabled, profile_enabled)
from news2douyin.scheduler.cron import cron_matches, next_occurrences, parse_cron
from news2douyin.server.app import create_app
from news2douyin.storage.db import init_db
from news2douyin.storage.models import (Article, Event, CollectJob, CollectProfile,
    CollectedObservation, TaskItem, TaskRecord, RunRecord, ScheduleOccurrence)
from news2douyin.tasks.control import TaskConflict
from news2douyin.tasks.service import TaskService
from news2douyin.tasks.worker import TaskWorker


@pytest.fixture
def strategy(engine):
    with Session(engine) as s:
        collect.create_or_update_profile(s, {'name': '中文策略', 'provider': 'mock', 'filter_mode': 'rules'})
    return '中文策略'


def test_trial_is_frozen_and_never_writes_news(engine, strategy, tmp_path, monkeypatch, article):
    fetch = Mock(return_value=[article, dict(article, title='sports', url='https://example.com/b', content='tennis')])
    monkeypatch.setitem(collect.PROVIDERS, 'mock', fetch)
    with Session(engine) as s:
        collect.create_or_update_profile(s, {'name': strategy, 'provider': 'mock', 'keywords_include': ['chip'], 'filter_mode': 'rules'})
    service = TaskService(engine)
    task = service.submit(strategy, trial=True, idempotency_key='trial')
    assert service.submit(strategy, trial=True, idempotency_key='trial')['task_id'] == task['task_id']
    with pytest.raises(TaskConflict):
        service.submit(strategy, idempotency_key='trial')
    with Session(engine) as s:
        collect.create_or_update_profile(s, {'name': strategy, 'provider': 'mock', 'keywords_include': ['sports'], 'filter_mode': 'rules'})
        set_profile_enabled(s, strategy, False)
    TaskWorker(engine, tmp_path).execute(service.claim())
    assert service.get(task['task_id'])['status'] == 'succeeded'
    assert fetch.call_args.args[0]['keywords_include'] == ['chip']
    with Session(engine) as s:
        report = task_diagnostics(s, task['task_id'])
        assert report['stats']['after_filter'] == 1
        assert report['stats']['diagnostics']['counts'] == {'kept_rule': 1, 'not_relevant_rule': 1}
        assert report['stats']['dry_run'] and report['task']['kind'] == 'trial'
        assert '没有写入新闻库' in report['summary']
        for model in [Article, Event, CollectedObservation, TaskItem]:
            assert not list(s.exec(select(model)))
        run = s.get(RunRecord, report['task']['run_id'])
        from pathlib import Path
        assert (Path(run.storage_path) / 'diagnostics.json').exists()
        assert not (Path(run.storage_path) / 'articles.jsonl').exists()
        delete_profile(s, strategy)
        assert task_diagnostics(s, task['task_id'])['profile']['keywords_include'] == ['chip']


def test_trial_retry_reuses_filter_checkpoint(engine, strategy, tmp_path, monkeypatch, article):
    fetch = Mock(return_value=[article])
    monkeypatch.setitem(collect.PROVIDERS, 'mock', fetch)
    service, writer = TaskService(engine), collect.write_json
    def fail_export(path, data):
        if path.name == 'diagnostics.json':
            raise OSError('simulated export failure')
        return writer(path, data)
    monkeypatch.setattr(collect, 'write_json', fail_export)
    task = service.submit(strategy, trial=True)
    worker = TaskWorker(engine, tmp_path)
    worker.execute(service.claim())
    assert service.get(task['task_id'])['status'] == 'failed'
    with Session(engine) as s:
        assert task_diagnostics(s, task['task_id'])['stats']['after_filter'] == 1
    monkeypatch.setattr(collect, 'write_json', writer)
    service.retry(task['task_id'])
    worker.execute(service.claim())
    assert service.get(task['task_id'])['status'] == 'succeeded'
    assert fetch.call_count == 1
    with Session(engine) as s:
        assert not list(s.exec(select(Article)))


def test_lifecycle_keeps_queued_work_history_and_plan_identity(engine, strategy, tmp_path):
    service = TaskService(engine)
    task = service.submit(strategy)
    with Session(engine) as s:
        job = save_job(s, dict(name='每天采集', profile_name=strategy, enabled=True, timezone='Asia/Shanghai', cron_expr='0 9 * * *'))
        assert profile_enabled(s, strategy)  # Existing 0.6 profile has no state row.
        assert set_profile_enabled(s, strategy, False)['paused_job_ids'] == [job['id']]
        with pytest.raises(TaskConflict):
            set_job_enabled(s, job['id'], True)
        s.rollback()
        with pytest.raises(TaskConflict, match='关联计划'):
            delete_profile(s, strategy)
        s.rollback()
        set_profile_enabled(s, strategy, True)
        assert not s.get(CollectJob, job['id']).enabled
        delete_job(s, job['id'])
        with pytest.raises(TaskConflict, match='未结束任务'):
            delete_profile(s, strategy)
        s.rollback()
        set_profile_enabled(s, strategy, False)
    with pytest.raises(TaskConflict):
        service.submit(strategy)
    TaskWorker(engine, tmp_path).execute(service.claim())
    assert service.get(task['task_id'])['status'] == 'succeeded'
    with Session(engine) as s:
        article_keys = [a.article_key for a in s.exec(select(Article))]
        assert article_keys
        delete_profile(s, strategy)
        assert [a.article_key for a in s.exec(select(Article))] == article_keys
        assert s.get(TaskRecord, task['task_id'])
        collect.create_or_update_profile(s, {'name': '新策略', 'provider': 'mock'})
        new = save_job(s, dict(name='新计划', profile_name='新策略', enabled=True, timezone='UTC', cron_expr='0 9 * * *'))
        assert new['id'] != job['id']
        assert s.get(CollectJob, job['id']).schedule_type == 'archived'
    init_db(engine)
    with Session(engine) as s:
        assert s.get(TaskRecord, task['task_id'])


def test_scheduled_submission_rechecks_current_schedule(engine, strategy):
    service = TaskService(engine)
    with Session(engine) as s:
        job = save_job(s, dict(name='schedule', profile_name=strategy, enabled=True, timezone='Asia/Shanghai', cron_expr='0 9 * * *'))
    due = dict(job_id=job['id'], occurrence_key='1:2026-09-30T01:00', scheduled_at='2026-09-30T01:00:00Z')
    with Session(engine) as s:
        save_job(s, dict(name='schedule', profile_name=strategy, enabled=True, timezone='UTC', cron_expr='0 10 * * *'), job['id'])
    assert service.submit(strategy, scheduled=due) is None
    with Session(engine) as s:
        assert not list(s.exec(select(ScheduleOccurrence)))
        save_job(s, dict(name='schedule', profile_name=strategy, enabled=True, timezone='Pacific/Honolulu', cron_expr='0 15 * * *'), job['id'])
    submitted = service.submit(strategy, {'date_str': '2099-01-01'}, scheduled=due)
    assert submitted['collection_date'] == '2026-09-29'
    assert submitted['timezone'] == 'Pacific/Honolulu'


@pytest.mark.parametrize('expression', ['60 9 * * *', '0 24 * * *', '0 9 0 * *', '0 9 * 13 *', '0 9 * * 8', '*/0 * * * *', '0, 9 * * *', '0 9 * * MON', '* * * *', '0 9 * * 5-1'])
def test_invalid_cron_is_rejected(expression):
    with pytest.raises(ValueError):
        parse_cron(expression)


def test_cron_timezone_dst_and_legacy_day_semantics():
    assert cron_matches('*/15 9 * * 1-5', datetime(2026, 9, 30, 9, 15))
    assert not cron_matches('0 9 1 * 1', datetime(2026, 10, 1, 9, 0))
    assert cron_matches('0 9 * * 7', datetime(2026, 10, 4, 9, 0))
    spring = next_occurrences('30 2 * * *', 'America/New_York', after=datetime(2026, 3, 8, tzinfo=timezone.utc), count=1)
    assert spring == ['2026-03-09T02:30-04:00']
    fall = next_occurrences('30 1 * * *', 'America/New_York', after=datetime(2026, 11, 1, tzinfo=timezone.utc), count=2)
    assert fall == ['2026-11-01T01:30-04:00', '2026-11-01T01:30-05:00']
    assert next_occurrences('0 9 30 2 *', 'UTC') == []
    with pytest.raises(ValueError):
        next_occurrences('0 9 * * *', 'not/a/zone')


def test_filter_diagnostics_count_actual_decisions(monkeypatch, article):
    monkeypatch.setattr(llm_filter, 'endpoint_alive', lambda: True)
    reply = {'items': [{'id': 1, 'keep': True, 'categories': ['technology'], 'sentiment': 'neutral'}]}
    monkeypatch.setattr(llm_filter, '_generate_with_retry', lambda *a, **k: json.dumps(reply))
    items = [dict(article, source={'domain': 'blocked.test'}), dict(article, title='excluded chip'), article,
             dict(article, title='tennis', content='tennis'), dict(article, title='chip factory')]
    diag = {}
    kept, mode = llm_filter.llm_filter_items(items, {'keywords_include': ['chip'], 'keywords_exclude': ['excluded'], 'source_blacklist': ['blocked.test']}, diagnostics=diag)
    assert mode == 'mixed'
    assert diag['counts'] == {'source_blocked': 1, 'excluded_keyword': 1, 'kept_ai': 1, 'not_relevant_rule': 1, 'kept_rule': 1}
    assert sum(diag['counts'].values()) == len(items)
    assert diag['after_hard_filter'] == 3 and diag['kept'] == len(kept) == 2
    assert diag['fallbacks'] == ['partial_response']
    assert all('_filter_index' not in item for item in kept + items)
    diag = {}
    monkeypatch.setattr(llm_filter, '_generate_with_retry', Mock(side_effect=RuntimeError('offline')))
    llm_filter.llm_filter_items([article] * 60, {}, diagnostics=diag)
    assert diag['counts'] == {'kept_rule': 60} and len(diag['samples']) == 50
    assert diag['fallbacks'] == ['request_failed']


@pytest.mark.parametrize('items,profile,expected', [([], {}, '新闻源返回 0 条'),
    ([{'title': 'chip', 'source': {'domain': 'a.test'}}], {'source_whitelist': ['b.test']}, '来源限制'),
    ([{'title': 'tennis'}], {'keywords_include': ['chip']}, '相关性筛选')])
def test_zero_results_explained(items, profile, expected):
    diag = {}
    kept, mode = llm_filter.llm_filter_items(items, profile, diagnostics=diag)
    text = summary(dict(diagnostics=diag, fetched=len(items), after_filter=len(kept), dry_run=True))
    assert expected in text


def test_api_validation_and_trial_cancellation(tmp_path):
    app = create_app(db_url=f'sqlite:///{tmp_path}/api.db', storage_root=str(tmp_path/'runs'))
    client = TestClient(app)  # No lifespan: control the worker ourselves.
    profile = dict(name='  中文试跑  ', provider='mock', categories=['', 'technology', 'technology'])
    response = client.post('/api/profiles', json=profile)
    assert response.status_code == 200
    assert response.json()['name'] == '中文试跑' and response.json()['categories'] == ['technology']
    for extra in [{'filter_mode': []}, {'timezone': ['UTC']}, {'date_str': 12}]:
        assert client.post('/api/profiles', json=dict(profile, extra=extra)).status_code == 422
    assert client.post('/api/profiles/中文试跑/test', json={'date_str': '2026-02-30'}).status_code == 422
    task = client.post('/api/profiles/中文试跑/test', json={'date_str': '2026-09-30'}).json()
    assert task['kind'] == 'trial'
    assert client.delete('/api/profiles/中文试跑').status_code == 409
    assert client.post('/api/tasks/'+task['task_id']+'/cancel').status_code == 200
    assert client.get('/api/tasks/'+task['task_id']+'/diagnostics').json()['task']['status'] == 'cancelled'
    assert client.get('/tasks/'+task['task_id']+'/diagnostics').status_code == 200
    assert client.post('/api/profiles/中文试跑/disable').status_code == 200
    assert client.post('/api/tasks/collect', json={'profile_name':'中文试跑'}).status_code == 409
    assert client.post('/api/profiles/中文试跑/test', json={}).status_code == 409
    assert client.post('/api/jobs', json={'name':'test','profile_name':'中文试跑'}).status_code == 409
    assert client.post('/api/jobs/preview', json={'cron_expr':'60 0 * * *'}).status_code == 422
    assert client.get('/profiles').status_code == 200
    assert client.delete('/api/profiles/中文试跑').status_code == 200
    assert len(client.get('/api/profiles/中文试跑/tests').json()) == 1
    app.state.engine.dispose()
