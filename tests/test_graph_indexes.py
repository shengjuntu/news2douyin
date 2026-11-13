"""Startup index work is additive, bounded and waits for the actual build."""
from types import SimpleNamespace
import re
import time

import pytest

from news2douyin.desk import graph_indexes as indexes


class FakeGraph:
    url = 'redis://private.example'
    bootstrapped = True

    def __init__(self):
        self.rows = {'active_global': []}
        self.reads = []
        self.writes = []
        self.error = None

    def read(self, graph, query):
        self.reads.append((graph, query))
        assert query == indexes.INVENTORY_QUERY
        if self.error:
            raise self.error
        return list(self.rows.get(graph, []))

    def query(self, graph, query):
        self.writes.append((graph, query))
        match = re.fullmatch(r'CREATE INDEX ON :(\w+)\((\w+)\)', query)
        assert match
        label, property_name = match.groups()
        self.rows.setdefault(graph, []).append([label, '['+property_name+']',
            '{'+property_name+': [RANGE]}', 'NODE', '[Indexing] 1/1: UNDER CONSTRUCTION'])


def operational(required):
    return [[label, '['+prop+']', '{'+prop+': [RANGE]}', 'NODE', 'OPERATIONAL']
            for label, prop in required]


@pytest.fixture
def clock(monkeypatch):
    value = [100.0]
    monkeypatch.setattr(indexes.time, 'monotonic', lambda: value[0])
    return value


def test_missing_builds_only_one_and_pending_build_is_not_ready(clock):
    graph = FakeGraph()
    assert indexes.ensure_step(graph) is False
    assert graph.writes == [('active_global', 'CREATE INDEX ON :GraphMeta(id)')]
    assert indexes.status_summary(graph)['state'] == 'building'
    assert indexes.status_summary(graph)['ready'] == 0
    clock[0] += indexes.POLL_INTERVAL
    assert indexes.ensure_step(graph) is False
    assert len(graph.writes) == 1, 'pending is not missing: never rebuild an indexing index'
    assert indexes.ready_for(graph, 'active_global', 'GraphMeta') is False
    graph.rows['active_global'][0][-1] = 'OPERATIONAL'
    clock[0] += indexes.POLL_INTERVAL
    assert indexes.ensure_step(graph) is False
    assert graph.writes[-1] == ('active_global', 'CREATE INDEX ON :Event(id)')
    assert indexes.ready_for(graph, 'active_global', 'GraphMeta') is True


def test_ready_indexes_not_rebuilt_and_read_views_are_io_free(clock):
    graph = FakeGraph()
    graph.rows['active_global'] = operational(indexes.GLOBAL_INDEXES)
    assert indexes.ensure_step(graph)
    assert not graph.writes
    before = len(graph.reads)
    for _ in range(10):
        assert indexes.ensure_step(graph)
        assert indexes.status_summary(graph)['ready'] == len(indexes.GLOBAL_INDEXES)
        assert indexes.ready_for(graph, 'active_global', 'Article')
    assert len(graph.reads) == before
    clock[0] += indexes.AUDIT_INTERVAL + 1
    graph.rows['active_global'] = [row for row in graph.rows['active_global'] if row[0] != 'Article']
    assert not indexes.ready_for(graph, 'active_global', 'Article')
    assert indexes.ensure_step(graph) is False
    assert graph.writes == [('active_global', 'CREATE INDEX ON :Article(id)')]


def test_only_node_range_index_qualifies_and_mixed_property_types_work(clock):
    graph = FakeGraph()
    graph.rows['active_global'] = operational(indexes.GLOBAL_INDEXES)[1:]
    graph.rows['active_global'] += [
        ['GraphMeta', '[id]', '{id: [FULLTEXT]}', 'NODE', 'OPERATIONAL'],
        ['GraphMeta', '[id]', '{id: [RANGE]}', 'RELATIONSHIP', 'OPERATIONAL'],
    ]
    assert indexes.ensure_step(graph) is False
    assert graph.writes == [('active_global', 'CREATE INDEX ON :GraphMeta(id)')]
    rows = [['X', '[id, title]', '{id: [RANGE], title: [FULLTEXT]}', 'NODE', 'OPERATIONAL']]
    assert indexes._inventory(rows) == {('X','id'): 'OPERATIONAL'}
    rows = [['X', ['id','title'], {'id':['RANGE'], 'title':['FULLTEXT']}, 'NODE', 'OPERATIONAL']]
    assert indexes._inventory(rows) == {('X','id'): 'OPERATIONAL'}


def test_unverified_owner_and_unknown_graph_cannot_create(clock):
    graph = FakeGraph(); graph.bootstrapped = False
    assert indexes.ensure_step(graph) is False
    assert not graph.reads and not graph.writes
    with pytest.raises(ValueError):
        indexes.ensure_step(graph, graph='other_graph')
    assert not graph.reads and not graph.writes


def test_status_error_sanitizes_provider_details_and_retry_checks_again(clock):
    graph = FakeGraph(); graph.error = RuntimeError('redis://user:secret@private.example')
    assert indexes.ensure_step(graph) is False
    state = indexes.status_summary(graph)
    assert state['state'] == 'error' and 'RuntimeError' in state['error']
    assert 'secret' not in str(state) and 'private.example' not in str(state)
    graph.error = None; graph.rows['active_global'] = operational(indexes.GLOBAL_INDEXES)
    indexes.reset(graph)
    assert indexes.ensure_step(graph)


def test_failed_existing_index_does_not_drop_or_recreate(clock):
    graph = FakeGraph(); graph.rows['active_global'] = operational(indexes.GLOBAL_INDEXES)
    graph.rows['active_global'][2][-1] = 'FAILED'
    assert indexes.ensure_step(graph) is False
    assert indexes.status_summary(graph)['state'] == 'error'
    assert indexes.status_summary(graph)['current'] == 'Article.id'
    assert not graph.writes


def test_new_focus_is_checked_independently(clock):
    graph = FakeGraph(); graph.rows['active_global'] = operational(indexes.GLOBAL_INDEXES)
    assert indexes.ensure_step(graph)
    assert indexes.ensure_step(graph, graph='focus_w_abc') is False
    assert graph.writes == [('focus_w_abc', 'CREATE INDEX ON :GraphMeta(id)')]
    assert indexes.status_summary(graph)['graph'] == 'focus_w_abc'
    assert indexes.ready_for(graph, 'active_global', 'Article')
    graph.rows['focus_w_abc'] = operational(indexes.FOCUS_INDEXES)
    clock[0] += indexes.POLL_INTERVAL
    assert indexes.ensure_step(graph, graph='focus_w_abc')
    assert indexes.status_summary(graph)['state'] == 'ready'


def test_unconfigured_status_never_touches_graph(clock):
    graph = FakeGraph(); graph.url = ''
    assert indexes.ensure_step(graph) is False
    assert indexes.status_summary(graph)['state'] == 'unconfigured'
    assert not graph.reads and not graph.writes


def test_real_falkor_range_index_inventory_and_explain(live_graph, monkeypatch):
    from news2douyin.desk.graph import GraphMirror
    mirror = GraphMirror(); mirror.bootstrapped = True
    mirror.query('active_global', 'CREATE (:Article {id:$id})', id='index-proof')
    monkeypatch.setattr(indexes, 'POLL_INTERVAL', 0)
    deadline = time.monotonic() + 20
    while not indexes.ensure_step(mirror):
        assert time.monotonic() < deadline, indexes.status_summary(mirror)
        time.sleep(.02)
    state = indexes.status_summary(mirror)
    assert state['state'] == 'ready' and state['ready'] == len(indexes.GLOBAL_INDEXES)
    assert indexes.ready_for(mirror, 'active_global', 'Article')
    plan = mirror.client.execute_command('GRAPH.EXPLAIN', mirror.prefix+'_active_global',
        "MATCH (a:Article {id:'index-proof'}) RETURN a.id")
    assert 'Index Scan' in str(plan) and 'Label Scan' not in str(plan)
    assert mirror.read('active_global', 'MATCH (a:Article {id:$id}) RETURN a.id', id='index-proof') == [['index-proof']]


# Reuse the isolated real database fixture. No production service is accessed.
from test_content_foundation import live_graph
