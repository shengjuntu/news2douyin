"""Administration counts stay scalar on SQLite and real PostgreSQL."""
from contextlib import contextmanager
import os
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session as OrmSession
from sqlmodel import Session

from news2douyin.server.app import create_app
from news2douyin.storage.models import Article, CollectJob, CollectProfile, Event, RunRecord


@pytest.fixture(params=['sqlite', 'postgresql'])
def administration(request, tmp_path, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_ENV_FILE', str(tmp_path/'absent.env'))
    admin = None
    if request.param == 'postgresql':
        raw = os.getenv('NEWS2DOUYIN_TEST_POSTGRES_URL')
        if not raw:
            pytest.skip('requires NEWS2DOUYIN_TEST_POSTGRES_URL and real PostgreSQL')
        name = 'n2d_admin_' + uuid4().hex
        admin = create_engine(raw, isolation_level='AUTOCOMMIT')
        with admin.connect() as connection:
            connection.execute(text('CREATE DATABASE "'+name+'"'))
        url = make_url(raw).set(database=name).render_as_string(hide_password=False)
    else:
        url = 'sqlite:///' + str(tmp_path/'admin.db')
    app = None
    try:
        app = create_app(db_url=url, storage_root=str(tmp_path/'storage'))
        yield TestClient(app), app.state.engine
    finally:
        if app is not None:
            app.state.engine.dispose()
        if admin is not None:
            with admin.connect() as connection:
                connection.execute(text('DROP DATABASE "'+name+'" WITH (FORCE)'))
            admin.dispose()


def populate(engine):
    with Session(engine) as session:
        session.add_all([CollectProfile(name='first'), CollectProfile(name='second')])
        for name, kind, enabled in [('ACTIVE_JOB_LABEL', 'cron', True),
                                    ('DISABLED_JOB_LABEL', 'cron', False),
                                    ('EXCLUDE_ARCHIVED_JOB_LABEL', 'archived', True)]:
            session.add(CollectJob(name=name, profile_name='first', schedule_type=kind, enabled=enabled))
        for index in range(3):
            # Large columns make accidental ORM retrieval a meaningful failure.
            session.add(Article(article_key='article-'+str(index), title='Article',
                                content='DO_NOT_LOAD_ARTICLE_TEXT '*4000,
                                raw_json='{"payload":"'+'x'*64000+'"}'))
            session.add(Event(event_key='event-'+str(index), event_title='Event',
                              summary='DO_NOT_LOAD_EVENT_TEXT '*4000))
            session.add(RunRecord(run_key='run-'+str(index), profile_name='first', status='succeeded'))
        session.commit()


@contextmanager
def scalar_statistics_only(engine, *, forbid_runs=False):
    statements = []
    forbidden = (Article, Event, RunRecord) if forbid_runs else (Article, Event)

    def loaded(session, instance):
        if session.bind is engine and isinstance(instance, forbidden):
            pytest.fail('statistics loaded '+type(instance).__name__+' ORM entity')

    def query(connection, cursor, statement, parameters, context, executemany):
        statements.append(' '.join(statement.lower().split()))

    event.listen(OrmSession, 'loaded_as_persistent', loaded)
    event.listen(engine, 'before_cursor_execute', query)
    try:
        yield statements
    finally:
        event.remove(engine, 'before_cursor_execute', query)
        event.remove(OrmSession, 'loaded_as_persistent', loaded)
    for table in ('article', 'event'):
        selected = [sql for sql in statements if 'from '+table+' ' in sql+' ']
        if table == 'event' or not forbid_runs:
            assert selected, 'expected scalar statistics for '+table
        for sql in selected:
            assert 'count(' in sql and 'from (select ' not in sql
            assert table+'.content' not in sql and table+'.raw_json' not in sql
            assert table+'.summary' not in sql


@pytest.mark.parametrize('populated', [False, True], ids=['empty', 'populated'])
def test_system_status_has_exact_uncached_counts_without_entity_loads(administration, populated):
    client, engine = administration
    if populated:
        populate(engine)
    with scalar_statistics_only(engine, forbid_runs=True):
        response = client.get('/api/system/status')
    assert response.status_code == 200
    data = response.json()
    assert set(data) == {'profiles', 'jobs', 'runs', 'events', 'scheduler_running',
                         'storage_root', 'worker_running'}
    assert {key:data[key] for key in ('profiles','jobs','runs','events')} == (
        dict(profiles=2, jobs=2, runs=3, events=3) if populated else
        dict(profiles=0, jobs=0, runs=0, events=0))
    assert not data['scheduler_running'] and not data['worker_running']
    # A subsequent request observes the database rather than a cross-request cache.
    with Session(engine) as session:
        session.add(Event(event_key='fresh', event_title='Fresh')); session.commit()
    with scalar_statistics_only(engine, forbid_runs=True):
        assert client.get('/api/system/status').json()['events'] == (4 if populated else 1)


@pytest.mark.parametrize('populated', [False, True], ids=['empty', 'populated'])
def test_admin_counts_do_not_select_article_event_text_and_hide_archived_jobs(administration, populated):
    client, engine = administration
    if populated:
        populate(engine)
    with scalar_statistics_only(engine) as statements:
        response = client.get('/admin')
    assert response.status_code == 200
    assert ('3 / 3 / 3' if populated else '0 / 0 / 0') in response.text
    assert 'DO_NOT_LOAD_' not in response.text
    assert 'EXCLUDE_ARCHIVED_JOB_LABEL' not in response.text
    if populated:
        assert 'ACTIVE_JOB_LABEL' in response.text and 'DISABLED_JOB_LABEL' in response.text
    run_reads = [sql for sql in statements if 'from runrecord ' in sql+' ']
    assert len(run_reads) == 2
    assert sum('count(' in sql for sql in run_reads) == 1
    assert all('count(' in sql or 'limit ' in sql for sql in run_reads)
