import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from news2douyin.collect import service as collect
from news2douyin.collect import llm_filter
from news2douyin.collect.profiles import normalize_profile_dict
from news2douyin.scheduler import service as scheduling
from news2douyin.search.service import search_articles, search_events
from news2douyin.server.app import create_app
from news2douyin.server.schemas.common import ProfilePayload
from news2douyin.storage.models import Article, ArticleEventLink, CollectJob, Event, RunRecord, TaskRecord
from news2douyin.video.service import build_script_package
from conftest import FrozenTime


def collect_fixture(session, monkeypatch, tmp_path, items):
    monkeypatch.setitem(collect.PROVIDERS, 'fixture', lambda _: items)
    collect.create_or_update_profile(session, {'name': 'fixture', 'provider': 'fixture'})
    return collect.run_collection(session, 'fixture', storage_root=tmp_path / 'runs')


def test_mock_to_script_package(engine, tmp_path):
    with Session(engine) as session:
        collect.create_or_update_profile(session, {'name': 'mock', 'provider': 'mock'})
        run = collect.run_collection(session, 'mock', storage_root=tmp_path / 'runs')
        assert run.status == 'succeeded'
        events = list(session.exec(select(Event)))
        assert len(events) == 2
        package = build_script_package(session, events[0].event_key, output_root=tmp_path / 'packages')
        assert (Path(package.output_dir) / 'script.txt').read_text()


def test_duplicate_reuses_event(engine, article, monkeypatch, tmp_path):
    items = [article, dict(article, url='https://example.org/b')]
    with Session(engine) as session:
        collect_fixture(session, monkeypatch, tmp_path, items)
        articles = list(session.exec(select(Article).order_by(Article.id)))
        assert [a.is_duplicate for a in articles] == [False, True]
        assert len({a.dedup_group_id for a in articles}) == 1
        events = list(session.exec(select(Event)))
        assert len(events) == 1
        assert events[0].article_count == 2
        links = list(session.exec(select(ArticleEventLink)))
        assert {link.event_key for link in links} == {events[0].event_key}


def test_same_second_runs_have_unique_ids(engine, monkeypatch, tmp_path):
    monkeypatch.setattr(collect, 'datetime', FrozenTime)
    with Session(engine) as session:
        collect.create_or_update_profile(session, {'name': 'mock', 'provider': 'mock'})
        one = collect.run_collection(session, 'mock', storage_root=tmp_path / 'runs')
        two = collect.run_collection(session, 'mock', storage_root=tmp_path / 'runs')
        assert one.run_key != two.run_key
        assert one.storage_path != two.storage_path
        assert Path(one.storage_path).is_dir() and Path(two.storage_path).is_dir()


def test_article_and_event_update_are_atomic_on_db_error(engine, article, monkeypatch, tmp_path):
    def fail_event(session, item, event_key):
        row = session.exec(select(Article)).first()
        session.add(Article(article_key=row.article_key))
        session.flush()

    monkeypatch.setattr(collect, '_upsert_event', fail_event)
    with Session(engine) as session:
        with pytest.raises(IntegrityError):
            collect_fixture(session, monkeypatch, tmp_path, [article])
        run = session.exec(select(RunRecord)).one()
        assert run.status == 'failed'
        assert 'IntegrityError' in run.error_text
        assert not list(session.exec(select(Article)))
        assert not list(session.exec(select(ArticleEventLink)))
        assert not list(session.exec(select(Event)))
        assert json.loads((Path(run.storage_path) / 'raw.jsonl').read_text())['title'] == article['title']


def test_backfill_does_not_reverse_event_time(engine, article, monkeypatch, tmp_path):
    items = [article, dict(article, url='https://example.com/older', published_at='2026-09-01T00:00:00Z')]
    with Session(engine) as session:
        collect_fixture(session, monkeypatch, tmp_path, items)
        event = session.exec(select(Event)).one()
        assert event.first_seen_at == '2026-09-01T00:00:00Z'
        assert event.last_seen_at == article['published_at']


@pytest.mark.parametrize('job_timezone', ['UTC', 'Asia/Shanghai', 'America/New_York'])
def test_scheduler_once_per_utc_minute_across_instances(engine, monkeypatch, job_timezone):
    with Session(engine) as session:
        collect.create_or_update_profile(session, {'name': 'fixture', 'provider': 'mock'})
        session.add(CollectJob(name='job', profile_name='fixture', timezone=job_timezone, cron_expr='* * * * *'))
        session.commit()
    monkeypatch.setattr(scheduling, 'datetime', FrozenTime)
    first = scheduling.SchedulerService(engine)
    first.tick()
    first.tick()
    scheduling.SchedulerService(engine).tick()
    with Session(engine) as session:
        assert len(list(session.exec(select(TaskRecord)))) == 1


def test_failed_scheduled_job_does_not_block_following_job(engine, monkeypatch):
    with Session(engine) as session:
        collect.create_or_update_profile(session, {'name': 'good', 'provider': 'mock'})
        session.add_all([CollectJob(name=n, profile_name=n, cron_expr='* * * * *') for n in ['bad', 'good']])
        session.commit()
    monkeypatch.setattr(scheduling, 'datetime', FrozenTime)
    scheduling.SchedulerService(engine).tick()
    with Session(engine) as session:
        statuses = {j.name: j.last_status for j in session.exec(select(CollectJob))}
    assert statuses == {'bad': 'failed', 'good': 'queued'}
    with Session(engine) as session:
        assert session.exec(select(TaskRecord)).one().profile_name == 'good'


def test_search_filters_before_limit(engine):
    with Session(engine) as session:
        session.add_all([
            Article(article_key='old', title='needle 芯片', category_tags_json='["technology"]'),
            Article(article_key='new', title='unrelated', country='cn'),
            Event(event_key='old', event_title='needle 芯片', countries_json='["us"]', last_seen_at='2026-09-28T00:00:00Z'),
            Event(event_key='new', event_title='unrelated', last_seen_at='2026-09-29T00:00:00Z'),
        ])
        session.commit()
        assert len(search_articles(session, query='needle', country='us', category='technology', limit=1)) == 1
        assert len(search_events(session, query='芯片', country='us', limit=1)) == 1


def test_search_treats_sql_wildcards_as_literal_text(engine):
    with Session(engine) as session:
        session.add_all([Article(article_key='one', title='Profit 10%'), Article(article_key='two', title='other')])
        session.commit()
        assert [a.article_key for a in search_articles(session, query='%')] == ['one']


def test_profile_extra_reaches_provider_and_roundtrips(engine):
    payload = ProfilePayload(name='api', extra={'date_str': '2026-09-01', 'cache_enabled': False})
    with Session(engine) as session:
        collect.create_or_update_profile(session, payload.model_dump())
        profile = collect.get_profile(session, 'api')
        assert profile['date_str'] == '2026-09-01'
        assert profile['cache_enabled'] is False
        collect.create_or_update_profile(session, profile)
        assert collect.get_profile(session, 'api')['date_str'] == '2026-09-01'


def test_profile_core_fields_cannot_be_overridden_by_extra():
    profile = normalize_profile_dict({'name': 'api', 'provider': 'mock', 'extra': {'provider': 'worldnewsapi'}})
    assert profile['provider'] == 'mock'


@pytest.mark.parametrize('profile', [
    {'source_blacklist': ['example.com']},
    {'source_whitelist': ['trusted.example']},
    {'keywords_exclude': ['chip']},
])
def test_hard_filters_apply_before_llm(article, monkeypatch, profile):
    monkeypatch.setattr(llm_filter, 'endpoint_alive', lambda: True)
    generate = Mock(return_value='{"items":[{"id":1,"keep":true,"categories":[],"sentiment":"neutral"}]}')
    monkeypatch.setattr(llm_filter, '_generate_with_retry', generate)
    kept, _ = llm_filter.llm_filter_items([article], profile)
    assert kept == []
    generate.assert_not_called()


def test_partial_llm_response_does_not_silently_drop_items(article, monkeypatch):
    batch = [article, dict(article, title='Other business news', url='https://example.com/b')]
    monkeypatch.setattr(llm_filter, 'endpoint_alive', lambda: True)
    monkeypatch.setattr(llm_filter, '_generate_with_retry', lambda _: '{"items":[{"id":1,"keep":true,"categories":[],"sentiment":"neutral"}]}')
    kept, mode = llm_filter.llm_filter_items(batch, {})
    assert {i['url'] for i in kept} == {i['url'] for i in batch}
    assert mode in {'rules', 'mixed'}


@pytest.mark.parametrize('entries', [
    [{'id': 1, 'keep': True}, {'id': 1, 'keep': False}],
    [{'id': 1, 'keep': 'true'}],
    [{'id': 2, 'keep': True}],
])
def test_malformed_llm_decisions_trigger_rule_fallback(article, monkeypatch, entries):
    monkeypatch.setattr(llm_filter, 'endpoint_alive', lambda: True)
    monkeypatch.setattr(llm_filter, '_generate_with_retry', lambda _: json.dumps({'items': entries}))
    kept, mode = llm_filter.llm_filter_items([article], {})
    assert len(kept) == 1
    assert mode in {'rules', 'mixed'}


def test_webui_uses_configured_storage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    app = create_app(db_url=f'sqlite:///{tmp_path / "web.db"}', storage_root=str(tmp_path / 'custom'))
    with TestClient(app) as client:
        assert client.post('/api/profiles', json={'name': 'web', 'provider': 'mock'}).status_code == 200
        response = client.post('/webui/run-now', data={'profile_name': 'web'})
        assert response.status_code == 200
        task_id = str(response.url).split('/')[-1]
        import time
        deadline = time.monotonic() + 5
        while client.get('/api/tasks/' + task_id).json()['status'] != 'succeeded':
            assert time.monotonic() < deadline
            time.sleep(0.05)
        runs = client.get('/api/runs').json()
        assert Path(runs[0]['storage_path']).is_relative_to(tmp_path / 'custom')
        assert not (tmp_path / 'runs_v7').exists()


def test_webui_and_api_use_same_search(engine, tmp_path):
    app = create_app(db_url=f'sqlite:///{tmp_path / "search.db"}', storage_root=str(tmp_path / 'runs'))
    with Session(app.state.engine) as session:
        session.add(Article(article_key='source', title='Target article', source_domain='trusted.example'))
        session.commit()
    client = TestClient(app)
    assert len(client.get('/api/articles/search', params={'query': 'trusted.example'}).json()) == 1
    assert 'Target article' in client.get('/articles', params={'q': 'trusted.example'}).text
