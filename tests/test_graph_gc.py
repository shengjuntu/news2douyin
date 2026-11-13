"""Maintenance correctness, restart recovery and a real native-ID query plan."""
import re
import time

import pytest
from sqlmodel import Session, select

from news2douyin.desk import graph_gc, graph_indexes
from news2douyin.desk.graph import GraphMirror
from news2douyin.desk.graph_maintenance_models import GraphGcState
from news2douyin.desk.models import GraphPending
from news2douyin.storage.db import make_engine, create_schema
from test_postgres_integration import postgres_url


class MemoryGraph(GraphMirror):
    """A graph protocol fixture with real SQL leases and exact reference checks."""
    def __init__(self, engine, nodes=None, edges=None):
        super().__init__()
        self.url, self.prefix, self.bootstrapped = 'redis://qa', 'gc_qa', True
        self.engine = engine
        self.nodes = dict(nodes or {})
        self.edges = list(edges or [])
        self.calls = []
        self.write_calls = 0
        self.fail_batch = False
        self.plan = 'Results\n Sort\n Node By Id Seek | (n:Article)'
        self.before_delete = None

    def _io(self, query, params):
        assert self.engine.pool.checkedout() == 0, 'SQL connection held during graph I/O'
        assert self._projection_lease is not None, 'GC bypassed the global graph lease'
        self.calls.append((query, params))

    def explain(self, graph, query, **params):
        self._io(query, params)
        return self.plan

    def read(self, graph, query, **params):
        self._io(query, params)
        match = re.search(r'\(n:(\w+)\)', query)
        label = match[1] if match else params['label']
        values = [(ident, value) for ident, value in self.nodes.items() if value[0] == label]
        if 'max(id(n))' in query:
            return [[max((ident for ident, _ in values), default=None)]]
        if self.fail_batch:
            self.fail_batch = False
            raise TimeoutError('secret-endpoint-must-not-leak')
        return [[ident, value[1]] for ident, value in sorted(values)
                if params['lo'] <= ident < params['hi']][:params['limit']]

    def query(self, graph, query, **params):
        self._io(query, params)
        self.write_calls += 1
        label = params['label']
        if self.before_delete:
            callback, self.before_delete = self.before_delete, None
            callback(self)
        deleted = 0
        for ident in params['ids']:
            node = self.nodes.get(ident)
            if node and node[0] == label and not any(
                    target == ident and relation in graph_gc._REFERENCES[label]
                    for source, relation, target in self.edges):
                del self.nodes[ident]
                self.edges = [edge for edge in self.edges if ident not in (edge[0], edge[2])]
                deleted += 1
        return [['count(n)'], [[deleted]], ['Nodes deleted: '+str(deleted)]]


@pytest.fixture
def gc_engine(tmp_path, monkeypatch):
    engine = make_engine('sqlite:///'+str(tmp_path/'gc.db'))
    create_schema(engine)
    monkeypatch.setenv('NEWS2DOUYIN_GRAPH_GC_BATCH_SIZE', '2')
    monkeypatch.setenv('NEWS2DOUYIN_GRAPH_GC_WINDOW', '64')
    monkeypatch.setattr(graph_indexes, 'ready_for', lambda *args: True)
    try:
        yield engine
    finally:
        engine.dispose()


def state_for(engine, label):
    with Session(engine, expire_on_commit=False) as session:
        return session.exec(select(GraphGcState).where(GraphGcState.label == label)).one()


def finish_cycle(engine, mirror, limit=100):
    for _ in range(limit):
        if not graph_gc.step(engine, mirror):
            return
    raise AssertionError('Maintenance did not settle')


def test_resume_preserves_shared_article_and_cleans_only_orphan(gc_engine):
    nodes = {0:('Event','event'), 1:('Article','shared'), 2:('Article','orphan'), 3:('Article','another')}
    mirror = MemoryGraph(gc_engine, nodes, [(0,'HAS_SOURCE',1), (0,'HAS_SOURCE',3)])
    assert graph_gc.step(gc_engine, mirror)
    checkpoint = state_for(gc_engine, 'Article')
    assert checkpoint.cursor == 3 and checkpoint.scanned == 2 and checkpoint.deleted == 1
    # Restarting the worker does not restart its label scan or repeat deletion.
    restarted = MemoryGraph(gc_engine, mirror.nodes, mirror.edges)
    assert graph_gc.step(gc_engine, restarted)
    checkpoint = state_for(gc_engine, 'Article')
    assert checkpoint.status == 'complete' and checkpoint.scanned == 3 and checkpoint.deleted == 1
    assert {value[1] for value in restarted.nodes.values()} == {'event','shared','another'}
    assert not any('max(id(n))' in query for query, _ in restarted.calls)
    assert all(not ('WHERE NOT' in query and 'UNWIND' not in query) for query, _ in mirror.calls+restarted.calls)


def test_cascade_reclaims_old_mentions_but_retains_intent_referent(gc_engine):
    mirror = MemoryGraph(gc_engine, {
        1:('Article','orphan-article'), 2:('EventMention','old-mention'),
        3:('Argument','old-argument'), 4:('Referent','old-name'),
        5:('Referent','still-watched'), 6:('InterestEntity','attention'),
    }, [(1,'DESCRIBES',2), (2,'HAS_ARGUMENT',3), (3,'REFERS_TO',4), (6,'MAPS_TO',5)])
    finish_cycle(gc_engine, mirror)
    assert set(mirror.nodes) == {5,6}
    status = graph_gc.summary(gc_engine, mirror)
    assert status['deleted'] == 4 and status['state'] == 'idle'
    assert status['complete_labels'] == len(graph_gc.LABELS)


def test_graph_failure_preserves_cursor_and_never_changes_projection_ack(gc_engine):
    mirror = MemoryGraph(gc_engine, {1:('Article','garbage')})
    with Session(gc_engine) as session:
        session.add(GraphPending(key='event/business',token='current',written='previous'));session.commit()
    mirror.fail_batch = True
    assert graph_gc.step(gc_engine, mirror) is False
    failed = state_for(gc_engine, 'Article')
    assert failed.status == 'error' and failed.phase == 'batch' and failed.cursor == 0
    assert 'secret' not in failed.error and failed.next_due > time.time()
    calls = len(mirror.calls)
    graph_gc.retry(gc_engine, mirror)
    assert len(mirror.calls) == calls, 'HTTP retry performed graph I/O'
    assert graph_gc.step(gc_engine, mirror)
    assert state_for(gc_engine, 'Article').deleted == 1
    with Session(gc_engine) as session:
        assert session.get(GraphPending,'event/business').written == 'previous'


def test_missing_ready_indexes_or_unsafe_plan_never_runs_delete(gc_engine, monkeypatch):
    mirror = MemoryGraph(gc_engine, {1:('Article','garbage')})
    monkeypatch.setattr(graph_indexes, 'ready_for', lambda *args: False)
    assert not graph_gc.step(gc_engine, mirror) and mirror.calls == []
    assert graph_gc.summary(gc_engine, mirror)['scanned'] is None
    monkeypatch.setattr(graph_indexes, 'ready_for', lambda *args: True)
    mirror.plan = 'Results\n Limit\n Node By Label Scan | (n:Article)'
    assert not graph_gc.step(gc_engine, mirror)
    assert 1 in mirror.nodes and state_for(gc_engine, 'Article').status == 'error'
    assert mirror.write_calls == 0


def test_reference_is_rechecked_at_delete_time(gc_engine):
    mirror = MemoryGraph(gc_engine, {0:('Event','new-owner'),1:('Article','temporarily-unlinked')})
    mirror.before_delete = lambda graph: graph.edges.append((0,'HAS_SOURCE',1))
    assert graph_gc.step(gc_engine, mirror)
    assert 1 in mirror.nodes and state_for(gc_engine, 'Article').deleted == 0


def test_sparse_native_windows_are_bounded_and_tail_is_processed(gc_engine):
    mirror = MemoryGraph(gc_engine, {1:('Article','first'),191:('Article','last')})
    finish_cycle(gc_engine, mirror)
    assert not mirror.nodes
    windows = [params for query, params in mirror.calls if 'RETURN id(n),n.id' in query]
    assert windows and all(params['hi']-params['lo'] <= 64 for params in windows)
    assert state_for(gc_engine, 'Article').deleted == 2


def test_summary_is_readonly_and_rejects_foreign_graph_names(gc_engine):
    mirror = MemoryGraph(gc_engine)
    assert graph_gc.summary(gc_engine, mirror)['state'] == 'waiting'
    assert mirror.calls == []
    with pytest.raises(ValueError):
        graph_gc.step(gc_engine, mirror, 'another_project')
    with Session(gc_engine) as session:
        assert not session.exec(select(GraphGcState)).all()


def test_next_cycle_revisits_native_ids_reused_below_old_cursor(gc_engine):
    mirror = MemoryGraph(gc_engine, {1:('Article','old-orphan')})
    finish_cycle(gc_engine, mirror)
    assert state_for(gc_engine,'Article').cycle == 1 and not mirror.nodes
    mirror.nodes[1] = ('Article','new-orphan-reused-id')
    with Session(gc_engine) as session:
        state=session.get(GraphGcState,state_for(gc_engine,'Article').key)
        state.next_due=0;session.add(state);session.commit()
    assert graph_gc.step(gc_engine, mirror)
    assert state_for(gc_engine,'Article').cycle == 2 and not mirror.nodes


def test_large_native_cursor_and_counters_roundtrip(postgres_url):
    engine=make_engine(postgres_url)
    create_schema(engine)
    large=2**40
    with Session(engine) as session:
        session.add(GraphGcState(key='large-native-cursor',namespace='large',graph='active_global',
            label='Article',cursor=large,through_key=large+10,scanned=large+20,deleted=large+30))
        session.commit()
    with Session(engine) as session:
        row=session.get(GraphGcState,'large-native-cursor')
        assert (row.cursor,row.through_key,row.scanned,row.deleted)==(large,large+10,large+20,large+30)
    engine.dispose()


def test_real_native_seek_and_orphan_reference_policy(tmp_path, live_graph, monkeypatch):
    from test_content_foundation import app_at
    app = app_at(tmp_path)
    mirror = app.state.desk.graph
    mirror.bootstrap(app.state.engine)
    # Index readiness itself has dedicated real-server tests. Here isolate the
    # maintenance plan and reference policy without waiting through each build.
    monkeypatch.setattr(graph_indexes,'ready_for',lambda *args: True)
    def fail_unexpected(engine, mirror, row, exc, phase):
        raise exc
    monkeypatch.setattr(graph_gc,'_error',fail_unexpected)
    with mirror.exclusive(app.state.engine) as acquired:
        assert acquired
        mirror.query('active_global', 'CREATE (e:Event {id:"kept"}),'
            '(a:Article {id:"shared"}),(b:Article {id:"orphan"}),'
            '(i:InterestEntity {id:"intent"}),(r:Referent {id:"watched"}),'
            '(e)-[:HAS_SOURCE]->(a),(i)-[:MAPS_TO]->(r)')
    assert graph_gc.step(app.state.engine, mirror), graph_gc.summary(app.state.engine, mirror)
    finish_cycle(app.state.engine, mirror, 100)
    assert mirror.read('active_global','MATCH (a:Article) RETURN a.id') == [['shared']]
    assert mirror.read('active_global','MATCH (r:Referent) RETURN r.id') == [['watched']]
    app.state.engine.dispose()


# Share the real disposable FalkorDB fixture, never a production graph.
from test_content_foundation import live_graph
