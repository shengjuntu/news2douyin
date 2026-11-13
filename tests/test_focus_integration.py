"""User flows for persistent topics; model outputs are controlled fixtures.

The HTTP app deliberately runs without lifespan/background threads.  Explicit
worker ticks prove that interpretation and history processing leave requests.
"""
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sql_event, func, inspect, text
from sqlmodel import Session, select

from news2douyin.desk import focus_jobs, intent, service as desk
from news2douyin.desk.focus_models import FocusBackfill, FocusBuild, FocusCandidate, FocusDefinition
from news2douyin.desk.models import RadarEntry, Watch, WatchEvent
from news2douyin.extraction.models import FocusPoint
from news2douyin.server.app import create_app
from news2douyin.storage.db import init_db, make_engine
from news2douyin.storage.models import ArticleVersion
from news2douyin.storage.migrate import migrate_sqlite
from test_content_foundation import article, process
from test_postgres_migration import target  # Dedicated disposable PG database.


DESCRIPTION = '持续跟踪特朗普对乌克兰军援政策的变化，了解决策历史和实际影响。'


def spec():
    return dict(
        name='对乌军援政策', kind='长期事件', description=DESCRIPTION,
        entities=[
            dict(id='trump', name='Donald Trump', type='person',
                 aliases=['特朗普', 'Trump'], core=True),
            dict(id='ukraine', name='Ukraine', type='country',
                 aliases=['乌克兰'], core=True),
        ],
        relations=[dict(subject='trump', relation='military aid',
                        object='ukraine', keywords=['military aid', '军援'])],
        keywords=['military aid', 'Ukraine', '军援'],
        include=['美国对乌克兰援助政策变化'], exclude=['pure celebrity gossip'],
        questions=['对乌军援政策有哪些变化？', '这些变化的历史背景是什么？'],
        time_start='', time_end='', geography=[],
    )


def fixture_app(tmp_path):
    app = create_app(db_url='sqlite:///' + str(tmp_path / 'news.db'),
                     storage_root=str(tmp_path / 'runs'))
    return app, TestClient(app)


def finish(engine, limit=100):
    for _ in range(limit):
        if not focus_jobs.step(engine):
            return
    raise AssertionError('bounded topic workers did not settle')


def install_model_fixture(monkeypatch):
    calls = {'interpret': [], 'judge': []}

    def interpret_fixture(description, **_):
        calls['interpret'].append(description)
        return dict(spec=spec(), method='model', message='已生成关注范围')

    def judge_fixture(topic, card, recalled, **_):
        sources = json.loads(card.sources) if isinstance(card.sources, str) else card.sources
        grounded = next(s for s in sources if s.get('revision'))
        calls['judge'].append((card.key, grounded['key'], grounded['revision']))
        return dict(decision='core', method='model', reason='原文涉及对乌军援政策',
                    evidence=[dict(article=grounded['key'], revision=grounded['revision'],
                                   quote='military aid')],
                    questions=['对乌军援政策有哪些变化？'])

    monkeypatch.setattr(intent, 'interpret', interpret_fixture)
    monkeypatch.setattr(intent, 'judge', judge_fixture)
    return calls


def create_topic(client, engine, calls):
    started = client.post('/api/focus/interpret', json={'description': DESCRIPTION})
    assert started.status_code == 202, started.text
    draft = started.json()['key']
    assert not calls['interpret']  # No model I/O in the HTTP creation request.
    finish(engine)
    built = client.get('/api/focus/interpret/' + draft)
    assert built.status_code == 200, built.text
    assert built.json()['status'] == 'ready'
    assert built.json()['spec']['description'] == DESCRIPTION
    created = client.post('/api/focus/from-description', json={'draft': draft})
    assert created.status_code == 201, created.text
    return created.json()['key']


def test_description_to_topic_background_history_and_fixed_evidence(tmp_path, monkeypatch):
    calls = install_model_fixture(monkeypatch)
    app, client = fixture_app(tmp_path)
    article(app, 'policy', 'Donald Trump announced military aid to Ukraine.',
            'Ukraine military aid policy')
    process(app)
    key = create_topic(client, app.state.engine, calls)
    with Session(app.state.engine) as session:
        assert session.get(FocusDefinition, key).description == DESCRIPTION
        assert session.get(WatchEvent, (key, 'e_policy')) is None
    assert calls['judge'] == []  # Saving also avoids scanning/judging synchronously.
    finish(app.state.engine)
    assert calls['judge'] == [('e_policy', 'policy', 1)]
    with Session(app.state.engine) as session:
        link = session.get(WatchEvent, (key, 'e_policy'))
        assert link and link.choice == 'auto'
        original = session.exec(select(ArticleVersion).where(
            ArticleVersion.article_key == 'policy', ArticleVersion.revision == 1)).one()
        assert 'military aid' in original.payload_json
    tracking = client.get('/api/focus/' + key + '/tracking')
    assert tracking.status_code == 200, tracking.text
    assert client.get('/focus/' + key).status_code == 200
    # Reopening preserves the topic and material without repeating model work.
    client.close()
    app.state.engine.dispose()
    reopened, client = fixture_app(tmp_path)
    before = list(calls['judge'])
    assert client.get('/focus/' + key).status_code == 200
    finish(reopened.state.engine)
    assert calls['judge'] == before
    client.close()
    reopened.state.engine.dispose()


def test_manual_exclusion_survives_new_evidence_and_history_retry(tmp_path, monkeypatch):
    calls = install_model_fixture(monkeypatch)
    app, client = fixture_app(tmp_path)
    article(app, 'policy', 'Donald Trump announced military aid to Ukraine.', 'Aid policy')
    process(app)
    key = create_topic(client, app.state.engine, calls)
    finish(app.state.engine)
    route = '/api/focus/' + key
    assert client.post(route + '/links', json={'event': 'e_policy', 'action': 'exclude'}).status_code == 200
    article(app, 'policy', 'Donald Trump denied suspending military aid to Ukraine.', 'Aid policy correction')
    process(app)
    retried = client.post(route + '/tracking/retry', json={})
    assert retried.status_code == 200, retried.text
    finish(app.state.engine)
    with Session(app.state.engine) as session:
        assert session.get(WatchEvent, (key, 'e_policy')).choice == 'exclude'
    client.close()
    app.state.engine.dispose()


def test_model_unavailable_keeps_original_and_candidate_pending(tmp_path, monkeypatch):
    calls = install_model_fixture(monkeypatch)
    monkeypatch.setattr(intent, 'judge', lambda *_a, **_k: dict(
        decision='pending', method='unavailable', reason='模型暂时不可用', evidence=[], questions=[]))
    app, client = fixture_app(tmp_path)
    article(app, 'policy', 'Donald Trump announced military aid to Ukraine.', 'Aid policy')
    process(app)
    key = create_topic(client, app.state.engine, calls)
    finish(app.state.engine)
    state = client.get('/api/focus/' + key + '/tracking')
    assert state.status_code == 200, state.text
    assert '模型暂时不可用' in state.text
    with Session(app.state.engine) as session:
        assert session.get(WatchEvent, (key, 'e_policy')) is None
        assert session.exec(select(ArticleVersion).where(ArticleVersion.article_key == 'policy')).one()
    client.close()
    app.state.engine.dispose()


def test_correction_pending_does_not_reuse_old_core_judgment(tmp_path, monkeypatch):
    calls = install_model_fixture(monkeypatch)
    app, client = fixture_app(tmp_path)
    article(app, 'policy', 'Donald Trump announced military aid to Ukraine.', 'Aid policy')
    process(app)
    key = create_topic(client, app.state.engine, calls)
    finish(app.state.engine)
    with Session(app.state.engine) as session:
        assert session.get(WatchEvent, (key, 'e_policy')).relevance == 2
    monkeypatch.setattr(intent, 'judge', lambda *_a, **_k: dict(
        decision='pending', method='unavailable', reason='更正尚待核对', evidence=[], questions=[]))
    article(app, 'policy', 'Correction: Donald Trump denied suspending military aid to Ukraine.',
            'Military aid correction')
    process(app)
    finish(app.state.engine)
    with Session(app.state.engine) as session:
        assert session.get(WatchEvent, (key, 'e_policy')) is None
        audit = session.exec(select(FocusCandidate).where(
            FocusCandidate.watch == key, FocusCandidate.event == 'e_policy')).all()
        assert any(row.decision == 'core' and row.status == 'superseded' for row in audit)
        assert any(row.decision == 'pending' for row in audit)
        assert len(session.exec(select(ArticleVersion).where(ArticleVersion.article_key == 'policy')).all()) == 2
    client.close()
    app.state.engine.dispose()


def test_existing_sqlite_adds_topic_tables_without_rewriting_watch(tmp_path):
    engine = make_engine('sqlite:///' + str(tmp_path / 'legacy.db'))
    init_db(engine)
    with Session(engine) as session:
        session.add(Watch(key='w_legacy', name='旧专题', kind='人物', scope='特朗普'))
        session.add(WatchEvent(watch='w_legacy', event='legacy_event', choice='exclude', reason='人工排除'))
        session.add(FocusPoint(key='legacy_point', watch='w_legacy', parent='w_legacy',
                               text='保留已有知识问题', created='2026-11-01T00:00:00Z'))
        session.commit()
    topic_names = [model.__tablename__ for model in (FocusDefinition, FocusBuild, FocusBackfill, FocusCandidate)]
    watch_columns = [col['name'] for col in inspect(engine).get_columns('watch')]
    assert topic_names
    with engine.begin() as connection:
        for name in topic_names:
            connection.execute(text('DROP TABLE "' + name + '"'))
    init_db(engine)
    assert set(topic_names) <= set(inspect(engine).get_table_names())
    assert [col['name'] for col in inspect(engine).get_columns('watch')] == watch_columns
    with Session(engine) as session:
        assert session.get(Watch, 'w_legacy').scope == '特朗普'
        assert session.get(WatchEvent, ('w_legacy', 'legacy_event')).choice == 'exclude'
        assert session.get(FocusPoint, 'legacy_point').text == '保留已有知识问题'
    engine.dispose()


def test_manual_create_returns_before_reading_full_historical_cards(tmp_path):
    app, client = fixture_app(tmp_path)
    with Session(app.state.engine) as session:
        for n in range(240):
            session.add(RadarEntry(key=f'old_{n:04}', title='Trump policy',
                seen='2026-11-01T00:00:00Z', priority=1, band='其他进展', reason='',
                evidence='', headings='donald trump policy', material='old report',
                sources='[]', geography='[]'))
        session.commit()
    statements = []

    def observe(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.casefold())

    sql_event.listen(app.state.engine, 'before_cursor_execute', observe)
    try:
        made = client.post('/api/focus', json=dict(name='人工专题', kind='人物', scope='Donald Trump'))
    finally:
        sql_event.remove(app.state.engine, 'before_cursor_execute', observe)
    assert made.status_code == 201, made.text
    key = made.json()['key']
    assert not any('from radarentry' in sql and 'radarentry.material' in sql for sql in statements)
    with Session(app.state.engine) as session:
        assert session.exec(select(func.count()).select_from(WatchEvent).where(WatchEvent.watch == key)).one() == 0
    finish(app.state.engine)
    with Session(app.state.engine) as session:
        assert session.exec(select(func.count()).select_from(WatchEvent).where(WatchEvent.watch == key)).one() == 240
    client.close()
    app.state.engine.dispose()


def test_topic_definition_and_pending_state_migrate_with_full_hashes(tmp_path, monkeypatch, target):
    calls = install_model_fixture(monkeypatch)
    app, client = fixture_app(tmp_path)
    key = create_topic(client, app.state.engine, calls)
    client.close()
    app.state.engine.dispose()
    snapshot = tmp_path / 'source-snapshot.db'
    with sqlite3.connect(tmp_path / 'news.db') as source, sqlite3.connect(snapshot) as copy:
        source.backup(copy)
    before = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    report = migrate_sqlite(snapshot, target, source_stopped=True, chunk_size=2)
    assert report['status'] == 'ready'
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == before
    tables = {row['table']: row for row in report['tables']}
    assert tables[FocusDefinition.__tablename__]['source_rows'] == 1
    assert all(row['source_sha256'] == row['target_sha256'] for row in report['tables'])
    migrated = make_engine(target)
    with Session(migrated) as session:
        assert session.get(FocusDefinition, key).description == DESCRIPTION
        assert json.loads(session.get(FocusDefinition, key).spec_json)['questions']
    migrated.dispose()
