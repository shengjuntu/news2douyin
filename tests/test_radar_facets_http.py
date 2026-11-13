"""The first radar response does not wait for exact, live facet counts."""
from html import unescape
import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from news2douyin.knowledge import radar
from news2douyin.server.app import create_app
from test_postgres_integration import postgres_url
from test_radar_views import seed


@pytest.fixture(params=['sqlite', 'postgres'])
def radar_http(request, tmp_path):
    url = request.getfixturevalue('postgres_url') if request.param == 'postgres' else 'sqlite:///'+str(tmp_path/'facets.db')
    app = create_app(db_url=url, storage_root=str(tmp_path/'storage'))
    client = TestClient(app)
    try:
        yield app, client
    finally:
        client.close(); app.state.engine.dispose()


def test_first_paint_has_records_and_lookahead_without_exact_facets(radar_http, monkeypatch):
    app, client = radar_http
    seed(app, 'first', provider='gkg'); seed(app, 'second', provider='gkg')
    queries = []
    def record(_connection, _cursor, _sql, _parameters, context, _many):
        queries.append(context.execution_options.get('n2d_query_name'))
    def forbidden(*args, **kwargs):
        pytest.fail('server-rendered list called exact facets')
    monkeypatch.setattr(radar, 'facets', forbidden)
    event.listen(app.state.engine, 'before_cursor_execute', record)
    try:
        response = client.get('/radar?period=all&provider=gkg&channel=diplomacy&view=list&limit=1')
        assert response.status_code == 200
        body = response.text
        assert body.count('data-radar-event=') == 1
        assert 'data-radar-stat="total">…' in body and 'data-facet-state="loading"' in body
        assert 'data-radar-stat="source_count">…' in body and '>None<' not in body
        assert '精确统计中' in body and '>下一页</a>' in body
        assert '当前范围没有事件。' not in body
        target = unescape(re.search(r'data-radar-facet-url="([^"]+)"', body).group(1))
        params = parse_qs(urlsplit(target).query)
        assert params['period'] == ['all'] and params['provider'] == ['gkg']
        assert params['channel'] == ['diplomacy']
        assert not set(params).intersection({'limit','offset','view'})
        second = client.get('/radar?period=all&provider=gkg&channel=diplomacy&limit=1&offset=1')
        assert second.status_code == 200 and second.text.count('data-radar-event=') == 1
        assert '>上一页</a>' in second.text and '>下一页</a>' not in second.text
        beyond = client.get('/radar?period=all&limit=1&offset=20')
        assert beyond.status_code == 200 and '当前页没有可展示的事件，完整范围统计中。' in beyond.text
        assert '>上一页</a>' in beyond.text and '>下一页</a>' not in beyond.text
    finally:
        event.remove(app.state.engine, 'before_cursor_execute', record)
    assert 'radar.facets' not in queries and 'radar.summary' not in queries
    assert 'radar.page_keys' in queries


def test_empty_first_paint_is_unknown_until_exact_zero_arrives(radar_http):
    _app, client = radar_http
    page = client.get('/radar?period=all')
    assert page.status_code == 200
    assert 'data-radar-stat="total">…' in page.text
    assert '当前页没有可展示的事件，完整范围统计中。' in page.text
    assert '当前范围没有事件。' not in page.text and '>下一页</a>' not in page.text
    result = client.get('/api/radar/facets?period=all')
    assert result.status_code == 200 and result.json()['total'] == 0
    assert result.json()['radar_stats'] == dict(total=0, analyzed=0, source_count=0)


def test_exact_api_only_computes_facets_and_sees_new_writes(radar_http):
    app, client = radar_http
    seed(app, 'gkg_one', provider='gkg'); seed(app, 'other', provider='worldnewsapi')
    queries = []
    def record(_connection, _cursor, _sql, _parameters, context, _many):
        queries.append(context.execution_options.get('n2d_query_name'))
    event.listen(app.state.engine, 'before_cursor_execute', record)
    try:
        response = client.get('/api/radar/facets?period=all&channel=diplomacy&provider=gkg')
    finally:
        event.remove(app.state.engine, 'before_cursor_execute', record)
    assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
    data = response.json()
    assert set(data) == {'total','historical','radar_stats','channels','providers'}
    assert data['total'] == 1 and data['radar_stats']['source_count'] == 1
    # SQLite can perform its one-time JSON/search capability metadata probe.
    assert [name for name in queries if name is not None] == ['radar.facets']
    assert client.get('/api/radar/facets?period=all&channel=diplomacy&provider=gkg&view=countries').json() == data
    seed(app, 'gkg_two', provider='gkg')
    fresh = client.get('/api/radar/facets?period=all&channel=diplomacy&provider=gkg').json()
    assert fresh['total'] == 2 and fresh['radar_stats']['source_count'] == 2
    assert client.get('/api/radar/facets?period=all').json()['total'] == 3
    assert client.get('/api/radar/facets?channel=unknown').status_code == 422
