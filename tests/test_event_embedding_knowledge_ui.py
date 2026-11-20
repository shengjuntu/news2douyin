from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlmodel import Session, SQLModel, create_engine

from news2douyin.server import knowledge


BASE = '/api/knowledge/embeddings/'
DATES = {'start_date': '2026-11-01', 'end_date': '2026-11-10'}
COUNTS = dict(pending=2, running=1, ready=5, failed=0)
BACKFILL = dict(**DATES, state='scanning', scanned=4, counts=dict(**COUNTS, skipped=1))


@pytest.fixture
def app(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'knowledge.db'))
    SQLModel.metadata.create_all(engine, tables=[knowledge.VersionReading.__table__, knowledge.DeskDirty.__table__])
    app = FastAPI()
    app.state.desk = SimpleNamespace(graph=None)
    worker = Mock()
    worker.status.return_value = dict(enabled=True, ready=True, message='', counts=COUNTS, backfill=BACKFILL)
    worker.request_backfill.return_value = BACKFILL
    worker.search.return_value = dict(events=[dict(event_key='evt_1', title='港口复航',
                                                   first_seen_at='2026-11-02T12:00:00Z', similarity=0.86)],
                                      model='bge-m3')
    app.state.event_embeddings = worker
    monkeypatch.setattr(knowledge.knowledge, 'status', lambda *_: dict(state='unconfigured', analyzed_at=''))
    root = Path(knowledge.__file__).parent / 'templates'
    env = Environment(loader=FileSystemLoader(root), autoescape=select_autoescape(['html']))

    def render(template, **context):
        return HTMLResponse(env.get_template(template).render(**context))

    knowledge.register(app, engine, render)
    with TestClient(app) as client:
        yield client, worker
    engine.dispose()


def test_knowledge_page_and_read_only_status_show_embedding_progress(app, monkeypatch):
    client, worker = app
    html = client.get('/knowledge').text
    assert '事件向量' in html and '首次记录日期' in html
    assert '补算历史事件' in html and '查找相似事件' in html
    assert '扫描中 · 已检查 4' in html and '已生成 5' in html
    status = client.get('/api/knowledge/status').json()
    assert status['embeddings']['backfill'] == BACKFILL
    assert isinstance(worker.status.call_args.args[0], Session)
    worker.request_backfill.assert_not_called()
    worker.search.assert_not_called()

    from news2douyin.knowledge import radar
    monkeypatch.setattr(knowledge.knowledge, 'graph_view', lambda *_args, **_kw: {'nodes': []})
    monkeypatch.setattr(radar, 'sources', lambda *_args: {})
    state = client.get('/api/knowledge/graph').json()['state']
    assert 'embeddings' not in state
    assert worker.status.call_count == 2


def test_embedding_actions_require_valid_fields_and_same_origin(app):
    client, worker = app
    for endpoint, body in [('backfill', DATES), ('search', dict(**DATES, query='港口复航'))]:
        url = BASE + endpoint
        assert client.get(url).status_code == 405
        assert client.post(url, json=body, headers={'Origin': 'https://other.example'}).status_code == 403
        assert client.post(url, json=dict(body, start_date='无效')).status_code == 422
    assert client.post(BASE + 'search', json=dict(**DATES, query='  ')).status_code == 422
    worker.request_backfill.assert_not_called()
    worker.search.assert_not_called()


def test_embedding_posts_pass_dates_and_report_unavailable(app):
    client, worker = app
    accepted = client.post(BASE + 'backfill', json=DATES)
    assert accepted.status_code == 202 and accepted.json() == BACKFILL
    assert isinstance(worker.request_backfill.call_args.args[0], Session)
    assert worker.request_backfill.call_args.args[1:] == (date(2026, 11, 1), date(2026, 11, 10))
    result = client.post(BASE + 'search', json=dict(**DATES, query=' 港口复航 '))
    assert result.status_code == 200 and result.json()['events'][0]['event_key'] == 'evt_1'
    worker.search.assert_called_once_with('港口复航', date(2026, 11, 1), date(2026, 11, 10))

    worker.status.return_value.update(enabled=False, ready=False, message='请配置向量服务')
    worker.request_backfill.side_effect = worker.search.side_effect = ValueError('请配置向量服务')
    html = client.get('/knowledge').text
    assert '请配置向量服务' in html
    assert 'class="ghost" disabled>开始补算' in html and 'class="ghost" disabled>检索' in html
    assert client.post(BASE + 'backfill', json=DATES).status_code == 409
    assert client.post(BASE + 'search', json=dict(**DATES, query='港口复航')).json() == {'detail': '请配置向量服务'}
