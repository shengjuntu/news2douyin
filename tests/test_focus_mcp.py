import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from news2douyin.desk.models import EventPlace, RadarEntry, Watch, WatchEvent
from news2douyin.server.app import create_app
from news2douyin.server import focus_mcp
from news2douyin.storage.articles import record_version
from news2douyin.storage.models import Article, ArticleEventLink, Event


HEADERS = {'Authorization': 'Bearer test-focus-token', 'Accept': 'application/json, text/event-stream'}
MODERN = '2026-07-28'


def rpc(client, method, params=None, *, headers=HEADERS, request_id=1):
    return client.post('/mcp/focus', headers=headers, json={
        'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params or {},
    })


def tool(client, name, **args):
    response = rpc(client, 'tools/call', {'name': name, 'arguments': args},
                   headers={**HEADERS, 'MCP-Protocol-Version': '2025-11-25'})
    assert response.status_code == 200, response.text
    return response.json()['result']


@pytest.fixture
def focus_app(tmp_path, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_FOCUS_MCP_TOKEN', 'test-focus-token')
    app = create_app(db_url=f'sqlite:///{tmp_path / "focus.db"}', storage_root=str(tmp_path))
    client = TestClient(app)
    try:
        yield app, client
    finally:
        client.close()
        app.state.engine.dispose()


def test_focus_mcp_tokens_are_separate_and_absent_token_is_disabled(tmp_path, monkeypatch, focus_app):
    app, client = focus_app
    own_conf = app.state.research_settings.get()
    research_headers = {'Authorization': 'Bearer ' + own_conf['mcp_token']}
    assert rpc(client, 'initialize', headers={}).status_code == 401
    assert rpc(client, 'initialize', headers=research_headers).status_code == 401
    assert rpc(client, 'initialize', headers={**HEADERS, 'Origin': 'https://other.example'}).status_code == 403
    request = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}
    assert client.post('/mcp/research', headers=HEADERS, json=request).status_code == 401
    assert client.post('/mcp/research', headers=research_headers, json=request).status_code == 200
    assert 'test-focus-token' not in client.get('/focus?new=1').text
    monkeypatch.setenv('NEWS2DOUYIN_FOCUS_MCP_TOKEN', '')
    disabled = create_app(db_url=f'sqlite:///{tmp_path / "no-token.db"}', storage_root=str(tmp_path / 'none'))
    try:
        with TestClient(disabled) as other:
            assert rpc(other, 'initialize').status_code == 503
    finally:
        disabled.state.engine.dispose()


def test_focus_mcp_handshake_tool_discovery_and_validation(focus_app):
    _, client = focus_app
    for version in ('2025-11-25', '2025-06-18', '2025-03-26'):
        result = rpc(client, 'initialize', {'protocolVersion': version}).json()['result']
        assert result['protocolVersion'] == version
        assert result['serverInfo']['name'] == 'news2douyin-focus'
        assert result['capabilities'] == {'tools': {'listChanged': False}}
    assert rpc(client, 'initialize', {'protocolVersion': '1999-01-01'}).json()['result']['protocolVersion'] == '2025-11-25'
    notice = client.post('/mcp/focus', headers=HEADERS, json={'jsonrpc': '2.0', 'method': 'notifications/initialized'})
    assert notice.status_code == 202 and notice.content == b''
    for method in ('GET', 'DELETE'):
        response = client.request(method, '/mcp/focus', headers=HEADERS)
        assert response.status_code == 405 and response.headers['allow'] == 'POST'
    tools = rpc(client, 'tools/list').json()['result']['tools']
    assert [item['name'] for item in tools] == ['focus_list', 'focus_overview', 'focus_list_events', 'focus_get_event', 'focus_read_source']
    for item in tools:
        assert item['annotations']['readOnlyHint'] is True
        assert item['annotations']['openWorldHint'] is False
        assert item['inputSchema']['additionalProperties'] is False
    assert rpc(client, 'tools/call', {'name': 'focus_delete'}).json()['error']['code'] == -32602
    assert rpc(client, 'tools/call', {'name': 'focus_list', 'arguments': []}).json()['error']['code'] == -32602
    assert rpc(client, 'tools/list', request_id=True).status_code == 400
    assert rpc(client, 'tools/list', headers={**HEADERS, 'MCP-Protocol-Version': '1999-01-01'}).status_code == 400
    for args in ({'limit': 101}, {'limit': '2'}, {'unexpected': 'field'}):
        assert tool(client, 'focus_list', **args)['isError']
    assert tool(client, 'focus_list_events', focus='focus-1', date_from='2026-99')['isError']
    assert client.post('/mcp/focus', headers=HEADERS, json=[]).json()['error']['code'] == -32600
    json_headers = {**HEADERS, 'Content-Type': 'application/json'}
    assert client.post('/mcp/focus', headers=json_headers, content='{ invalid').json()['error']['code'] == -32700
    assert client.post('/mcp/focus', headers=json_headers, content='x' * 65537).status_code == 413
    legacy = rpc(client, 'tools/call', {'name': 'focus_list'}).json()['result']
    assert not legacy['isError'] and 'structuredContent' not in legacy
    assert json.loads(legacy['content'][0]['text'])['items'] == []


def test_focus_mcp_modern_stateless_discovery_and_headers(focus_app):
    _, client = focus_app
    meta = {'io.modelcontextprotocol/protocolVersion': MODERN, 'io.modelcontextprotocol/clientCapabilities': {}}
    headers = {**HEADERS, 'MCP-Protocol-Version': MODERN}
    discover = rpc(client, 'server/discover', {'_meta': meta}, headers={**headers, 'Mcp-Method': 'server/discover'})
    assert discover.status_code == 200, discover.text
    result = discover.json()['result']
    assert result['resultType'] == 'complete' and result['supportedVersions'] == [MODERN]
    assert result['_meta']['io.modelcontextprotocol/serverInfo']['name'] == 'news2douyin-focus'
    params = {'_meta': meta, 'name': 'focus_list'}
    call_headers = {**headers, 'Mcp-Method': 'tools/call', 'Mcp-Name': 'focus_list'}
    returned = rpc(client, 'tools/call', params, headers=call_headers)
    assert returned.status_code == 200, returned.text
    assert returned.json()['result']['structuredContent']['items'] == []
    for inconsistent in ({**call_headers, 'Mcp-Name': 'focus_get_event'}, {**call_headers, 'Mcp-Method': 'tools/list'}):
        denied = rpc(client, 'tools/call', params, headers=inconsistent)
        assert denied.status_code == 400 and denied.json()['error']['code'] == -32020
    denied = rpc(client, 'tools/call', {**params, '_meta': {'io.modelcontextprotocol/protocolVersion': MODERN}}, headers=call_headers)
    assert denied.status_code == 400 and denied.json()['error']['code'] == -32602
    unknown = rpc(client, 'unknown', {'_meta': meta}, headers={**headers, 'Mcp-Method': 'unknown'})
    assert unknown.status_code == 404 and unknown.json()['error']['code'] == -32601


def add_event(session, key, focus, title, seen, branch, country):
    article = Article(article_key='article-' + key, title=title, content='first hand local material',
                      published_at=seen, fetched_at=seen, url='https://fixture.example/' + key,
                      source_domain='fixture.example', country='gb')
    record_version(session, article)
    session.add_all([
        article,
        Event(event_key=key, event_title=title, topic='security'),
        RadarEntry(key=key, title=title, seen=seen, priority=4, band='event', reason='',
                   evidence='', headings='[]', material='', sources='[]', geography='[]'),
        WatchEvent(watch=focus, event=key, choice='include', branch=branch),
        ArticleEventLink(article_key=article.article_key, event_key=key),
        EventPlace(key='location-' + key, event=key, article=article.article_key,
                   role='action', iso=country, revision=1, quote='first hand local material'),
    ])


def test_focus_mcp_paged_filter_reads_provenance_and_preserves_scope(focus_app):
    app, client = focus_app
    with Session(app.state.engine) as session:
        session.add_all([
            Watch(key='focus-1', name='关税', kind='长期事件', scope='关税'),
            Watch(key='focus-2', name='选举', kind='长期事件', scope='选举'),
        ])
        add_event(session, 'event-1', 'focus-1', 'Japan first', '2026-11-15T17:00:00Z', 'negotiations', 'JP')
        add_event(session, 'event-2', 'focus-1', 'Japan second', '2026-11-16T08:00:00Z', 'negotiations', 'JP')
        add_event(session, 'event-3', 'focus-1', 'France', '2026-11-16T09:00:00Z', 'negotiations', 'FR')
        add_event(session, 'event-4', 'focus-2', 'Outside focus', '2026-11-16T09:00:00Z', 'negotiations', 'JP')
        session.commit()
    listed = tool(client, 'focus_list', query='关税')['structuredContent']
    assert [row['key'] for row in listed['items']] == ['focus-1']
    filters = {'focus': 'focus-1', 'date_from': '2026-11-16', 'date_to': '2026-11-16',
               'category': 'security', 'branch': 'negotiations', 'country': 'JP', 'country_role': 'action'}
    first = tool(client, 'focus_list_events', **filters, limit=1)['structuredContent']
    assert [row['key'] for row in first['items']] == ['event-2'] and first['next_cursor']
    second = tool(client, 'focus_list_events', **filters, limit=1, cursor=first['next_cursor'])['structuredContent']
    assert [row['key'] for row in second['items']] == ['event-1']
    counts = tool(client, 'focus_overview', **filters, group_by='day')['structuredContent']
    assert counts['total'] == 2
    detail = tool(client, 'focus_get_event', focus='focus-1', event='event-1')['structuredContent']
    assert len(detail['sources']) == 1
    source = tool(client, 'focus_read_source', focus='focus-1', event='event-1',
                  article='article-event-1', revision=1, limit=10)['structuredContent']
    assert source['text'] == 'first hand' and source['revision'] == 1 and source['next_offset'] == 10
    assert tool(client, 'focus_get_event', focus='focus-1', event='event-4')['isError']
    assert tool(client, 'focus_read_source', focus='focus-1', event='event-1',
                article='article-event-4', revision=1)['isError']
    assert tool(client, 'focus_read_source', focus='focus-1', event='event-4',
                article='article-event-4', revision=1)['isError']


def test_focus_mcp_limits_valid_tool_calls_and_reports_retry_time(tmp_path, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_FOCUS_MCP_TOKEN', 'test-focus-token')
    monkeypatch.setattr(focus_mcp, '_CALLS_PER_MINUTE', 1)
    app = create_app(db_url=f'sqlite:///{tmp_path / "rate.db"}', storage_root=str(tmp_path))
    try:
        with TestClient(app) as client:
            assert rpc(client, 'tools/call', {'name': 'focus_list'}).status_code == 200
            limited = rpc(client, 'tools/call', {'name': 'focus_list'})
            assert limited.status_code == 429 and int(limited.headers['retry-after']) > 0
            assert rpc(client, 'tools/list').status_code == 200
    finally:
        app.state.engine.dispose()
