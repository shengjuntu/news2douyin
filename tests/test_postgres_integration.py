"""Real PostgreSQL product flows; never connect to a database by default.

Run with tools/run_postgres_test_server.py.  The configured URL only supplies
an administrative connection: each test creates and drops its own random DB.
Background/network workers are not started; queues advance explicitly.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from threading import Barrier
from uuid import uuid4
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sql_event, func, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.engine import make_url
from sqlmodel import Session, select

from news2douyin.collect import service as collect
from news2douyin.desk import service as desk
from news2douyin.desk.models import DeskDirty, GkgSource, RadarEntry, WatchEvent
from news2douyin.desk.rules import PRESETS
from news2douyin.extraction.models import VersionReading
from news2douyin.intake.lake import Lake
from news2douyin.intake.models import IntakeBatch
from news2douyin.intake.projection import (
    IntakeProjectionReceipt, import_document, project_step, projection_status,
)
from news2douyin.search.service import article_page, event_page
from news2douyin.server.app import create_app
from news2douyin.storage.db import init_db
from news2douyin.storage.articles import identity_key, lock_news
from news2douyin.storage.models import Article, ArticleEventLink, ArticleVersion, Event, RunRecord
from news2douyin.topics.models import TopicHandoff, TopicWorkspace
from news2douyin.workbench.models import ContentBinding
from sql_readonly_audit import is_read_only_execution


@pytest.fixture
def postgres_url():
    configured = os.getenv('NEWS2DOUYIN_TEST_POSTGRES_URL', '').strip()
    if not configured:
        pytest.skip('set NEWS2DOUYIN_TEST_POSTGRES_URL to run real PostgreSQL integration tests')
    psycopg = pytest.importorskip('psycopg')
    from psycopg import sql
    url = make_url(configured)
    if url.get_backend_name() != 'postgresql':
        pytest.fail('NEWS2DOUYIN_TEST_POSTGRES_URL must be a PostgreSQL URL')
    # The fixture owns only this freshly generated name, never the given DB.
    name = 'n2d_product_test_' + uuid4().hex
    native = url.set(drivername='postgresql', database='postgres').render_as_string(hide_password=False)
    with psycopg.connect(native, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    try:
        yield url.set(drivername='postgresql+psycopg', database=name).render_as_string(hide_password=False)
    finally:
        with psycopg.connect(native, autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))


@pytest.fixture
def pg_app(postgres_url, tmp_path, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_INTAKE_TOKEN', 'postgres-product-fixture-token')
    monkeypatch.setenv('NEWS2DOUYIN_GKG_STREAM_ENABLED', '0')
    monkeypatch.setenv('NEWS2DOUYIN_GKG_PROJECTION_ENABLED', '0')
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', '')
    app = create_app(db_url=postgres_url, storage_root=str(tmp_path / 'runs'))
    client = TestClient(app)  # No lifespan: do not launch remote/background workers.
    try:
        yield app, client, tmp_path / 'runs'
    finally:
        client.close()
        app.state.engine.dispose()


def post_document(client, **changes):
    payload = dict(provider='industry.monitor', record_id='report-17',
        uri='https://industry.example/report/17', title='Trump announces Ukraine aid talks',
        content='Donald Trump announced talks about military aid to Ukraine.',
        published_at='2026-11-04T10:00:00Z', language='en', country='us',
        metadata={'source_app': 'industry-monitor'})
    payload.update(changes)
    result = client.post('/api/intake/documents', json=payload,
        headers={'Authorization': 'Bearer postgres-product-fixture-token'})
    assert result.status_code == 200, result.text
    return result.json()


def gkg_line(stamp, index, title, *, url=None, date=None):
    columns = [''] * 27
    columns[:5] = [stamp + '-' + str(index), date or stamp, '1', 'media.example',
        url or 'https://media.example/article/' + str(index)]
    columns[7] = 'DIPLOMACY;'
    columns[11] = 'donald trump;'
    columns[13] = 'Example Organization;'
    columns[26] = '<PAGE_TITLE>' + title + '</PAGE_TITLE>' if title is not None else ''
    return '\t'.join(columns) + '\n'


def archive(engine, storage, rows, *, stamp='20261104120000', feed='gkg_english'):
    Path(storage).mkdir(parents=True, exist_ok=True)
    path = Path(storage) / (feed + stamp + '.zip')
    member = stamp + ('.translation' if feed == 'gkg_translation' else '') + '.gkg.csv'
    with ZipFile(path, 'w') as bundle:
        bundle.writestr(member, ''.join(rows))
    details = Lake(storage).ingest_archive(feed, stamp, path)
    key = feed + '_' + stamp
    with Session(engine) as session:
        session.add(IntakeBatch(key=key, feed=feed, stamp=stamp, state='archived',
            rows=details['rows'], valid_rows=details['valid_rows'], invalid_rows=details['invalid_rows']))
        session.commit()
    return key


def count(session, model):
    return session.exec(select(func.count()).select_from(model)).one()


def test_postgres_connector_versions_search_and_repeat_startup(pg_app):
    app, client, _ = pg_app
    engine = app.state.engine
    assert client.get('/api/health').json()['ok']
    first = post_document(client)
    assert first['revision'] == 1 and not first['reused']
    assert post_document(client)['reused']
    corrected = 'Donald Trump announced Ukraine military aid talks. The date was corrected.'
    second = post_document(client, content=corrected)
    assert second['article_key'] == first['article_key'] and second['revision'] == 2
    # Initialization is repeatable and keeps the immutable snapshots intact.
    init_db(engine)
    init_db(engine)
    with Session(engine) as session:
        assert count(session, Article) == 1
        versions = session.exec(select(ArticleVersion).order_by(ArticleVersion.revision)).all()
        assert [v.revision for v in versions] == [1, 2]
        assert 'corrected' not in json.loads(versions[0].payload_json)['content']
        assert json.loads(versions[1].payload_json)['content'] == corrected
        assert count(session, VersionReading) == 2
        page = article_page(session, query='Trump', period='custom', date_from='2026-11-04', date_to='2026-11-04')
        assert page.total == 1
        assert page.search_backend in {'pg_trgm+literal', 'pg_sql_literal_degraded'}
        if page.search_backend.endswith('degraded'):
            assert page.search_warning
        assert event_page(session, query='military aid').total == 1
    assert client.get('/articles?q=Trump').status_code == 200


def test_postgres_gkg_projection_resume_receipts_and_provenance(pg_app):
    app, client, storage = pg_app
    engine = app.state.engine
    stamp = '20261104120000'
    # A report's DATE can differ from the archive batch; metadata is not full text.
    key = archive(engine, storage, [
        gkg_line(stamp, 1, 'Trump announces Ukraine military aid talks', date='20261104115900'),
        gkg_line(stamp, 2, 'Trump announces Ukraine military aid talks', url='https://media.example/article/1'),
        gkg_line(stamp, 3, None),
        'malformed\tmetadata\n',
    ])
    assert project_step(engine, storage, limit=1, time_budget_ms=60000)
    with Session(engine) as session:
        assert session.get(IntakeBatch, key).projected_offset == 1
        assert count(session, ArticleVersion) == 1
    # A new engine is unnecessary for cursor recovery: every call is a fresh transaction.
    while project_step(engine, storage, limit=1, time_budget_ms=60000):
        pass
    state = projection_status(engine)
    assert state['projected'] == 3 and state['skipped'] == 1 and state['waiting'] == 0
    with Session(engine) as session:
        assert count(session, Article) == 2 and count(session, Event) == 1
        assert count(session, ArticleVersion) == 2 and count(session, IntakeProjectionReceipt) == 4
        assert session.get(IntakeBatch, key).projected_offset == 4
        article = session.exec(select(Article).where(Article.url == 'https://media.example/article/1')).one()
        assert article.provider == 'gkg' and article.content == ''
        assert session.get(GkgSource, article.article_key).record_id.startswith(stamp)
        empty = session.exec(select(Article).where(Article.url.endswith('/3'))).one()
        version = session.exec(select(ArticleVersion).where(ArticleVersion.article_key == empty.article_key)).one()
        assert session.get(VersionReading, version.id).state == 'metadata_only'
        batch = session.get(IntakeBatch, key)
        batch.projected_offset = 0
        session.add(batch)
        session.commit()
    while project_step(engine, storage, limit=2, time_budget_ms=60000):
        pass
    with Session(engine) as session:
        assert count(session, ArticleVersion) == 2 and count(session, IntakeProjectionReceipt) == 4
    raw = client.get('/api/intake/records/gkg_english/' + stamp + '/1')
    assert raw.status_code == 200, raw.text
    assert raw.json()['document_date'] == '20261104115900'
    assert not raw.json()['content_available'] and raw.json()['projection']['revision'] == 1


def test_postgres_radar_focus_and_content_workflow(pg_app):
    app, client, _ = pg_app
    receipt = post_document(client)
    watch_result = client.post('/api/focus', json=PRESETS[0])
    assert watch_result.status_code == 201, watch_result.text
    watch = watch_result.json()['key']
    desk.drain(app.state.engine)
    with Session(app.state.engine) as session:
        event = session.exec(select(Event)).one().event_key
        assert session.get(RadarEntry, event) is not None
        assert session.get(WatchEvent, (watch, event)) is not None
    assert 'Trump announces' in client.get('/radar?period=all').text
    assert 'Trump announces' in client.get('/focus/' + watch).text
    created = client.post('/api/workbench', json=dict(request_id='postgres-product-flow', event=event, watch=watch))
    assert created.status_code == 201, created.text
    task = created.json()
    assert task['event'] == event and task['watch'] == watch
    assert task['sources']
    assert receipt['article_key'] in client.get('/articles/' + receipt['article_key']).text
    assert client.get('/workbench/' + task['key'] + '/selection').status_code == 200
    review = client.post('/api/focus/' + watch + '/reviews', json={'event': event})
    duplicate = client.post('/api/focus/' + watch + '/reviews', json={'event': event})
    assert review.status_code == duplicate.status_code == 200
    assert review.json()['key'] == duplicate.json()['key']


def test_postgres_product_gets_never_write_or_drain_pending_work(pg_app):
    app, client, _ = pg_app
    receipt = post_document(client)
    watch = client.post('/api/focus', json=PRESETS[0]).json()['key']
    desk.drain(app.state.engine)
    with Session(app.state.engine) as session:
        event = session.exec(select(Event)).one().event_key
    task = client.post('/api/workbench', json=dict(request_id='postgres-read-audit', event=event)).json()['key']
    with Session(app.state.engine) as session:
        case = session.get(TopicWorkspace, task).case_id
        # A legacy/unbound handoff must not make a read repair relationships.
        session.add(TopicHandoff(handoff_id='legacy_unbound', workspace_id=task,
            request_hash='fixture', payload_json='{"action":"research"}', case_id=case))
        session.merge(DeskDirty(key=event))
        session.commit()
        pending = count(session, DeskDirty)
        bindings = count(session, ContentBinding)
    paths = ['/radar?period=all', '/focus', '/focus/' + watch, '/focus/' + watch + '/survey',
        '/events/' + event, '/articles', '/articles/' + receipt['article_key'],
        '/api/events/' + event, '/api/desk/status', '/api/intake/status',
        '/workbench', '/api/workbench/' + task, '/workbench/' + task,
        '/workbench/' + task + '/selection', '/api/system/status',
        '/api/desk/articles/' + receipt['article_key'] + '/1/mentions',
        '/api/knowledge/status', '/api/knowledge/graph?event=' + event,
        '/api/knowledge/graph?watch=' + watch, '/api/radar/view?period=all',
        '/knowledge', '/api/research/cases/' + case, '/research/' + case]
    statements = []
    def audit(conn, cursor, statement, parameters, context, executemany):
        statements.append((statement.lstrip(), is_read_only_execution(statement, context)))
    sql_event.listen(app.state.engine, 'before_cursor_execute', audit)
    try:
        for path in paths:
            response = client.get(path, follow_redirects=False)
            assert response.status_code in {200, 303}, (path, response.status_code, response.text[:500])
    finally:
        sql_event.remove(app.state.engine, 'before_cursor_execute', audit)
    mutating = [statement for statement, readonly in statements if not readonly]
    assert not mutating, '\n'.join(mutating)
    with Session(app.state.engine) as session:
        assert count(session, DeskDirty) == pending
        assert count(session, ContentBinding) == bindings


def test_postgres_concurrent_connector_retries_share_one_article_and_revision(pg_app):
    app, _, _ = pg_app
    barrier = Barrier(4)
    payload = dict(provider='industry.monitor', record_id='simultaneous',
        uri='https://industry.example/concurrent', title='Shared immutable report',
        content='Two connected applications retry the same original report.',
        published_at='2026-11-04T10:00:00Z', observed_at='2026-11-04T10:01:00Z')
    def submit(_):
        barrier.wait(timeout=15)
        return import_document(app.state.engine, payload)
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(submit, range(4)))
    assert len({r['article_key'] for r in receipts}) == 1
    assert {r['revision'] for r in receipts} == {1}
    assert sum(not r['reused'] for r in receipts) == 1
    with Session(app.state.engine) as session:
        assert count(session, Article) == 1 and count(session, ArticleVersion) == 1
        assert count(session, IntakeProjectionReceipt) == 1
        assert count(session, Event) == 1


def test_postgres_overlapping_collections_preserve_counts_and_article_history(pg_app, monkeypatch):
    app, _, storage = pg_app
    with Session(app.state.engine) as session:
        collect.create_or_update_profile(session, {'name': 'postgres-collect', 'provider': 'postgres_fixture'})
    barrier = Barrier(2)
    def fetch(_):
        barrier.wait(timeout=15)
        return [dict(title='Semiconductor demand rises as factories expand',
            content='Demand for semiconductor equipment increased as global factories expanded.',
            url='https://industry.example/semiconductors', country='us', language='en',
            published_at='2026-11-04T10:00:00Z', source={'domain': 'industry.example'})]
    monkeypatch.setitem(collect.PROVIDERS, 'postgres_fixture', fetch)
    def run(_):
        with Session(app.state.engine) as session:
            return collect.run_collection(session, 'postgres-collect', storage_root=storage).status
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, range(2)))
    assert results == ['succeeded', 'succeeded']
    with Session(app.state.engine) as session:
        assert count(session, RunRecord) == 2
        assert count(session, Article) == count(session, ArticleVersion) == count(session, ArticleEventLink) == 1
        assert session.exec(select(Event)).one().article_count == 1


def test_postgres_two_projectors_commit_receipts_and_cursor_once(pg_app, monkeypatch):
    app, _, storage = pg_app
    stamp = '20261104123000'
    key = archive(app.state.engine, storage,
        [gkg_line(stamp, index, 'Source headline ' + str(index)) for index in range(1, 5)], stamp=stamp)
    barrier = Barrier(2)
    original = Lake.rows
    def simultaneous_read(self, *args, **kwargs):
        rows = original(self, *args, **kwargs)
        barrier.wait(timeout=15)
        return rows
    with monkeypatch.context() as scoped:
        scoped.setattr(Lake, 'rows', simultaneous_read)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: project_step(app.state.engine, storage,
                limit=2, time_budget_ms=60000), range(2)))
        assert results == [True, True]
    with Session(app.state.engine) as session:
        assert session.get(IntakeBatch, key).projected_offset == 2
        assert count(session, IntakeProjectionReceipt) == count(session, ArticleVersion) == 2
    assert project_step(app.state.engine, storage, limit=2, time_budget_ms=60000)
    assert not project_step(app.state.engine, storage, limit=2, time_budget_ms=60000)
    with Session(app.state.engine) as session:
        assert session.get(IntakeBatch, key).projected_offset == 4
        assert count(session, IntakeProjectionReceipt) == count(session, ArticleVersion) == 4


@pytest.mark.parametrize('release', ['commit', 'rollback'])
def test_postgres_scoped_locks_exclude_only_same_resource_and_release_with_transaction(pg_app, release):
    app, _, _ = pg_app
    with Session(app.state.engine) as holder, Session(app.state.engine) as other:
        lock_news(holder, 'article:held')
        other.execute(text("SET LOCAL lock_timeout = '100ms'"))
        # Other logical resources do not share a global writer lock.
        lock_news(other, 'article:unrelated')
        with pytest.raises(OperationalError) as caught:
            lock_news(other, 'article:held')
        assert caught.value.orig.sqlstate == '55P03'
        other.rollback()
        getattr(holder, release)()
        other.execute(text("SET LOCAL lock_timeout = '100ms'"))
        lock_news(other, 'article:held')
        other.commit()


def test_postgres_open_writer_does_not_block_reads_or_unrelated_imports(pg_app):
    app, client, _ = pg_app
    first = post_document(client)
    desk.drain(app.state.engine)
    app.state.engine.dispose()
    def test_timeout(connection, _):
        # Shorten this test's waits on fresh connections, not product defaults.
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '100ms'")
        connection.commit()
    sql_event.listen(app.state.engine, 'connect', test_timeout)
    try:
        with Session(app.state.engine) as writer:
            lock_news(writer, 'article:' + identity_key({'url': 'https://industry.example/report/17'}))
            article = writer.exec(select(Article).where(Article.article_key == first['article_key'])).one()
            article.content = 'This uncommitted edit must stay invisible to readers.'
            writer.add(article)
            writer.flush()
            with ThreadPoolExecutor(max_workers=1) as pool:
                response = pool.submit(client.get, '/articles/' + first['article_key']).result(timeout=5)
            assert response.status_code == 200
            assert 'uncommitted edit' not in response.text
            unrelated = post_document(client, record_id='independent', uri='https://industry.example/independent')
            assert unrelated['article_key'] != first['article_key']
            blocked = client.post('/api/intake/documents', json=dict(provider='industry.monitor',
                record_id='report-17', uri='https://industry.example/report/17',
                title='A concurrent correction', content='Wait for the original article writer.'),
                headers={'Authorization': 'Bearer postgres-product-fixture-token'})
            assert blocked.status_code == 503, blocked.text
            assert blocked.headers['Retry-After'] == '2'
            writer.rollback()
        assert post_document(client, content='The correction after rollback.')['revision'] == 2
    finally:
        sql_event.remove(app.state.engine, 'connect', test_timeout)


def test_postgres_aggregated_radar_facets_keep_mixed_source_and_latest_revision_semantics(pg_app):
    from news2douyin.extraction.models import MentionReview, TextMention
    from news2douyin.knowledge import radar
    from news2douyin.storage.models import GdeltDiscovery
    app, client, _ = pg_app
    first = post_document(client)
    post_document(client, content='Corrected current revision about an attack.')
    second = post_document(client, record_id='second', uri='https://industry.example/second')
    third = post_document(client, record_id='third', uri='https://industry.example/third')
    desk.drain(app.state.engine)
    with Session(app.state.engine) as session:
        article2 = session.exec(select(Article).where(Article.article_key == second['article_key'])).one()
        article3 = session.exec(select(Article).where(Article.article_key == third['article_key'])).one()
        article2.provider = 'gdelt'
        article3.provider = ''
        session.add(article2)
        session.add(article3)
        session.add(GkgSource(article_key=first['article_key'], record_id='synthetic-gkg', attributes='{}'))
        session.add(GdeltDiscovery(article_key=second['article_key'], seen_at='2026-11-04T10:00:00Z'))
        versions = session.exec(select(ArticleVersion)).all()
        for version in versions:
            kind = 'meeting' if version.article_key in (first['article_key'], second['article_key']) else 'signing'
            if version.article_key == first['article_key'] and version.revision == 2:
                kind = 'attack'
            mention = 'facet-' + str(version.id)
            session.add(TextMention(id=mention, version_id=version.id, article=version.article_key,
                revision=version.revision, data=json.dumps({'kind': kind, 'status': 'reported', 'arguments': []})))
            if version.article_key == second['article_key']:
                session.add(MentionReview(id=mention, decision='rejected'))
        session.commit()
    with Session(app.state.engine) as session:
        data = radar.facets(session, radar.checked(period='all'))
        assert data['total'] == data['radar_stats']['source_count'] == 3
        channels = {row['id']: row['count'] for row in data['channels']}
        providers = {row['id']: row['count'] for row in data['providers']}
        assert channels == {'all': 3, 'diplomacy': 1, 'conflict': 1}
        assert providers == {'all': 3, 'gkg': 1, 'gdelt': 1, 'worldnewsapi': 0,
            'mock': 0, 'other': 1, 'unknown': 1}
        mixed = radar.facets(session, radar.checked(period='all', provider='gkg'))
        assert mixed['total'] == 1
        assert {row['id']: row['count'] for row in mixed['channels']} == {'all': 1, 'diplomacy': 0, 'conflict': 1}
