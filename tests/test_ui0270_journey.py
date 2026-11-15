"""Cross-feature editorial journey; translations remain reading copies.

These use real SQL backends and the real task worker with a deterministic model
boundary. No remote model, article server or graph server is contacted.
"""
from datetime import datetime, timezone
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sql_event, func
from sqlmodel import Session, select

from news2douyin.intake.official_models import OfficialEvent
from news2douyin.server.app import create_app
from news2douyin.storage.models import Article, Event
from news2douyin.topics.models import TopicSource
from news2douyin.workbench.models import ContentTask
from test_postgres_integration import postgres_url


@pytest.fixture(params=['sqlite', 'postgres'])
def journey(request, tmp_path, monkeypatch):
    for flag in ('NEWS2DOUYIN_GKG_STREAM_ENABLED', 'NEWS2DOUYIN_GKG_PROJECTION_ENABLED',
                 'NEWS2DOUYIN_GDELT_STREAM_ENABLED', 'NEWS2DOUYIN_GDELT_PROJECTION_ENABLED'):
        monkeypatch.setenv(flag, '0')
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', '')
    url = request.getfixturevalue('postgres_url') if request.param == 'postgres' else 'sqlite:///' + str(tmp_path/'journey.db')
    app = create_app(db_url=url, storage_root=str(tmp_path/'runs'))
    now = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    with Session(app.state.engine) as session:
        session.add(OfficialEvent(event_id='123456', event_date='2026-01-01', first_seen=now, last_seen=now,
            actor1_name='DONALD TRUMP', actor2_name='UKRAINE', event_code='072', event_base_code='072',
            event_root_code='07', action_country_iso2='ua', action_place='Kyiv',
            source_url='https://news.example/report', feed='events_english', stamp='20261114000000', row_index=1))
        session.commit()
    client = TestClient(app)  # Deliberately do not launch collection/research workers.
    try:
        yield app, client
    finally:
        client.close()
        app.state.engine.dispose()


def selected(client):
    result = client.post('/api/gdelt/events/123456/select', json={
        'request_id':'cross-feature-editorial-journey', 'title':'援助行动的变化', 'angle':'读取原文后解释背景'})
    assert result.status_code == 200, result.text
    return result.json()['key']


def test_source_snapshot_and_translation_do_not_manufacture_article_evidence(journey, monkeypatch):
    app, client = journey
    key = selected(client)
    with Session(app.state.engine) as session:
        source = session.exec(select(TopicSource).where(TopicSource.workspace_id == key)).one()
        sid, before, before_hash = source.source_id, source.document_json, source.content_hash
    page = client.get('/workbench/' + key + '/selection')
    assert page.status_code == 200
    assert '/articles/gdelt_event' not in page.text
    assert 'data-work-source="' + sid + '"' in page.text
    assert '事件线索' in page.text
    snapshot = client.get('/api/topics/' + key + '/sources/' + sid)
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()['source_kind'] == 'event_metadata'
    detail_url = snapshot.json()['detail_url']
    assert detail_url == '/radar?source=gdelt&event=123456'
    assert client.get(detail_url).status_code == 200

    from news2douyin.translations import service as translations
    calls = []
    def translate(original, _target):
        calls.append(original)
        return '特朗普向乌克兰提供援助。机器翻译仅供阅读。'
    monkeypatch.setattr(translations, 'translate', translate)
    queued = client.post('/api/translations', json={'kind':'topic_source', 'key':sid, 'target':'zh-CN'})
    assert queued.status_code in (200, 202), queued.text
    ident = queued.json()['id']
    task = app.state.worker.service.claim(app.state.worker.lease_seconds)
    assert task is not None and task.trigger_type == 'translation'
    app.state.worker.execute(task)
    done = client.get('/api/translations/' + ident)
    assert done.status_code == 200 and done.json()['status'] == 'succeeded', done.text
    assert done.json()['source_language'] == 'unknown'
    assert 'DONALD TRUMP' in calls[0]
    assert client.post('/api/translations', json={'kind':'topic_source', 'key':sid}).json()['id'] == ident
    assert len(calls) == 1
    with Session(app.state.engine) as session:
        source = session.get(TopicSource, sid)
        assert (source.document_json, source.content_hash) == (before, before_hash)
        assert json.loads(source.document_json)['source_kind'] == 'event_metadata'
        assert session.exec(select(func.count()).select_from(Article)).one() == 0
        assert session.exec(select(func.count()).select_from(Event)).one() == 0


def test_deleting_focus_preserves_its_independent_content_task_and_source(journey):
    app, client = journey
    key = selected(client)
    created = client.post('/api/focus', json={'name':'特朗普专题', 'kind':'人物', 'scope':'特朗普'})
    assert created.status_code == 201, created.text
    watch = created.json()
    with Session(app.state.engine) as session:
        task = session.get(ContentTask, key)
        task.watch = watch['key']
        session.add(task)
        session.commit()
    deleted = client.request('DELETE', '/api/focus/' + watch['key'], json={'version':watch['version']})
    assert deleted.status_code == 200, deleted.text
    assert client.get('/focus/' + watch['key']).status_code in (404, 410)
    assert client.get('/workbench/' + key + '/selection').status_code == 200
    context = client.get('/api/workbench/' + key)
    assert context.status_code == 200 and context.json()['watch_deleted'] is True
    assert context.json()['sources'][0]['source_kind'] == 'event_metadata'
    trashed = next(row for row in client.get('/api/focus/trash').json()['items'] if row['key'] == watch['key'])
    restored = client.post('/api/focus/' + watch['key'] + '/restore', json={'version':trashed['version']})
    assert restored.status_code == 200 and restored.json()['enabled'] is False, restored.text
    assert client.get('/focus/' + watch['key']).status_code == 200


def test_radar_count_is_explicit_and_matches_the_same_filter(journey):
    app, client = journey
    queries = []
    def capture(_conn, _cursor, _statement, _params, context, _many):
        queries.append(context.execution_options.get('n2d_query_name', ''))
    sql_event.listen(app.state.engine, 'before_cursor_execute', capture)
    try:
        page = client.get('/radar?source=gdelt&period=all&cameo=07&limit=1')
        assert page.status_code == 200
        assert not any(name == 'selection.candidates.count' for name in queries)
        count = client.get('/api/gdelt/events/count?period=all&cameo=07&limit=1')
        assert count.status_code == 200, count.text
        assert count.json()['status'] == 'complete'
        assert count.json()['total'] == 1 and count.json()['pages'] == 1
        empty = client.get('/api/gdelt/events/count?period=all&cameo=19&limit=1')
        assert empty.status_code == 200 and empty.json()['total'] == 0
    finally:
        sql_event.remove(app.state.engine, 'before_cursor_execute', capture)
