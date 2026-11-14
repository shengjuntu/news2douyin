"""Startup stays observable without waiting for external graph readiness."""
import json
import logging
from threading import Event as Signal

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event as sql_event
from sqlmodel import Session

from news2douyin.desk.graph import GraphMirror
from news2douyin.desk.models import DeskMeta
from news2douyin.extraction.models import VersionReading
from news2douyin.extraction import service as extraction
from news2douyin.server.app import create_app
from news2douyin.server.startup import StartupDiagnostics
from news2douyin.storage.db import init_db, make_engine
from news2douyin.storage.models import ArticleVersion
from test_postgres_integration import postgres_url


def test_startup_phase_timing_and_failure_redacts_messages(caplog):
    now = [0.0]
    report = StartupDiagnostics(clock=lambda: now[0])
    with caplog.at_level(logging.INFO, logger='uvicorn.error'):
        with report.phase('database.schema'):
            now[0] = .125
        with pytest.raises(RuntimeError):
            with report.phase('database.search_indexes'):
                now[0] = .5
                raise RuntimeError('postgresql://private:secret@example/db')
    snapshot = report.snapshot()
    assert snapshot['state'] == 'failed'
    assert snapshot['phases'][0]['elapsed_ms'] == 125
    assert snapshot['phases'][1] == dict(name='database.search_indexes', state='failed',
        elapsed_ms=375, error_type='RuntimeError')
    assert 'secret' not in json.dumps(snapshot) and 'secret' not in caplog.text
    assert 'state=running' in caplog.text and 'state=failed' in caplog.text


def test_startup_health_diagnostics_are_read_only_and_never_contact_graph(tmp_path, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', 'redis://private:secret@invalid:6379/0')
    monkeypatch.setenv('NEWS2DOUYIN_ENV_FILE', str(tmp_path/'absent.env'))
    monkeypatch.setattr(GraphMirror, '_execute', lambda *a, **k: pytest.fail('HTTP startup must not call graph'))
    app = create_app(db_url='sqlite:///'+str(tmp_path/'startup.db'), storage_root=str(tmp_path/'runs'))
    statements = []
    def record(*args):
        statements.append(args[2])
    sql_event.listen(app.state.engine, 'before_cursor_execute', record)
    try:
        client = TestClient(app)  # Inspect routes without launching workers.
        health = client.get('/api/health')
        details = client.get('/api/system/startup')
        client.close()
        assert health.status_code == 200 and health.json()['ok'] is True
        assert details.status_code == 200 and details.headers['cache-control'] == 'no-store'
        data = details.json()
        assert data['state'] == 'initialized' and not data['graph_readiness_required']
        names = {row['name'] for row in data['phases']}
        assert {'database.schema', 'database.extraction_triggers_backfill', 'application.desk'} <= names
        assert not statements
        assert 'secret' not in json.dumps(data)
    finally:
        sql_event.remove(app.state.engine, 'before_cursor_execute', record)
        app.state.engine.dispose()


def test_http_readiness_does_not_wait_for_graph_worker(tmp_path, monkeypatch):
    from news2douyin.desk.runtime import DeskWorker
    from news2douyin.tasks.worker import TaskWorker
    from news2douyin.scheduler.service import SchedulerService
    from news2douyin.research.runtime import ResearchRuntime
    monkeypatch.setenv('NEWS2DOUYIN_ENV_FILE', str(tmp_path/'absent.env'))
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', 'redis://invalid:6379/0')
    for cls in (TaskWorker, SchedulerService, ResearchRuntime):
        monkeypatch.setattr(cls, 'start', lambda self: None)
        monkeypatch.setattr(cls, 'stop', lambda self: None)
    for name in ('index_loop','read_loop','model_loop','focus_build_loop','focus_history_loop','focus_judge_loop'):
        monkeypatch.setattr(DeskWorker, name, lambda self: None)
    entered, release = Signal(), Signal()
    def blocked_graph(self, engine):
        entered.set()
        assert release.wait(10)
        return False
    monkeypatch.setattr(GraphMirror, 'step', blocked_graph)
    monkeypatch.setattr(GraphMirror, 'maintenance_step', lambda *a: False)
    from news2douyin.knowledge import service as knowledge
    monkeypatch.setattr(knowledge, 'refresh', lambda *a: False)
    app = create_app(db_url='sqlite:///'+str(tmp_path/'background.db'), storage_root=str(tmp_path/'runs'))
    try:
        with TestClient(app) as client:
            assert entered.wait(2)
            assert not release.is_set()
            data = client.get('/api/health').json()
            assert data['ok'] and data['startup']['http_ready']
            app.state.desk.stop_event.set()
            release.set()
    finally:
        release.set()
        app.state.engine.dispose()


@pytest.fixture(params=['sqlite', 'postgres'])
def startup_engine(request, tmp_path):
    url = request.getfixturevalue('postgres_url') if request.param == 'postgres' else 'sqlite:///'+str(tmp_path/'checkpoint.db')
    engine = make_engine(url)
    init_db(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def _legacy_missing_reading(engine):
    with Session(engine) as session:
        row = ArticleVersion(article_key='legacy', revision=1, content_hash='hash', payload_json='{}')
        session.add(row)
        session.flush()
        version = row.id
        session.delete(session.get(VersionReading, version))
        session.delete(session.get(DeskMeta, 'extraction_startup_backfill_1'))
        session.commit()
    return version


def test_repeat_startup_skips_completed_version_aggregate_but_trigger_stays_live(startup_engine):
    engine = startup_engine
    version = _legacy_missing_reading(engine)
    extraction.initialize(engine, checkpoint=True)
    with Session(engine) as session:
        assert session.get(VersionReading, version).state == 'baseline'
    statements = []
    def record(_conn, _cursor, statement, *_args):
        statements.append(statement.lower())
    sql_event.listen(engine, 'before_cursor_execute', record)
    try:
        init_db(engine)
    finally:
        sql_event.remove(engine, 'before_cursor_execute', record)
    assert not any('from articleversion group by article_key' in ' '.join(sql.split()) for sql in statements)
    with Session(engine) as session:
        fresh = ArticleVersion(article_key='legacy', revision=2, content_hash='changed', payload_json='{}')
        session.add(fresh)
        session.flush()
        assert session.get(VersionReading, fresh.id).state == 'baseline'
        session.commit()


def test_extraction_checkpoint_keeps_reader_mode_transitions(startup_engine, monkeypatch):
    engine = startup_engine
    version = _legacy_missing_reading(engine)
    extraction.initialize(engine, checkpoint=True)
    with Session(engine) as session:
        row = session.get(VersionReading, version)
        row.state, row.method = 'ready', 'rules'
        session.add(row)
        session.commit()
    monkeypatch.setenv('NEWS2DOUYIN_EVENT_READER', 'model')
    extraction.initialize(engine, checkpoint=True)
    with Session(engine) as session:
        assert session.get(VersionReading, version).state == 'model_queued'
        assert session.get(DeskMeta, 'extraction_startup_backfill_1').value.endswith(':model')
    # The marker changes back with configuration, rather than permanently
    # suppressing future rules -> model transitions.
    monkeypatch.setenv('NEWS2DOUYIN_EVENT_READER', 'rules')
    extraction.initialize(engine, checkpoint=True)
    with Session(engine) as session:
        row = session.get(VersionReading, version)
        row.state = 'ready'
        session.add(row)
        session.commit()
    monkeypatch.setenv('NEWS2DOUYIN_EVENT_READER', 'model')
    extraction.initialize(engine, checkpoint=True)
    with Session(engine) as session:
        assert session.get(VersionReading, version).state == 'model_queued'


def test_failed_backfill_does_not_publish_checkpoint_and_retries(startup_engine):
    engine = startup_engine
    version = _legacy_missing_reading(engine)
    def fail(_conn, _cursor, statement, *_args):
        if 'INSERT INTO deskmeta(key,value)' in statement:
            raise RuntimeError('injected checkpoint write failure')
    sql_event.listen(engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(RuntimeError, match='injected'):
            extraction.initialize(engine, checkpoint=True)
    finally:
        sql_event.remove(engine, 'before_cursor_execute', fail)
    with Session(engine) as session:
        assert session.get(DeskMeta, 'extraction_startup_backfill_1') is None
        assert session.get(VersionReading, version) is None
    extraction.initialize(engine, checkpoint=True)
    with Session(engine) as session:
        assert session.get(DeskMeta, 'extraction_startup_backfill_1')
        assert session.get(VersionReading, version).state == 'baseline'
