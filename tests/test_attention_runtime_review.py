"""Independent recovery, concurrency and editorial-guard release checks."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json

import pytest
from sqlalchemy import event as sql_event, func
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from news2douyin.attention import runtime, service
from news2douyin.attention.models import AttentionInbox, AttentionMeta, AttentionTopic, AttentionItem, AttentionSelection
from news2douyin.collect.newsnow_archive import persist_batch
from news2douyin.research.models import ResearchCase, ResearchSource
from news2douyin.storage.db import init_db, make_engine
from news2douyin.storage.models import Article, Event, RunRecord
from news2douyin.topics import service as topics
from news2douyin.topics.models import TopicWorkspace, TopicSource, TopicHandoff
from news2douyin.workbench.models import ContentTask
from test_postgres_integration import postgres_url


@pytest.fixture(params=['sqlite', 'postgres'])
def reviewed_engine(request, tmp_path):
    url = request.getfixturevalue('postgres_url') if request.param == 'postgres' else 'sqlite:///' + str(tmp_path / 'review.db')
    engine = make_engine(url); init_db(engine)
    yield engine, tmp_path / 'runs'
    engine.dispose()


def _report(root, key='archive-original'):
    return persist_batch(root, root / key, dict(summary={}, sources=[], snapshots=[]))


def _run(session, root, run_key, stats, **changes):
    session.add(RunRecord(run_key=run_key, profile_name='QA archived run', status='succeeded',
                          storage_path=str(root / run_key), stats_json=json.dumps(stats), **changes))


def _count(session, model):
    return session.exec(select(func.count()).select_from(model)).one()


def _topic(engine, count=1):
    stamp = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    with Session(engine) as session:
        session.add(AttentionTopic(key='att_review', title='港口工人罢工', generation='generation-1', last_seen=stamp))
        for index in range(count):
            session.add(AttentionItem(key='item_' + str(index), topic='att_review', source_id='weibo',
                title='港口工人罢工', excerpt='这只是榜单摘要，未取得正文。',
                url=f'https://news.example/{index}', last_seen=stamp))
        session.commit()


def test_backfill_restores_frozen_original_manifest_after_collection_resume(reviewed_engine):
    engine, root = reviewed_engine
    report = _report(root)
    with Session(engine) as session:
        _run(session, root, 'resumed-new-run-without-manifest', dict(provider='newsnow', newsnow=report))
        session.commit()
    assert runtime.backfill_step(engine, root)
    with Session(engine) as session:
        inbox = session.exec(select(AttentionInbox)).one()
        assert inbox.manifest_path == report['manifest_path']
        assert session.get(AttentionMeta, 'backfill_cursor').value == '1'


@pytest.mark.parametrize('bad_stats', [[], {'provider': 'newsnow', 'newsnow': 'invalid-old-report'}])
def test_one_malformed_old_stats_record_does_not_block_later_archives(reviewed_engine, bad_stats):
    engine, root = reviewed_engine
    report = _report(root)
    with Session(engine) as session:
        _run(session, root, 'malformed-old-run', bad_stats)
        _run(session, root, 'good-later-run', dict(provider='newsnow', newsnow=report))
        session.commit()
    assert runtime.backfill_step(engine, root)
    with Session(engine) as session:
        assert _count(session, AttentionInbox) == 1
        assert session.get(AttentionMeta, 'backfill_cursor').value == '2'
        assert session.get(AttentionMeta, 'backfill_error').value


def test_user_rescan_is_not_lost_to_an_already_prepared_backfill_page(reviewed_engine, monkeypatch):
    engine, root = reviewed_engine
    report = _report(root)
    with Session(engine) as session:
        _run(session, root, 'already-scanned', dict(provider='gkg'))
        _run(session, root, 'next-page', dict(provider='newsnow', newsnow=report))
        session.add(AttentionMeta(key='backfill_cursor', value='1'))
        session.commit()
    original = runtime.enqueue_manifest
    def enqueue_then_rescan(*args, **kwargs):
        result = original(*args, **kwargs)
        runtime.rescan(engine)
        return result
    monkeypatch.setattr(runtime, 'enqueue_manifest', enqueue_then_rescan)
    runtime.backfill_step(engine, root)
    with Session(engine) as session:
        assert session.get(AttentionMeta, 'backfill_cursor').value == '0', 'A stale page overwrote the requested full rescan'


def test_expired_lease_reclaim_fences_the_old_publisher(reviewed_engine):
    engine, root = reviewed_engine
    report = _report(root)
    key = runtime.enqueue_manifest(engine, root, report)
    first, second = runtime.AttentionWorker(engine, root), runtime.AttentionWorker(engine, root)
    job = first._claim()
    assert job and second._claim() is None
    with Session(engine) as session:
        row = session.get(AttentionInbox, key); row.lease_until = 0
        session.add(row); session.commit()
    recovered = second._claim()
    assert recovered and recovered['token'] != job['token']
    with pytest.raises(RuntimeError, match='租约已失效'):
        first.publish(job, {}, {})
    with Session(engine) as session:
        row = session.get(AttentionInbox, key)
        assert row.lease == recovered['token'] and row.state == 'running'
        assert _count(session, AttentionTopic) == 0


def test_process_ownership_skips_without_sql_or_lake_side_effects(reviewed_engine):
    engine, root = reviewed_engine
    from news2douyin.intake.runtime import ReceiverLock
    owner = ReceiverLock(root); owner.path = root / 'newsnow' / 'attention-worker.lock'
    owner.acquire()
    statements = []
    def audit(_connection, _cursor, statement, *_): statements.append(statement)
    sql_event.listen(engine, 'before_cursor_execute', audit)
    try:
        assert runtime.AttentionWorker(engine, root).step() is False
        assert statements == [] and not (root / 'newsnow' / 'attention.duckdb').exists()
    finally:
        sql_event.remove(engine, 'before_cursor_execute', audit)
        owner.release()


def test_database_publication_failure_does_not_write_an_error_feedback_loop(reviewed_engine, monkeypatch):
    engine, root = reviewed_engine
    report = _report(root)
    key = runtime.enqueue_manifest(engine, root, report)
    worker = runtime.AttentionWorker(engine, root)
    job = worker._claim()
    from news2douyin.attention.lake import AttentionLake
    def failed(*args, **kwargs): raise OperationalError('SELECT', {}, Exception('temporary unavailable'))
    monkeypatch.setattr(AttentionLake, 'ingest_manifest', failed)
    statements = []
    def audit(_connection, _cursor, statement, *_): statements.append(statement)
    sql_event.listen(engine, 'before_cursor_execute', audit)
    try:
        worker._process(job)
    finally:
        sql_event.remove(engine, 'before_cursor_execute', audit)
    assert statements == []
    with Session(engine) as session:
        row = session.get(AttentionInbox, key)
        assert row.state == 'running' and row.lease == job['token'] and row.error == ''


def test_many_leads_remain_available_but_default_research_handoff_is_executable(reviewed_engine):
    engine, _root = reviewed_engine
    _topic(engine, count=15)
    created = service.select_topic(engine, 'att_review', 'review-many-leads-request')
    with Session(engine) as session:
        workspace = session.get(TopicWorkspace, created['key'])
        assert len(json.loads(workspace.selected_json)) == 12
        assert _count(session, TopicSource) == 15
        snapshot = json.loads(session.get(AttentionSelection, created['key']).snapshot_json)
        assert snapshot['selection']['selected_sources'] == 12
        revision = workspace.revision
        assert all(json.loads(row.document_json)['requires_original_text'] for row in session.exec(select(TopicSource)))
    class Recorder:
        def __init__(self): self.calls = []
        def enqueue(self, *args, **kwargs): self.calls.append((args, kwargs))
    recorder = Recorder()
    result = topics.handoff(engine, recorder, created['key'], dict(revision=revision,
        request_id='review-materials-handoff', action='materials', write_script=False, find_images=False))
    assert result['case_id'] and not result.get('pending') and len(recorder.calls) == 1
    with Session(engine) as session:
        case = session.get(ResearchCase, result['case_id'])
        assert len(json.loads(case.article_keys_json)) == 12
        sources = session.exec(select(ResearchSource).where(ResearchSource.case_id == result['case_id'])).all()
        assert sources and all(row.kind == 'search_snippet' for row in sources)
        assert all(json.loads(row.provenance_json).get('requires_original_text') for row in sources)
        assert _count(session, Article) == _count(session, Event) == 0


def test_parallel_selection_is_idempotent_and_rollback_keeps_no_orphan_task(reviewed_engine):
    engine, _root = reviewed_engine
    _topic(engine)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: service.select_topic(engine, 'att_review', 'review-same-selection'), range(2)))
    assert results[0] == results[1]
    with Session(engine) as session:
        assert _count(session, AttentionSelection) == _count(session, TopicWorkspace) == _count(session, ContentTask) == 1
    def fail(_conn, _cursor, statement, *_):
        if statement.lower().lstrip().startswith('insert into attentionselection'):
            raise ValueError('forced publication rollback')
    sql_event.listen(engine, 'before_cursor_execute', fail)
    try:
        with pytest.raises(ValueError, match='forced publication rollback'):
            service.select_topic(engine, 'att_review', 'review-roll-back-selection')
    finally:
        sql_event.remove(engine, 'before_cursor_execute', fail)
    with Session(engine) as session:
        assert _count(session, AttentionSelection) == _count(session, TopicWorkspace) == _count(session, ContentTask) == 1
        assert _count(session, Article) == _count(session, Event) == 0


def test_metadata_only_short_script_is_blocked_before_any_handoff_is_frozen(reviewed_engine):
    engine, _root = reviewed_engine
    _topic(engine)
    created = service.select_topic(engine, 'att_review', 'review-short-guard-request')
    with pytest.raises(ValueError, match='只是线索'):
        topics.handoff(engine, object(), created['key'], dict(revision=1, request_id='review-short-handoff', action='short'))
    with Session(engine) as session:
        assert _count(session, TopicHandoff) == 0
