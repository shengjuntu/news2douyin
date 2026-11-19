"""Viewer-specific deadlines preserve completed graph batches and explain fallback."""
from types import SimpleNamespace

import pytest
from redis.exceptions import ConnectionError, ResponseError, TimeoutError

from news2douyin.desk import graph_explorer
from news2douyin.desk.graph import GraphMirror
from test_graph_explorer import store  # Reuse the editor and focus snapshot.


@pytest.mark.parametrize(('error', 'reason'), [
    (ResponseError('Query timed out'), 'query_timeout'),
    (TimeoutError('Timeout reading from socket'), 'socket_timeout'),
    (ResponseError('Unsupported query option'), 'query_error'),
    (ConnectionError('Error connecting to socket'), 'unavailable'),
])
def test_graph_failure_before_first_batch_uses_sql_with_accurate_reason(store, monkeypatch, error, reason):
    mirror = GraphMirror()
    mirror.url = 'redis://localhost:6379'

    def fail_read(*_args, **_params):
        raise error

    monkeypatch.setattr(mirror, 'read', fail_read)
    data = graph_explorer.explore(store, mirror)
    assert (data['backend'], data['state'], data['reason']) == ('sql', 'degraded', reason)
    assert {'Event:event1', 'Event:event2'} <= {node['id'] for node in data['nodes']}
    assert str(error) not in str(data)


@pytest.mark.parametrize(('error', 'reason'), [
    (ResponseError('Query timed out'), 'query_timeout'),
    (TimeoutError('Timeout reading from socket'), 'socket_timeout'),
    (graph_explorer._BudgetSpent(), 'budget'),
])
def test_late_failure_returns_completed_falkor_nodes_and_edges(store, monkeypatch, error, reason):
    mirror = GraphMirror()
    mirror.url = 'redis://localhost:6379'

    def query(_graph, statement, **_params):
        if statement == graph_explorer._OWNER_QUERY:
            return [['test-owner']]
        raise error

    def completes_one_batch(graph, _focus, _keys, result, *, read):
        event = graph_explorer._node('Event', 'event1', 'Projected title')
        person = graph_explorer._node('Person', 'person1', 'Current participant')
        result.add(event)
        result.add(person)
        result.connect('e1', event['id'], person['id'], 'MENTIONS')
        return read(graph, 'next read')

    monkeypatch.setattr(mirror, 'read', query)
    monkeypatch.setattr(graph_explorer, '_read_snapshot', completes_one_batch)
    data = graph_explorer.explore(store, mirror)
    assert (data['backend'], data['state'], data['reason']) == ('falkordb', 'partial', reason)
    assert [node['title'] for node in data['nodes']] == ['Projected title', 'Current participant']
    assert [edge['kind'] for edge in data['edges']] == ['MENTIONS']
    assert data['truncated'] is False


def test_deadline_reduces_query_timeout_and_stops_sending_reads(monkeypatch):
    calls = []
    now = [0.0]
    mirror = SimpleNamespace(read=lambda *args, **params: calls.append(params))
    monkeypatch.setattr(graph_explorer, 'time', SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setenv('NEWS2DOUYIN_GRAPH_EXPLORER_QUERY_TIMEOUT_MS', '150')
    monkeypatch.setenv('NEWS2DOUYIN_GRAPH_EXPLORER_BUDGET_MS', '250')
    read = graph_explorer._ReadBudget(mirror, 'separate client')
    read('active_global', 'first')
    now[0] = 0.200
    read('active_global', 'second')
    now[0] = 0.251
    with pytest.raises(graph_explorer._BudgetSpent):
        read('active_global', 'third')
    assert calls[0] == dict(_query_timeout_ms=150, _client='separate client')
    assert 45 <= calls[1]['_query_timeout_ms'] <= 50
    assert len(calls) == 2


def test_viewer_transport_budget_overrides_url_and_keeps_worker_pool():
    mirror = GraphMirror()
    mirror.url = 'redis://localhost:6379/2?socket_timeout=10&retry_on_timeout=true'
    worker = mirror.client = object()
    with mirror.explorer_client(350) as client:
        options = client.connection_pool.connection_kwargs
        assert options['db'] == 2
        assert options['socket_timeout'] == options['socket_connect_timeout'] == 0.35
        assert not options['retry_on_timeout']
        assert options['retry'].get_retries() == 0
    assert mirror.client is worker
