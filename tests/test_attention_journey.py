"""NewsNow HTTP -> durable inbox -> lake -> PG summaries -> editorial handoff.

Run on disposable SQLite and actual PostgreSQL via the PG QA launcher. Synthetic
lists are intentionally not evidence about real audience attention. Lifespan is
off: collection and attention workers advance explicitly; no model is called.
"""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sql_event, func
from sqlmodel import Session, select

from attention_test_support import attention_http, profile, collect_observed_round, execute, TARIFF, CEASEFIRE, DENIAL
from news2douyin.attention.models import AttentionInbox, AttentionMeta, AttentionTopic, AttentionItem
from news2douyin.desk.models import Watch, WatchEvent
from news2douyin.intake.official_models import OfficialEvent, OfficialMention
from news2douyin.server.app import create_app
from news2douyin.storage.models import Article, Event, RunRecord
from sql_readonly_audit import is_read_only_execution
from test_postgres_integration import postgres_url


@pytest.fixture(params=['sqlite', 'postgres'])
def attention_case(request, tmp_path, monkeypatch):
    for flag in ('NEWS2DOUYIN_GKG_STREAM_ENABLED', 'NEWS2DOUYIN_GKG_PROJECTION_ENABLED',
                 'NEWS2DOUYIN_GDELT_STREAM_ENABLED', 'NEWS2DOUYIN_GDELT_PROJECTION_ENABLED'):
        monkeypatch.setenv(flag, '0')
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', '')
    url = request.getfixturevalue('postgres_url') if request.param == 'postgres' else 'sqlite:///' + str(tmp_path/'app.db')
    root = tmp_path/'runs'
    app = create_app(db_url=url, storage_root=str(root))
    client = TestClient(app)
    try:
        with attention_http() as upstream:
            yield app, client, root, upstream
    finally:
        client.close()
        app.state.engine.dispose()


def count(session, model):
    return session.exec(select(func.count()).select_from(model)).one()


def drain(app, maximum=40):
    """Use the public worker one tick at a time, never a background busy loop."""
    worker = app.state.attention
    for _ in range(maximum):
        if not worker.step():
            return
    raise AssertionError('Attention queue did not drain within bounded fixture ticks')


def topics(client):
    result = client.get('/api/attention/topics?period=all&limit=100')
    assert result.status_code == 200, result.text
    return result.json()['items']


def topic_title(client, title):
    return next(row for row in topics(client) if row['title'] == title)


def test_http_collection_manifest_worker_and_topic_scope(attention_case):
    app, client, root, upstream = attention_case
    from news2douyin.intake.service import initialize
    from news2douyin.intake.models import FeedState
    initialize(app.state.engine)
    with Session(app.state.engine) as session:
        feed_before = {row.key: row.model_dump() for row in session.exec(select(FeedState))}
    assert len(feed_before) == 6
    name = profile(client, upstream)
    execute(app, client, client.post('/api/profiles/Attention%20journey/test', json={}))
    with Session(app.state.engine) as session:
        assert count(session, Article) == count(session, Event) == count(session, AttentionInbox) == 0
    assert topics(client) == []
    collect_observed_round(app, client, name, upstream)
    manifests = list(root.rglob('newsnow-snapshots.json'))
    assert manifests and all(path.stat().st_size < 128_000 for path in manifests)
    with Session(app.state.engine) as session:
        inbox = session.exec(select(AttentionInbox)).all()
        assert len(inbox) == 1
        assert len(inbox[0].manifest_sha256) == 64 and inbox[0].manifest_path
        assert count(session, AttentionTopic) == 0, 'Formal request synchronously built the attention index'
        assert count(session, Event) == 0, 'NewsNow discovery fabricated a local fact event'
    drain(app)
    tariff, ceasefire = topic_title(client, TARIFF), topic_title(client, CEASEFIRE)
    assert tariff['key'] != ceasefire['key']
    assert tariff['platform_count'] == 2, 'Realtime CLS must not become a third Chinese heat platform'
    assert tariff['rise_score'] is None, 'First observation has no historical rise baseline'
    detail = client.get('/api/attention/topics/' + tariff['key']).json()
    assert len(detail['items']) >= 2
    assert all(row['title'] == TARIFF for row in detail['items'])
    assert all(row.get('snapshot_ref') or row.get('snapshot_ref_json') for row in detail['items'])
    assert not any(row['title'] == '英伟达' and row.get('attention_score') for row in topics(client))
    assert set(upstream.calls) >= {'zhihu', 'weibo', 'douyin', 'cls', 'fastbull', 'xueqiu', 'github'}
    with Session(app.state.engine) as session:
        assert {row.key: row.model_dump() for row in session.exec(select(FeedState))} == feed_before


def test_cached_rounds_replay_and_failed_source_keep_unknown(attention_case):
    app, client, root, upstream = attention_case
    name = profile(client, upstream)
    collect_observed_round(app, client, name, upstream); drain(app)
    first = topic_title(client, TARIFF)
    with Session(app.state.engine) as session:
        topic_count, item_count = count(session, AttentionTopic), count(session, AttentionItem)
    upstream.set_round(1)
    collect_observed_round(app, client, name, upstream); drain(app)
    cached = topic_title(client, TARIFF)
    assert cached['key'] == first['key'] and cached['attention_score'] == first['attention_score']
    assert cached['rise_score'] is None or cached['rise_score'] == 0
    with Session(app.state.engine) as session:
        assert count(session, AttentionTopic) == topic_count and count(session, AttentionItem) == item_count
    upstream.set_round(2)
    collect_observed_round(app, client, name, upstream); drain(app)
    assert topic_title(client, TARIFF)['key'] == first['key']
    assert topic_title(client, DENIAL)['key'] != first['key'], 'A negated action must remain a separate candidate issue'
    detail = client.get('/api/attention/topics/' + first['key']).json()
    saved = json.dumps(detail, ensure_ascii=False)
    assert 'weibo' in saved and 'douyin' in saved
    assert topic_title(client, CEASEFIRE)['key'] != first['key']
    with Session(app.state.engine) as session:
        assert {row.state for row in session.exec(select(AttentionInbox))} == {'done'}


def test_attention_get_is_sql_only_readonly_and_count_free(attention_case, monkeypatch):
    app, client, root, upstream = attention_case
    name = profile(client, upstream)
    collect_observed_round(app, client, name, upstream); drain(app)
    tariff = topic_title(client, TARIFF)
    from news2douyin.attention.lake import AttentionLake
    calls = []
    def forbidden(*args, **kwargs):
        calls.append('external')
        raise AssertionError('An attention GET accessed the lake or graph')
    monkeypatch.setattr(AttentionLake, '__init__', forbidden)
    for name in ('query', 'read', 'explain'):
        monkeypatch.setattr(app.state.desk.graph, name, forbidden)
    executions = []
    def audit(_connection, _cursor, statement, _parameters, context, _executemany):
        executions.append((statement, is_read_only_execution(statement, context)))
    sql_event.listen(app.state.engine, 'before_cursor_execute', audit)
    try:
        for url in ('/radar?source=attention', '/api/attention/topics?period=all',
                    '/api/attention/topics/' + tariff['key'], '/api/attention/status'):
            response = client.get(url)
            assert response.status_code == 200, response.text
        assert executions and all(read for _sql, read in executions)
        assert not any('count(' in sql.lower().replace(' ', '') for sql, _ in executions), executions
        assert calls == []
    finally:
        sql_event.remove(app.state.engine, 'before_cursor_execute', audit)


def test_attention_selection_freezes_leads_and_reuses_workbench(attention_case):
    app, client, root, upstream = attention_case
    name = profile(client, upstream)
    collect_observed_round(app, client, name, upstream); drain(app)
    tariff = topic_title(client, TARIFF)
    body = dict(request_id='attention-editorial-fixture', angle='核对关税变化及对普通家庭的影响')
    url = '/api/attention/topics/' + tariff['key'] + '/select'
    created = client.post(url, json=body)
    assert created.status_code in {200, 201}, created.text
    made = created.json()
    assert made['url'].startswith('/workbench/')
    assert client.post(url, json=body).json() == made
    assert client.post(url, json=dict(body, angle='另一角度')).status_code == 409
    page = client.get(made['url'])
    assert page.status_code == 200 and '关税' in page.text
    task_key = made['url'].split('/')[2]
    task = client.get('/api/topics/' + task_key).json()
    assert task['sources'] and all(row['requires_original_text'] for row in task['sources'])
    assert task['materials']['can_write_from_sources'] is False
    from news2douyin.topics import service as topic_service
    class DeferredResearch:
        def __init__(self): self.calls = []
        def enqueue(self, *args, **kwargs): self.calls.append((args, kwargs))
    research = DeferredResearch()
    with pytest.raises(ValueError, match='先补材或研究'):
        topic_service.handoff(app.state.engine, research, task_key, dict(request_id='qa-attention-unsafe-short',
            revision=task['revision'], action='short'))
    assert research.calls == []
    deep = topic_service.handoff(app.state.engine, research, task_key, dict(request_id='qa-attention-deep',
        revision=task['revision'], action='deep'))
    assert len(research.calls) == 1 and deep['case_id']
    from news2douyin.research.models import ResearchSource
    with Session(app.state.engine) as session:
        sources = session.exec(select(ResearchSource).where(ResearchSource.case_id == deep['case_id'])).all()
        assert len(sources) == len(task['sources']) and all(row.kind == 'search_snippet' for row in sources)
    frozen = json.dumps(task['sources'], ensure_ascii=False, sort_keys=True)
    upstream.set_round(2)
    collect_observed_round(app, client, name, upstream); drain(app)
    assert json.dumps(client.get('/api/topics/' + task_key).json()['sources'], ensure_ascii=False, sort_keys=True) == frozen


def test_corrupt_raw_archive_is_visible_failure_and_never_zero_attention(attention_case):
    app, client, root, upstream = attention_case
    name = profile(client, upstream)
    collect_observed_round(app, client, name, upstream)
    with Session(app.state.engine) as session:
        inbox = session.exec(select(AttentionInbox)).one()
        manifest = json.loads((root/inbox.manifest_path).read_text())
    raw = root/manifest['snapshots'][0]['archive_ref']
    raw.write_bytes(b'corrupt QA archive: checksum must fail')
    app.state.attention.step()
    with Session(app.state.engine) as session:
        inbox = session.exec(select(AttentionInbox)).one()
        assert inbox.state != 'done' and inbox.error
        assert 'checksum' in inbox.error.lower()
        assert count(session, AttentionTopic) == 0
    status = client.get('/api/attention/status')
    assert status.status_code == 200
    assert 'checksum' in json.dumps(status.json()).lower()
    assert topics(client) == [], 'Corrupt evidence was converted to zero-valued attention summaries'
    assert raw.read_bytes().startswith(b'corrupt'), 'A failed derivative job rewrote evidence'


def test_pg_publication_failure_after_duckdb_replays_without_duplicate_topics(attention_case, monkeypatch):
    app, client, root, upstream = attention_case
    name = profile(client, upstream)
    collect_observed_round(app, client, name, upstream)
    from news2douyin.attention import grouping
    def failed_publication(*args, **kwargs):
        raise RuntimeError('QA PostgreSQL publication failed after lake receipt')
    with monkeypatch.context() as patcher:
        patcher.setattr(grouping, 'apply_prepared', failed_publication)
        app.state.attention.step()
    import duckdb
    from news2douyin.attention.lake import AttentionLake
    with duckdb.connect(str(AttentionLake(root).path), read_only=True) as db:
        receipts = db.execute('SELECT count(*) FROM attention_receipts').fetchone()[0]
        assert receipts == 1
    with Session(app.state.engine) as session:
        assert count(session, AttentionTopic) == 0
        inbox = session.exec(select(AttentionInbox)).one()
        assert inbox.error and inbox.state != 'done'
        inbox.state = 'pending'; inbox.next_attempt = 0; inbox.lease_until = 0; inbox.lease = ''
        session.add(inbox); session.commit()
    drain(app)
    before = {row['key']: (row['attention_score'], row['rise_score']) for row in topics(client)}
    assert before
    with Session(app.state.engine) as session:
        items_before = count(session, AttentionItem)
        inbox = session.exec(select(AttentionInbox)).one()
        inbox.state = 'pending'; inbox.next_attempt = 0; inbox.lease_until = 0; inbox.lease = ''
        session.add(inbox); session.commit()
    drain(app)
    assert {row['key']: (row['attention_score'], row['rise_score']) for row in topics(client)} == before
    with Session(app.state.engine) as session:
        assert count(session, AttentionItem) == items_before
    with duckdb.connect(str(AttentionLake(root).path), read_only=True) as db:
        assert db.execute('SELECT count(*) FROM attention_receipts').fetchone()[0] == receipts


def test_official_candidates_manual_decisions_and_watch_scope_are_not_facts(attention_case):
    app, client, root, upstream = attention_case
    name = profile(client, upstream)
    collect_observed_round(app, client, name, upstream); drain(app)
    tariff = topic_title(client, TARIFF)
    now = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    with Session(app.state.engine) as session:
        session.add(Watch(key='w_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', name='QA特朗普关税专题', kind='topic', scope='特朗普', dimensions='关税'))
        for index, source in enumerate(('zhihu', 'weibo'), 1):
            ident, uri = 'qa-event-' + str(index), f'https://{source}.example/news/tariff'
            session.add(OfficialEvent(event_id=ident, event_date=now[:10], first_seen=now, last_seen=now,
                actor1_name='DONALD TRUMP', actor2_name='CHINA', source_url=uri,
                event_code='172', event_base_code='172', event_root_code='17',
                feed='events_english', stamp=stamp, row_index=index))
            session.add(OfficialMention(key='qa-mention-' + str(index), event_id=ident,
                doc_identifier=uri, domain=source+'.example', first_seen=now, last_seen=now,
                first_feed='mentions_english', first_stamp=stamp, first_row_index=index,
                last_feed='mentions_english', last_stamp=stamp, last_row_index=index))
        session.commit()
    from news2douyin.attention import matching
    matching.refresh_topic(app.state.engine, tariff['key'])
    detail_url = '/api/attention/topics/' + tariff['key']
    detail = client.get(detail_url).json()
    assert len(detail['events']) == 2 and {row['status'] for row in detail['events']} == {'candidate'}
    assert any(row['key'] == 'w_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' for row in detail['watches'])
    for match, status in zip(detail['events'], ('confirmed', 'rejected')):
        result = client.post('/api/attention/matches/' + match['key'], json={'status': status})
        assert result.status_code == 200, result.text
    matching.refresh_topic(app.state.engine, tariff['key'])
    assert {row['status'] for row in client.get(detail_url).json()['events']} == {'confirmed', 'rejected'}
    scoped = client.get('/api/attention/topics?period=all&watch=w_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa').json()['items']
    assert any(row['key'] == tariff['key'] for row in scoped)
    assert all(row['title'] != CEASEFIRE for row in scoped)
    with Session(app.state.engine) as session:
        assert count(session, Event) == count(session, WatchEvent) == 0


def test_old_run_backfill_is_pk_paged_and_not_synchronous_startup(attention_case, monkeypatch):
    app, client, root, upstream = attention_case
    from news2douyin.collect.newsnow_archive import persist_batch
    from news2douyin.attention import runtime
    expected = 0
    with Session(app.state.engine) as session:
        for index in range(55):
            run_key = 'old-v028-' + str(index)
            newsnow = index % 3 == 0
            dry_run = index % 11 == 0
            stats = dict(provider='newsnow' if newsnow else 'gkg')
            if newsnow:
                report = persist_batch(root, root/run_key, dict(summary={}, sources=[], snapshots=[]))
                stats['newsnow'] = report
                expected += not dry_run
            session.add(RunRecord(run_key=run_key, profile_name='Historical synthetic run', status='succeeded',
                trigger_type='profile_test' if dry_run else 'manual', stats_json=json.dumps(stats), storage_path=str(root/run_key)))
        session.commit()
    from news2douyin.attention.lake import AttentionLake
    def forbidden(*_args, **_kwargs):
        raise AssertionError('Application construction synchronously scanned historical attention')
    with monkeypatch.context() as patcher:
        patcher.setattr(runtime, 'backfill_step', forbidden)
        patcher.setattr(AttentionLake, '__init__', forbidden)
        second = create_app(db_url=app.state.engine.url.render_as_string(hide_password=False), storage_root=str(root))
        second.state.engine.dispose()
    assert not (root/'newsnow'/'attention.duckdb').exists()
    assert runtime.backfill_step(app.state.engine, root, limit=7)
    with Session(app.state.engine) as session:
        assert session.get(AttentionMeta, 'backfill_cursor').value == '7'
        assert count(session, AttentionInbox) <= 7
    for _ in range(10):
        if not runtime.backfill_step(app.state.engine, root, limit=7):
            break
    else:
        raise AssertionError('Historical fixture failed to finish within bounded pages')
    with Session(app.state.engine) as session:
        assert count(session, AttentionInbox) == expected
        assert session.get(AttentionMeta, 'backfill_cursor').value == '55'
    assert not runtime.backfill_step(app.state.engine, root, limit=7)


def test_formal_all_source_failure_still_updates_observation_state(attention_case):
    app, client, root, upstream = attention_case
    name = profile(client, upstream)
    collect_observed_round(app, client, name, upstream); drain(app)
    tariff = topic_title(client, TARIFF)
    for source in upstream.attention_rounds[0]:
        upstream.set(source, (503, 'application/json', dict(error=True, message='QA all-source outage')))
    stamp = (upstream.observation_start+timedelta(minutes=20)).isoformat().replace('+00:00', 'Z')
    with patch('news2douyin.collect.providers.newsnow._now', return_value=stamp):
        response = client.post('/api/tasks/collect', json={'profile_name': name})
        assert response.status_code == 202
        task = app.state.tasks.claim(); assert task.task_id == response.json()['task_id']
        app.state.worker.execute(task)
    assert client.get('/api/tasks/' + task.task_id).json()['status'] == 'failed'
    with Session(app.state.engine) as session:
        assert count(session, AttentionInbox) == 2, 'Failed formal observations never reached the durable inbox'
    drain(app)
    after = topic_title(client, TARIFF)
    assert after['key'] == tariff['key'] and after['attention_score'] is None and after['rise_score'] is None
    assert after['platform_count'] == 0
    assert all(row['observation_state'] == 'source_failed' for row in after['summary']['platforms'])
