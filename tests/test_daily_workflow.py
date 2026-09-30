import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.collect import service as collect
from news2douyin.editorial.daily import choose, list_picks, build_pick
from news2douyin.search.dates import date_range
from news2douyin.search.service import article_page
from news2douyin.server.app import create_app
from news2douyin.storage.db import init_db
from news2douyin.storage.models import (Article, ArticleEventLink, CollectedObservation,
    DailySelection, Event, RunRecord, ScriptPackage, TaskRecord, CollectJob)
from news2douyin.tasks.service import TaskService
from news2douyin.tasks.worker import TaskWorker
from news2douyin.video.workbench import script_detail


def seed(session):
    session.add_all([Event(event_key='event', event_title='事件的聚合标题'),
        Article(article_key='chosen', title='选中报道', content='这条报道有可供编辑的正文。发布机构给出了具体说明。', source_domain='source.test', published_at='2026-09-29T16:00:00Z'),
        Article(article_key='other', title='不应混入的另一篇报道', content='这段资料不应被自动加入所选脚本。', published_at='2026-09-30T16:00:00Z'),
        ArticleEventLink(article_key='chosen', event_key='event'),
        ArticleEventLink(article_key='other', event_key='event')])
    session.commit()


def test_source_bound_draft_survives_reload_and_repeated_build(engine, tmp_path):
    with Session(engine) as s:
        seed(s)
        picked = choose(s, 'chosen', day='2026-09-30')
        first = build_pick(s, picked[0]['selection_key'], storage_root=tmp_path)
        doc = script_detail(s, first['package_key'])
        assert doc['document']['title'] == '选中报道'
        assert [source['article_key'] for source in doc['sources']] == ['chosen']
        assert doc['document']['visual_notes']
        assert '这条报道' in doc['script_text'] and '不应' not in doc['script_text']
        assert doc['editorial']['generation']['mode'] == 'basic'
    init_db(engine)
    with Session(engine) as s:
        picks = list_picks(s, '2026-09-30')
        second = build_pick(s, picks[0]['selection_key'], storage_root=tmp_path)
        assert second == dict(first, reused=True)
        choose(s, 'chosen', day='2026-09-30', active=False)
        assert list_picks(s, '2026-09-30') == []
        choose(s, 'chosen', day='2026-09-30', active=True)
        assert list_picks(s, '2026-09-30')[0]['package_key'] == first['package_key']
        assert len(list(s.exec(select(ScriptPackage)))) == 1
        assert list_picks(s, '2026-10-01') == []


def test_concurrent_generation_only_commits_one_script(engine, tmp_path, monkeypatch):
    from news2douyin.editorial import daily
    with Session(engine) as s:
        seed(s)
        key = choose(s, 'chosen')[0]['selection_key']
    barrier = Barrier(2)
    original = daily.draft_document
    def generate(*args):
        barrier.wait(timeout=5)
        return original(*args)
    monkeypatch.setattr(daily, 'draft_document', generate)
    def build(_):
        with Session(engine) as s:
            return build_pick(s, key, storage_root=tmp_path)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(build, range(2)))
    assert results[0]['package_key'] == results[1]['package_key']
    assert sorted(r['reused'] for r in results) == [False, True]
    with Session(engine) as s:
        assert len(list(s.exec(select(ScriptPackage)))) == 1


def test_model_failure_does_not_create_package_then_retry_succeeds(engine, tmp_path, monkeypatch):
    import requests
    with Session(engine) as s:
        seed(s)
        key = choose(s, 'chosen')[0]['selection_key']
        monkeypatch.setattr(requests, 'post', Mock(side_effect=requests.ConnectionError('private error')))
        with pytest.raises(ValueError, match='AI 脚本生成失败'):
            build_pick(s, key, mode='llm', storage_root=tmp_path)
        assert not list(s.exec(select(ScriptPackage)))
        assert s.get(DailySelection, key).package_key is None
        reply = {'title': '中文标题', 'script_text': '这是根据所选资料撰写的口播。', 'visual_notes': '来源卡', 'notes': '核对后使用'}
        post = Mock(return_value=Mock(json=lambda: {'choices': [{'message': {'content': json.dumps(reply)}}]}))
        monkeypatch.setattr(requests, 'post', post)
        result = build_pick(s, key, mode='llm', storage_root=tmp_path)
        detail = script_detail(s, result['package_key'])
        assert detail['document']['script_text'] == reply['script_text']
        assert detail['document']['source_keys'] == ['chosen']
        assert detail['editorial']['generation']['mode'] == 'llm'
        prompt = post.call_args.kwargs['json']['messages'][1]['content']
        assert '这条报道' in prompt and '不应混入' not in prompt


def test_selection_cancelled_during_model_call_is_not_saved(engine, tmp_path, monkeypatch):
    from news2douyin.editorial import daily
    from news2douyin.video.workbench import ScriptConflict
    with Session(engine) as s:
        seed(s)
        key = choose(s, 'chosen')[0]['selection_key']
    original = daily.draft_document
    def generate(*args):
        with Session(engine) as other:
            choose(other, 'chosen', active=False)
        return original(*args)
    monkeypatch.setattr(daily, 'draft_document', generate)
    with Session(engine) as s:
        with pytest.raises(ScriptConflict):
            build_pick(s, key, storage_root=tmp_path)
    with Session(engine) as s:
        assert not list(s.exec(select(ScriptPackage)))


def test_date_boundaries_filter_before_pagination(engine):
    with Session(engine) as s:
        seed(s)
        s.add_all([Article(article_key='before', published_at='2026-09-29T15:59:59Z'),
                   Article(article_key='last', published_at='2026-09-30T23:59:59+08:00'),
                   Article(article_key='unknown', published_at='')])
        s.commit()
        args = dict(date_from='2026-09-30', date_to='2026-09-30', timezone='Asia/Shanghai')
        page = article_page(s, **args, limit=1)
        assert page.total == 2 and page.items[0].article_key == 'last'
        assert article_page(s, **args, limit=1, offset=1).items[0].article_key == 'chosen'
        assert article_page(s, date_from='2026-09-30', date_to='2026-09-30', timezone='UTC').total == 2
        with pytest.raises(ValueError):
            article_page(s, date_from='2026-10-01', date_to='2026-09-30')
        with pytest.raises(ValueError):
            article_page(s, timezone='bad-zone')
    # A local calendar day can be 23 hours at the daylight-saving boundary.
    assert date_range('custom', '2026-03-08', '2026-03-08', 'America/New_York') == ('2026-03-08T05:00:00+00:00', '2026-03-09T04:00:00+00:00')


def test_repeat_collection_remains_visible_on_each_batch_day(engine):
    with Session(engine) as s:
        seed(s)
        s.add_all([TaskRecord(task_id='t1', profile_name='one', profile_json='{}', request_hash='x', created_at='2026-09-29T01:00:00Z'),
                   TaskRecord(task_id='t2', profile_name='two', profile_json='{}', request_hash='x', created_at='2026-09-30T01:00:00Z'),
                   CollectedObservation(observation_key='t1:0', scope_id='t1', input_index=0, article_key='chosen', revision=1, disposition='stored'),
                   CollectedObservation(observation_key='t2:0', scope_id='t2', input_index=0, article_key='chosen', revision=1, disposition='existing')])
        s.commit()
        for day in ['2026-09-29', '2026-09-30']:
            assert article_page(s, query='选中报道', date_from=day, date_to=day, time_field='collected').total == 1
        assert article_page(s, task_id='t2').items[0].article_key == 'chosen'
        assert article_page(s, profile_name='one').total == 1
        assert article_page(s, profile_name='two').total == 1


def test_queued_collection_freezes_local_day_and_no_silent_yesterday(engine, monkeypatch, tmp_path):
    from news2douyin.collect.providers import worldnewsapi
    from news2douyin.search import dates
    from conftest import FrozenTime
    monkeypatch.setattr(dates, 'datetime', FrozenTime)
    with Session(engine) as s:
        collect.create_or_update_profile(s, {'name': 'source', 'provider': 'worldnewsapi'})
    tasks = TaskService(engine)
    task = tasks.submit('source', {'timezone': 'Pacific/Honolulu'})
    assert task['collection_date'] == '2026-09-28'
    upstream = Mock(return_value=[])
    monkeypatch.setattr(worldnewsapi, 'fetch_top_news', upstream)
    TaskWorker(engine, tmp_path).execute(tasks.claim())
    assert tasks.get(task['task_id'])['status'] == 'succeeded'
    assert upstream.call_count == 1
    assert upstream.call_args.args[0]['date_str'] == '2026-09-28'


def test_http_complete_workflow_profiles_dates_and_validation(tmp_path):
    app = create_app(db_url=f'sqlite:///{tmp_path}/app.db', storage_root=str(tmp_path / 'runs'))
    client = TestClient(app)  # Drive worker explicitly; no background race.
    for path in ['/', '/daily', '/articles', '/profiles', '/scripts', '/videos', '/admin']:
        assert client.get(path).status_code == 200
    profile = {'name':'demo', 'provider':'mock', 'extra': {'request_timeout_sec': 20}}
    assert client.post('/api/profiles', json=profile).status_code == 200
    assert client.post('/api/profiles', json=dict(profile, name='demo_copy')).status_code == 200
    assert client.post('/api/profiles', json=dict(profile, max_items=0)).status_code == 422
    response = client.post('/webui/run-now', data={'profile_name':'demo', 'date_str':'2026-09-30', 'timezone':'Asia/Shanghai'}, follow_redirects=False)
    assert response.status_code == 303
    task_id = response.headers['location'].split('/')[-1]
    TaskWorker(app.state.engine, tmp_path/'runs').execute(app.state.tasks.claim())
    page = client.get('/daily', params={'task_id':task_id,'day':'2026-09-30'})
    assert page.status_code == 200 and '演示数据' in page.text
    data = client.get('/api/articles', params={'task_id':task_id, 'paginated':True}).json()
    assert data['total'] == 2
    key = data['items'][0]['article_key']
    picked = client.post('/api/daily/selections', json={'article_key':key, 'day':'2026-09-30'}).json()
    assert len(client.get('/api/daily/selections?day=2026-09-30').json()) == 1
    build = '/api/daily/selections/'+picked[0]['selection_key']+'/build'
    result = client.post(build, json={'mode':'basic'})
    assert result.status_code == 200
    package = result.json()['package_key']
    assert client.get('/scripts/'+package).status_code == 200
    assert client.post(build, json={'mode':'basic'}).json()['reused']
    assert client.post(build, json={'mode':'invented'}).status_code == 422
    assert client.post('/api/daily/selections', json={'article_key':'absent'}).status_code == 404
    assert client.get('/articles?date_from=invalid').status_code == 422
    assert client.get('/api/articles?date_from=2026-10-01&date_to=2026-09-30').status_code == 422
    assert client.get('/daily?day=2026-02-30').status_code == 422
    assert client.get('/daily?task_id=missing').status_code == 404
    assert 'period=today' in client.get('/articles?period=today&limit=1').text
