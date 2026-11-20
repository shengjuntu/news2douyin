"""Durable event-vector work on disposable SQLite and real PostgreSQL.

No field deployment or model is contacted. Most cases use a deterministic
adapter; one uses the actual embedding client and a loopback HTTP fixture.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import hashlib
import json
from threading import Barrier
from time import time

import pytest
from sqlalchemy import create_engine, func
from sqlmodel import SQLModel, Session, select

from news2douyin.candidates.promotion import stage_event
from news2douyin.event_embeddings import queue, runtime
from news2douyin.event_embeddings.documents import digest, document, profile
from news2douyin.event_embeddings.models import EventEmbeddingBackfill, EventEmbeddingJob, EventEmbeddingRecord
from news2douyin.events import workbench as work
from news2douyin.llm.client.embedding import EmbeddingClient, EmbeddingError
from news2douyin.storage.db import init_db, load_models, make_engine
from news2douyin.storage.models import Event, EventState, NewsWriteLock
from test_embedding_client import endpoint
from test_postgres_integration import postgres_url
from test_postgres_migration import target as migration_target


class Adapter:
    base_url = 'http://127.0.0.1:9/v1'
    model = 'fixture-model'
    dimensions = 3

    def __init__(self, engine):
        self.engine, self.calls, self.callback, self.reply = engine, [], None, [3., 4., 0.]

    def resolve_model(self):
        assert self.engine.pool.checkedout() == 0
        return self.model

    def get_embedding(self, texts):
        assert self.engine.pool.checkedout() == 0, 'Outbound encoding retained a SQL connection'
        values = [texts] if isinstance(texts, str) else list(texts)
        self.calls.append(values)
        if self.callback:
            self.callback(values)
        if isinstance(self.reply, Exception):
            raise self.reply
        return [list(self.reply) for _ in values]


@pytest.fixture(params=['sqlite', 'postgres'])
def vector_case(request, tmp_path, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_EVENT_EMBEDDING_ENABLED', '1')
    monkeypatch.setenv('EMBEDDING_BASE_URL', Adapter.base_url)
    monkeypatch.setenv('EMBEDDING_MODEL', Adapter.model)
    monkeypatch.setenv('EMBEDDING_DIMENSIONS', '3')
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    monkeypatch.setenv('no_proxy', '127.0.0.1,localhost')
    url = request.getfixturevalue('postgres_url') if request.param == 'postgres' else 'sqlite:///' + str(tmp_path/'vectors.db')
    engine = make_engine(url); init_db(engine)
    adapter = Adapter(engine)
    monkeypatch.setattr(runtime, 'vector_client', lambda: adapter)
    try:
        yield engine, adapter
    finally:
        engine.dispose()


def create(engine, title='港口恢复货运', summary='港口管理部门确认货运通道重新开放。'):
    with Session(engine) as session:
        value = work.create_event(session, dict(title=title, summary=summary, topic='交通'))
        row = session.exec(select(Event).where(Event.event_key == value['event_key'])).one()
        row.first_seen_at = row.last_seen_at = '2026-11-04T12:00:00Z'
        session.add(row); session.commit()
    return value['event_key']


def edit(engine, key, *, title=None, summary=None, topic=None):
    with Session(engine) as session:
        value = work.workspace(session, key)
        return work.edit_event(session, key, dict(expected_version=value['version'],
            title=value['title'] if title is None else title,
            summary=value['summary'] if summary is None else summary,
            topic=value['topic'] if topic is None else topic))


def drain(engine, maximum=30):
    worker = runtime.EventEmbeddingWorker(engine)
    for _ in range(maximum):
        if not worker.step():
            return worker
    raise AssertionError('Bounded fixture queue did not settle')


def job_state(engine, key):
    with Session(engine) as session:
        row = session.get(EventEmbeddingJob, key)
        return row.model_dump() if row else None


def record(engine, key):
    with Session(engine) as session:
        row = session.get(EventEmbeddingRecord, key)
        return row.model_dump() if row else None


def test_curated_create_and_edit_are_durable_and_same_document_avoids_remote_repeat(vector_case):
    engine, adapter = vector_case
    key = create(engine)
    assert job_state(engine, key)['state'] == 'pending' and adapter.calls == []
    assert record(engine, key) is None
    drain(engine)
    original = record(engine, key)
    assert json.loads(original['embedding']) == pytest.approx([.6, .8, 0.])
    assert job_state(engine, key)['state'] == 'ready' and len(adapter.calls) == 1
    assert not runtime.EventEmbeddingWorker(engine).step()
    edit(engine, key, topic='交通基础设施')
    assert job_state(engine, key)['state'] == 'pending'
    drain(engine)
    assert len(adapter.calls) == 1 and record(engine, key) == original
    edit(engine, key, summary='港口管理部门修正了开放时间，部分通道仍在检修。')
    assert job_state(engine, key)['state'] == 'pending'
    drain(engine)
    assert len(adapter.calls) == 2 and record(engine, key)['content_hash'] != original['content_hash']


def test_real_http_encoding_runs_without_an_open_sql_connection(vector_case, endpoint, monkeypatch):
    engine, adapter = vector_case
    endpoint.dimensions = 3
    client = EmbeddingClient('qa-only', endpoint.url, 'fixture-model', dimensions=3)
    monkeypatch.setattr(runtime, 'vector_client', lambda: client)
    original = client.get_embedding
    def encode(texts):
        assert engine.pool.checkedout() == 0
        with Session(engine) as session:
            assert session.exec(select(EventEmbeddingJob.state)).one() == 'running'
            session.add(Event(event_key='independent-write', event_title='编码期间的独立写入'))
            session.commit()
        return original(texts)
    monkeypatch.setattr(client, 'get_embedding', encode)
    key = create(engine)
    drain(engine)
    assert job_state(engine, key)['state'] == 'ready'
    assert [(method, path) for method, path, *_ in endpoint.calls] == [('POST', '/v1/embeddings')]
    with Session(engine) as session:
        assert session.exec(select(Event).where(Event.event_key == 'independent-write')).one()


def test_historical_backfill_is_bounded_restartable_and_pins_its_id_ceiling(vector_case, monkeypatch):
    engine, adapter = vector_case
    monkeypatch.setattr(queue, 'BACKFILL_PAGE', 2); monkeypatch.setattr(runtime, 'BATCH_SIZE', 1)
    with Session(engine) as session:
        for key in ('old-a', 'old-b', 'old-c', 'old-merged'):
            session.add(Event(event_key=key, event_title='历史货运事件 '+key, first_seen_at='2026-11-04T12:00:00Z'))
        session.add(Event(event_key='outside', event_title='日期范围外事件', first_seen_at='2026-10-01T00:00:00Z'))
        session.add(EventState(event_key='old-merged', merged_into='old-a')); session.commit()
    worker = runtime.EventEmbeddingWorker(engine)
    with Session(engine) as session:
        started = worker.request_backfill(session, date(2026, 11, 1), date(2026, 11, 10))
        repeated = worker.request_backfill(session, date(2026, 11, 1), date(2026, 11, 10))
        assert repeated['state'] == started['state'] == 'scanning'
        with pytest.raises(ValueError, match='已有补算'):
            worker.request_backfill(session, date(2026, 11, 2), date(2026, 11, 10))
        session.rollback()
    assert worker.step()
    with Session(engine) as session:
        batch = session.get(EventEmbeddingBackfill, 1)
        assert batch.scanned == 2 and batch.cursor > 0
        cursor, ceiling = batch.cursor, batch.ceiling
        session.add(Event(event_key='after-request', event_title='请求后新增的旧日期事件', first_seen_at='2026-11-04T12:00:00Z'))
        session.commit()
    drain(engine)
    with Session(engine) as session:
        batch = session.get(EventEmbeddingBackfill, 1)
        assert batch.cursor >= cursor and batch.ceiling == ceiling and batch.scanned == 3
        assert batch.state == 'queued'
        assert {row.event_key for row in session.exec(select(EventEmbeddingRecord))} == {'old-a', 'old-b', 'old-c'}
        assert queue.counts(session, batch.batch_id)['ready'] == 3
        assert session.get(EventEmbeddingJob, 'after-request') is None
    assert len(adapter.calls) == 3


@pytest.mark.parametrize('failure', ['service_error', 'zero_vector'])
def test_failed_response_has_persisted_backoff_limit_and_explicit_historical_retry(vector_case, failure):
    engine, adapter = vector_case
    key = create(engine)
    adapter.reply = EmbeddingError('合成向量服务暂不可用') if failure == 'service_error' else [0., 0., 0.]
    for attempt in range(1, 5):
        worker = runtime.EventEmbeddingWorker(engine)
        assert worker.step()
        job = job_state(engine, key)
        assert job['attempts'] == attempt and job['error'] and record(engine, key) is None
        assert not runtime.EventEmbeddingWorker(engine).step()
        if attempt < 4:
            assert job['state'] == 'pending' and job['available_at'] > time()
            with Session(engine) as session:
                row = session.get(EventEmbeddingJob, key); row.available_at = 0; session.add(row); session.commit()
        else:
            assert job['state'] == 'failed'
    adapter.reply = [3., 4., 0.]
    with Session(engine) as session:
        runtime.EventEmbeddingWorker(engine).request_backfill(session, date(2026, 11, 1), date(2026, 11, 10))
    drain(engine)
    assert job_state(engine, key)['state'] == 'ready' and job_state(engine, key)['attempts'] == 0
    assert record(engine, key) and len(adapter.calls) == 5


def test_inflight_edit_invalidates_old_remote_result_and_new_input_is_published(vector_case):
    engine, adapter = vector_case
    key = create(engine)
    adapter.callback = lambda _texts: edit(engine, key, summary='更新确认：货运恢复时间推迟，港口继续维修。')
    assert runtime.EventEmbeddingWorker(engine).step()
    assert record(engine, key) is None and job_state(engine, key)['state'] == 'pending'
    adapter.callback = None
    drain(engine)
    with Session(engine) as session:
        expected = digest(document(session, key))
    assert record(engine, key)['content_hash'] == expected and len(adapter.calls) == 2
    assert '推迟' in adapter.calls[-1][0]


def test_merge_discards_existing_vector_and_invalidates_inflight_source_lease(vector_case):
    engine, adapter = vector_case
    source, target = create(engine, title='旧港口事件'), create(engine, title='合并目标事件')
    drain(engine)
    assert record(engine, source) and record(engine, target)
    edit(engine, source, summary='等待合并的新增进展。')
    def merge(_texts):
        with Session(engine) as session:
            src, dst = work.workspace(session, source), work.workspace(session, target)
            work.merge(session, source, dict(expected_version=src['version'], target_event_key=target,
                target_expected_version=dst['version'], note='合成测试中的人工合并'))
    adapter.callback = merge
    assert runtime.EventEmbeddingWorker(engine).step()
    assert record(engine, source) is None and job_state(engine, source)['state'] == 'skipped'
    adapter.callback = None
    drain(engine)
    assert record(engine, source) is None and record(engine, target)


def test_reclaimed_lease_rejects_old_ack_and_parallel_claims_have_distinct_owners(vector_case, monkeypatch):
    engine, adapter = vector_case
    monkeypatch.setattr(runtime, 'BATCH_SIZE', 1)
    first, second = create(engine, title='第一条'), create(engine, title='第二条')
    barrier = Barrier(2)
    def claim():
        barrier.wait(); return runtime.claim_jobs(engine)
    with ThreadPoolExecutor(2) as pool:
        batches = list(pool.map(lambda _index: claim(), range(2)))
    claims = [value for batch in batches for value in batch]
    assert {value.key for value in claims} == {first, second}
    assert len({value.lease for value in claims}) == 2
    stale = next(value for value in claims if value.key == first)
    with Session(engine) as session:
        row = session.get(EventEmbeddingJob, first); row.available_at = 0; session.add(row); session.commit()
    replacement = runtime.claim_jobs(engine)
    assert len(replacement) == 1 and replacement[0].key == first and replacement[0].lease != stale.lease
    runtime._finish(engine, [stale], model=adapter.model, current_profile=profile(adapter),
        encoded={first: ('obsolete', [.6, .8, 0.])})
    assert record(engine, first) is None and job_state(engine, first)['lease'] == replacement[0].lease
    runtime._finish(engine, replacement, model=adapter.model, current_profile=profile(adapter),
        encoded={first: ('replacement', [.6, .8, 0.])})
    assert record(engine, first)['content_hash'] == 'replacement'


def test_post_encoding_sql_failure_recovers_without_duplicate_record(vector_case, monkeypatch):
    engine, adapter = vector_case
    key = create(engine)
    def failed(*_args, **_kwargs): raise RuntimeError('合成的提交阶段故障')
    with monkeypatch.context() as patcher:
        patcher.setattr(runtime, 'save_record', failed)
        with pytest.raises(RuntimeError, match='提交阶段'):
            runtime.EventEmbeddingWorker(engine).step()
    assert record(engine, key) is None and job_state(engine, key)['state'] == 'running'
    with Session(engine) as session:
        row = session.get(EventEmbeddingJob, key); row.available_at = 0; session.add(row); session.commit()
    drain(engine)
    with Session(engine) as session:
        assert session.exec(select(func.count()).select_from(EventEmbeddingRecord)).one() == 1
    assert len(adapter.calls) == 2 and job_state(engine, key)['state'] == 'ready'


def test_profile_change_and_paused_edit_do_not_leave_an_old_vector_searchable(vector_case, monkeypatch):
    engine, adapter = vector_case
    key = create(engine); drain(engine)
    before = record(engine, key)
    adapter.model = 'fixture-model-v2'
    with Session(engine) as session:
        runtime.EventEmbeddingWorker(engine).request_backfill(session, date(2026, 11, 1), date(2026, 11, 10))
    drain(engine)
    assert record(engine, key)['profile'] != before['profile'] and len(adapter.calls) == 2
    monkeypatch.setenv('NEWS2DOUYIN_EVENT_EMBEDDING_ENABLED', '0')
    edit(engine, key, summary='暂停后台后保存的新内容。')
    assert job_state(engine, key)['state'] == 'pending' and not runtime.EventEmbeddingWorker(engine).step()
    from news2douyin.event_embeddings.search import find_similar
    with Session(engine) as session:
        assert find_similar(session, profile=profile(adapter), vector=[.6, .8, 0.],
            lower='2026-11-01T00:00:00', upper='2026-11-11T00:00:00') == []


def test_event_transaction_rollback_also_rolls_back_automatic_enqueue(vector_case):
    engine, adapter = vector_case
    with Session(engine) as session:
        stage_event(session, key='rolled-back-event', title='尚未提交的人工事件')
        session.flush(); session.rollback()
    with Session(engine) as session:
        assert session.exec(select(Event).where(Event.event_key == 'rolled-back-event')).first() is None
        assert session.get(EventEmbeddingJob, 'rolled-back-event') is None
    assert adapter.calls == []


def test_sqlite_vector_text_and_durable_history_queue_migrate_without_reencoding(tmp_path, migration_target):
    from news2douyin.storage.migrate import migrate_sqlite
    path = tmp_path/'offline-vectors.sqlite'
    load_models()
    source = create_engine('sqlite:///' + str(path))
    SQLModel.metadata.create_all(source)
    serialized = ' [0.6, 0.8, -0.0] \n'
    with Session(source) as session:
        session.add(NewsWriteLock(name='collection'))
        session.add(Event(id=7, event_key='migration-vector', event_title='待继续处理的事件'))
        session.add(EventEmbeddingJob(event_key='migration-vector', state='pending', attempts=2,
            batch_id='migration-batch', available_at=0))
        session.add(EventEmbeddingRecord(event_key='migration-vector', profile='frozen-profile',
            model='frozen-model', content_hash='frozen-input', embedding=serialized))
        session.add(EventEmbeddingBackfill(batch_id='migration-batch', start_date='2026-11-01',
            end_date='2026-11-10', lower='2026-10-31T16:00:00', upper='2026-11-10T16:00:00',
            cursor=4, ceiling=7, scanned=4, state='scanning'))
        session.commit()
    source.dispose()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    report = migrate_sqlite(path, migration_target, chunk_size=2, source_stopped=True)
    assert report['status'] == 'ready' and hashlib.sha256(path.read_bytes()).hexdigest() == before
    tables = {row['table']: row for row in report['tables']}
    for name in ('eventembeddingjob', 'eventembeddingrecord', 'eventembeddingbackfill'):
        assert tables[name]['target_rows'] == 1
        assert tables[name]['source_sha256'] == tables[name]['target_sha256']
    target = make_engine(migration_target)
    try:
        init_db(target)
        with Session(target) as session:
            assert session.get(EventEmbeddingRecord, 'migration-vector').embedding == serialized
            row = session.get(EventEmbeddingJob, 'migration-vector')
            assert row.state == 'pending' and row.attempts == 2 and row.batch_id == 'migration-batch'
            history = session.get(EventEmbeddingBackfill, 1)
            assert (history.cursor, history.ceiling, history.scanned, history.state) == (4, 7, 4, 'scanning')
    finally:
        target.dispose()
