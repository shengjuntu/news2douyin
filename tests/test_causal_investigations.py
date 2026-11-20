"""Temporal and durable investigation contracts on SQLite and actual PG.

The independent model and retrieval boundaries are controlled fixtures. No live
model, embedding endpoint, source site, or deployed database is contacted.
"""
from copy import deepcopy
from datetime import datetime, timezone
from time import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sql_event, func
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from news2douyin.causal import chronology, model, retrieval, service
from news2douyin.causal.models import CausalInvestigation, CausalJob, CausalNode
from news2douyin.causal.runtime import CausalWorker
from news2douyin.server.app import create_app
from news2douyin.storage.models import Event, EventMoment, EventState
from sql_readonly_audit import is_read_only_execution
from test_postgres_integration import postgres_url


BASE = '/api/causal/investigations'
TARGET = '港口货运为何出现持续延误'
REAL_RETRIEVE = retrieval.retrieve


def candidate(ref, *, text=None, occurred='2026-11-08T08:00:00Z',
              published='2026-11-08T09:00:00Z', score=.8, sources=None):
    text = text or '前期线索 ' + ref
    return dict(ref=ref, text=text, event_key='', occurred_at=occurred, published_at=published,
        sources=deepcopy(sources) if sources is not None else [dict(url='https://qa.example/'+ref,
            title='保存的来源 '+ref, quote=text, published_at=published, kind='local_article')],
        fixture_score=score)


class ControlledBoundaries:
    def __init__(self, engine):
        self.engine = engine
        self.calls, self.seen_candidates = [], []
        self.results = []
        self.by_target = {}
        self.failure = ''

    def free(self, boundary):
        assert self.engine.pool.checkedout() == 0, boundary+' retained a SQL connection'

    def configure(self):
        self.free('configure'); self.calls.append(('configure',))
        return object()

    def propose(self, settings, target):
        self.free('model.propose'); self.calls.append(('propose', target))
        if self.failure == 'model':
            raise model.CausalModelError('合成归因模型暂不可用，请明确重试')
        return [target]

    def retrieve(self, engine, queries, *, lower, upper, exclude):
        self.free('retrieval/embedding'); assert engine is self.engine
        self.calls.append(('retrieve', queries[0], lower, upper, set(exclude)))
        if self.failure == 'lookup':
            raise RuntimeError('private-source-body and private-token must not reach the browser')
        return [deepcopy(row) for row in self.by_target.get(queries[0], self.results)
                if row['ref'] not in exclude]

    def rank(self, settings, target, candidates):
        self.free('model.rank'); self.calls.append(('rank', target))
        self.seen_candidates.append(deepcopy(candidates))
        return [dict(ref=row['ref'], reason='候选解释，因果待核：保存资料存在时间先后联系。',
                     score=row.get('fixture_score', .8)) for row in candidates]


@pytest.fixture(params=['sqlite', 'postgres'])
def causal_case(request, tmp_path, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_TIMEZONE', 'UTC')
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', '')
    monkeypatch.setenv('NEWS2DOUYIN_ENV_FILE', str(tmp_path/'absent.env'))
    url = request.getfixturevalue('postgres_url') if request.param == 'postgres' else 'sqlite:///'+str(tmp_path/'causal.db')
    app = create_app(db_url=url, storage_root=str(tmp_path/'runs'))
    fake = ControlledBoundaries(app.state.engine)
    monkeypatch.setattr(model, 'configure', fake.configure)
    monkeypatch.setattr(model, 'propose', fake.propose)
    monkeypatch.setattr(model, 'rank', fake.rank)
    monkeypatch.setattr(retrieval, 'retrieve', fake.retrieve)
    from news2douyin.server import causal as routes
    monkeypatch.setattr(routes, 'configure', fake.configure)
    client = TestClient(app)  # Lifespan off: queue work advances explicitly.
    try:
        yield app, client, fake
    finally:
        client.close(); app.state.engine.dispose()


def create(client, *, text=TARGET, event_at='2026-11-10', event_key='', automatic=False):
    response = client.post(BASE, json=dict(text=text, event_at=event_at, event_key=event_key, automatic=automatic))
    assert response.status_code == 202, response.text
    assert response.headers['cache-control'] == 'no-store'
    return response.json()


def detail(client, case):
    response = client.get(BASE+'/'+case['key'])
    assert response.status_code == 200, response.text
    return response.json()


def nodes(app, case_key):
    with Session(app.state.engine) as session:
        return [row.model_dump() for row in session.exec(select(CausalNode).where(CausalNode.case_key == case_key))]


def drain(app, maximum=30):
    worker = CausalWorker(app.state.engine)
    for number in range(maximum):
        if not worker.step():
            return number
    raise AssertionError('Synthetic causal queue did not settle within bounded steps')


def test_day_precision_keeps_same_day_unknown_and_excludes_late_public_sources(causal_case):
    app, client, fake = causal_case
    fake.results = [
        candidate('prior'),
        candidate('same-day', occurred='2026-11-10T08:00:00Z', published='2026-11-10T09:00:00Z', score=.99),
        candidate('same-day-precision', occurred='2026-11-10', published='2026-11-09T09:00:00Z'),
        candidate('earlier-but-late-report', published='2026-11-12T09:00:00Z'),
        candidate('future-occurrence', occurred='2026-11-11T00:00:00Z'),
        candidate('missing-publication', published=''),
        candidate('mixed-source', sources=[
            dict(url='https://qa.example/early', title='先前报道', quote='先前来源的保存资料。', published_at='2026-11-09T09:00:00Z'),
            dict(url='https://qa.example/late', title='事后报道', quote='事后报道不能用于此前解释。', published_at='2026-11-12T09:00:00Z')]),
    ]
    case = create(client); assert drain(app) == 1
    rows = {row['ref']: row for row in nodes(app, case['key']) if row['parent_key']}
    assert set(rows) == {'prior', 'same-day', 'same-day-precision', 'missing-publication', 'mixed-source'}
    assert rows['prior']['time_status'] == rows['mixed-source']['time_status'] == 'earlier'
    assert all(rows[key]['time_status'] == 'unknown' for key in ('same-day', 'same-day-precision', 'missing-publication'))
    ranked = {row['ref']: row for row in fake.seen_candidates[-1]}
    assert [source['url'] for source in ranked['mixed-source']['sources']] == ['https://qa.example/early']
    recall = next(call for call in fake.calls if call[0] == 'retrieve')
    assert recall[3] == '2026-11-11T00:00:00'


def test_exact_timestamp_uses_strict_precedence_and_preserves_uncertain_day_source(causal_case):
    app, client, fake = causal_case
    fake.results = [candidate('before', occurred='2026-11-10T11:59:58Z', published='2026-11-10T11:59:59Z'),
        candidate('at-cutoff', occurred='2026-11-10T12:00:00Z', published='2026-11-10T11:00:00Z'),
        candidate('published-at-cutoff', occurred='2026-11-10T11:00:00Z', published='2026-11-10T12:00:00Z'),
        candidate('day-only-source', occurred='2026-11-09', published='2026-11-10')]
    case = create(client, event_at='2026-11-10T12:00:00Z'); drain(app)
    rows = {row['ref']: row for row in nodes(app, case['key']) if row['parent_key']}
    assert set(rows) == {'before', 'day-only-source'}
    assert rows['before']['time_status'] == 'earlier' and rows['day-only-source']['time_status'] == 'unknown'


def test_automatic_beam_uses_three_global_branches_and_stops_at_depth_two(causal_case):
    app, client, fake = causal_case
    fake.by_target[TARGET] = [candidate('root-'+str(i), text='一级前因 '+str(i), score=1-i*.1) for i in range(5)]
    fake.by_target[TARGET].append(candidate('uncertain-top-score', text='时间不明的一级线索', published='', score=1.))
    for i in range(5):
        fake.by_target['一级前因 '+str(i)] = [candidate('leaf-'+str(i)+'-'+str(j),
            occurred='2026-11-01T08:00:00Z', published='2026-11-01T09:00:00Z') for j in range(4)]
    case = create(client, automatic=True)
    assert drain(app) == 4, 'Automatic expansion multiplied the three-branch beam across descendants'
    rows = nodes(app, case['key'])
    assert max(row['depth'] for row in rows) == 2
    assert {row['ref'] for row in rows if row['beam_selected']} == {'root-0', 'root-1', 'root-2'}
    investigated = [call[1] for call in fake.calls if call[0] == 'propose']
    assert len(investigated) == 4 and set(investigated) == {TARGET, '一级前因 0', '一级前因 1', '一级前因 2'}
    with Session(app.state.engine) as session:
        assert len(session.exec(select(CausalJob).where(CausalJob.case_key == case['key'])).all()) == 4
    assert not detail(client, case)['working']


def test_manual_refresh_updates_existing_child_and_does_not_duplicate_tree_nodes(causal_case):
    app, client, fake = causal_case
    fake.results = [candidate('a'), candidate('b')]
    case = create(client); drain(app)
    before = {row['ref']: row for row in nodes(app, case['key']) if row['parent_key']}
    root = next(row for row in nodes(app, case['key']) if not row['parent_key'])
    fake.results = [candidate('a', text='更新后可核对的前期线索', score=.95), candidate('b'), candidate('c')]
    first = client.post(BASE+'/'+case['key']+'/expand', json=dict(node_key=root['key'], automatic=False))
    repeat = client.post(BASE+'/'+case['key']+'/expand', json=dict(node_key=root['key'], automatic=False))
    assert first.status_code == repeat.status_code == 202
    assert drain(app) == 1
    after = {row['ref']: row for row in nodes(app, case['key']) if row['parent_key']}
    assert set(after) == {'a', 'b', 'c'} and after['a']['text'] == '更新后可核对的前期线索'
    assert after['a']['key'] == before['a']['key'] and after['b']['key'] == before['b']['key']
    assert len(nodes(app, case['key'])) == 4


def test_durable_lease_reclaim_fences_old_results_and_old_error_ack(causal_case):
    app, client, fake = causal_case
    case = create(client)
    first = service.claim(app.state.engine)
    assert first and service.claim(app.state.engine) is None
    with Session(app.state.engine) as session:
        row = session.get(CausalJob, first.node_key); row.due_at = time()-1; session.add(row); session.commit()
    second = service.claim(app.state.engine)
    assert second and second.lease != first.lease
    result = dict(candidate('saved'), time_status='earlier', score=.8, reason='候选解释，因果待核。')
    service.complete(app.state.engine, first, [result])
    assert len(nodes(app, case['key'])) == 1
    service.complete(app.state.engine, second, [result])
    service.complete(app.state.engine, first, error='过期租约不能覆盖新结果')
    data = detail(client, case)
    assert len(data['nodes']) == 2 and not data['working']
    assert next(row for row in data['nodes'] if row['depth'] == 0)['job_state'] == 'ready'
    assert not next(row for row in data['nodes'] if row['depth'] == 0)['job_error']


@pytest.mark.parametrize('failure', ['model', 'lookup'])
def test_failed_work_is_durable_safe_and_can_be_explicitly_retried(causal_case, failure):
    app, client, fake = causal_case
    fake.failure = failure
    case = create(client); assert drain(app) == 1
    failed = detail(client, case); root = failed['nodes'][0]
    assert not failed['working'] and root['job_state'] == 'failed' and root['job_error']
    assert 'private-token' not in str(failed) and 'private-source-body' not in str(failed)
    fake.failure = ''; fake.results = [candidate('retry-result')]
    response = client.post(BASE+'/'+case['key']+'/expand', json=dict(node_key=root['key'], automatic=False))
    assert response.status_code == 202
    assert drain(app) == 1
    done = detail(client, case)
    assert len(done['nodes']) == 2 and all(not row['job_error'] for row in done['nodes'])


def test_publish_failure_leaves_replayable_lease_and_rolls_back_children(causal_case):
    app, client, fake = causal_case
    fake.results = [candidate('atomic-result')]
    case = create(client)
    def failed_insert(_connection, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().lower().startswith('insert into causalnode'):
            raise OperationalError(statement, parameters, RuntimeError('synthetic publish failure'))
    sql_event.listen(app.state.engine, 'before_cursor_execute', failed_insert)
    try:
        with pytest.raises(OperationalError):
            CausalWorker(app.state.engine).step()
    finally:
        sql_event.remove(app.state.engine, 'before_cursor_execute', failed_insert)
    assert len(nodes(app, case['key'])) == 1
    with Session(app.state.engine) as session:
        row = session.exec(select(CausalJob).where(CausalJob.case_key == case['key'])).one()
        assert row.state == 'running' and row.lease
        row.due_at = 0; session.add(row); session.commit()
    assert drain(app) == 1 and len(nodes(app, case['key'])) == 2


def test_api_validation_scope_guards_and_gets_do_not_invoke_or_drain_work(causal_case):
    app, client, fake = causal_case
    assert client.post(BASE, json=dict(text=TARGET), headers={'Origin':'https://other.example'}).status_code == 403
    assert client.post(BASE, json=dict(text='  ')).status_code == 422
    assert client.post(BASE, json=dict(text=TARGET, event_at='2026-02-30')).status_code == 422
    case = create(client); another = create(client, text='另一次独立调查')
    root = case['nodes'][0]
    wrong_scope = client.post(BASE+'/'+another['key']+'/expand', json=dict(node_key=root['key'], automatic=False))
    assert wrong_scope.status_code == 404
    assert client.post(BASE+'/'+case['key']+'/expand', json=dict(node_key='bad')).status_code == 422
    assert client.post(BASE+'/'+case['key']+'/expand', json=dict(node_key=root['key']),
        headers={'Origin':'https://other.example'}).status_code == 403
    before = len(fake.calls); statements = []
    def audit(_connection, _cursor, sql, _parameters, context, _many):
        statements.append((sql, is_read_only_execution(sql, context)))
    sql_event.listen(app.state.engine, 'before_cursor_execute', audit)
    try:
        for url in (BASE, BASE+'/'+case['key'], '/radar/causes?investigation='+case['key']):
            response = client.get(url); assert response.status_code == 200, response.text
        assert statements and all(read for _sql, read in statements)
        assert len(fake.calls) == before
    finally:
        sql_event.remove(app.state.engine, 'before_cursor_execute', audit)
    assert detail(client, case)['nodes'][0]['job_state'] == 'pending'
    assert client.get(BASE+'/ci_missing').status_code == 404


def test_reviewed_event_seed_rejects_merged_root_and_path_exclusions_survive_expansion(causal_case):
    app, client, fake = causal_case
    with Session(app.state.engine) as session:
        session.add(Event(event_key='curated-root', event_title=TARGET, summary='保存的整理事件摘要'))
        session.add(EventMoment(moment_key='reviewed-date', event_key='curated-root', title='核对的进展日期',
            time_kind='occurred', date_start='2026-11-10', certainty='exact', reviewed=True))
        session.add(Event(event_key='old-merged', event_title='已合并事件'))
        session.add(EventState(event_key='old-merged', merged_into='curated-root')); session.commit()
    page = client.get('/radar/causes?event_key=curated-root')
    assert page.status_code == 200 and TARGET in page.text and '2026-11-10' in page.text
    assert client.get('/radar/causes?event_key=old-merged').status_code == 404
    assert client.post(BASE, json=dict(text='已合并事件', event_key='old-merged')).status_code == 404
    fake.results = [candidate('curated-root'), candidate('cause-a', text='中间前因')]
    case = create(client, event_at='', event_key='curated-root'); assert case['event_at'] == '2026-11-10'
    drain(app)
    first = next(row for row in nodes(app, case['key']) if row['parent_key'])
    fake.results = [candidate('curated-root'), candidate('cause-a'), candidate('cause-b',
        occurred='2026-11-01T08:00:00Z', published='2026-11-01T09:00:00Z')]
    response = client.post(BASE+'/'+case['key']+'/expand', json=dict(node_key=first['key'], automatic=False))
    assert response.status_code == 202; drain(app)
    recall = next(call for call in reversed(fake.calls) if call[0] == 'retrieve')
    assert recall[4] == {'curated-root', 'cause-a'}
    assert {row['ref'] for row in nodes(app, case['key'])} == {'curated-root', 'cause-a', 'cause-b'}


def test_manual_expansion_waits_for_current_branch_and_respects_tree_limits(causal_case, monkeypatch):
    app, client, fake = causal_case
    fake.results = [candidate('a'), candidate('b')]
    case = create(client); drain(app)
    children = [row for row in nodes(app, case['key']) if row['parent_key']]
    url = BASE+'/'+case['key']+'/expand'
    assert client.post(url, json=dict(node_key=children[0]['key'])).status_code == 202
    assert client.post(url, json=dict(node_key=children[1]['key'])).status_code == 409
    assert client.post(url, json=dict(node_key=children[0]['key'])).status_code == 202
    drain(app)
    with Session(app.state.engine) as session:
        row = session.get(CausalNode, children[0]['key']); row.depth = service.MAX_DEPTH
        session.add(row); session.commit()
    assert client.post(url, json=dict(node_key=children[0]['key'])).status_code == 409
    monkeypatch.setattr(service, 'MAX_TREE_NODES', len(nodes(app, case['key'])))
    assert client.post(url, json=dict(node_key=children[1]['key'])).status_code == 409


def test_archive_repair_link_is_durable_scoped_idempotent_and_never_downloads_on_post(causal_case, monkeypatch):
    app, client, fake = causal_case
    from news2douyin.causal.models import CausalArchiveLink
    from news2douyin.intake import service as intake
    from news2douyin.intake.feeds import FEEDS
    from news2douyin.intake.models import DailyRepairJob
    monkeypatch.setenv('NEWS2DOUYIN_GDELT_STREAM_ENABLED', '1')
    def forbidden(*_args, **_kwargs):
        pytest.fail('Archive enqueue synchronously fetched an upstream archive')
    monkeypatch.setattr(intake, 'download', forbidden)
    for feed in FEEDS:
        intake.configure(app.state.engine, feed, feed == 'gkg_english')
    case, other = create(client), create(client, text='不关联这次归档的调查')
    before = len(fake.calls)
    url = BASE+'/'+case['key']+'/archive'
    assert client.post(url, json=dict(day='2026-11-09', retry=False),
        headers={'Origin':'https://other.example'}).status_code == 403
    assert client.post(url, json=dict(day='invalid', retry=False)).status_code == 422
    for _ in range(2):
        response = client.post(url, json=dict(day='2026-11-09', retry=False))
        assert response.status_code == 202, response.text
        repairs = response.json()['coverage']['repairs']
        assert len(repairs) == 1 and repairs[0]['day'] == '2026-11-09' and repairs[0]['status'] == 'queued'
    assert len(fake.calls) == before and detail(client, other)['coverage']['repairs'] == []
    with Session(app.state.engine) as session:
        assert session.exec(select(func.count()).select_from(DailyRepairJob)).one() == 1
        assert session.exec(select(func.count()).select_from(CausalArchiveLink)).one() == 1
        row = session.get(DailyRepairJob, '2026-11-09'); row.status = 'failed'; session.add(row); session.commit()
    assert detail(client, case)['coverage']['repairs'][0]['status'] == 'failed'
    assert detail(client, case)['nodes'][0]['job_state'] == 'pending', 'Archive repair started causal/model work'


def test_real_retrieval_encodes_before_opening_its_sql_session(causal_case, monkeypatch):
    app, client, fake = causal_case
    calls = []
    class Encoder:
        base_url, model, dimensions = 'http://127.0.0.1:9/v1', 'offline-fixture', 2
        def get_embedding(self, queries):
            assert app.state.engine.pool.checkedout() == 0
            calls.append(list(queries))
            with Session(app.state.engine) as session:
                session.add(Event(event_key='encoding-independent-write', event_title='无关的数据库写入'))
                session.commit()
            return [[1., 0.] for _query in queries]
    monkeypatch.setattr(retrieval, 'vector_client', lambda: Encoder())
    monkeypatch.setattr(retrieval, 'retrieve', REAL_RETRIEVE)
    case = create(client); assert drain(app) == 1
    assert calls == [[TARGET]]
    assert len(detail(client, case)['nodes']) == 1
    assert detail(client, case)['nodes'][0]['job_state'] == 'ready'
