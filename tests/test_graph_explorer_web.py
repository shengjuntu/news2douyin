"""Check public navigation and graph routing on the actual application."""
import pytest
from fastapi.testclient import TestClient

from news2douyin.server.app import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', '')
    app = create_app(db_url=f'sqlite:///{tmp_path / "test.db"}', storage_root=str(tmp_path))
    browser = TestClient(app)
    try:
        yield browser
    finally:
        browser.close()
        app.state.engine.dispose()


def _create_focus(client, name):
    response = client.post('/api/focus', json={'name': name, 'kind': '人物', 'scope': name})
    assert response.status_code == 201, response.text
    return response.json()


def test_each_focus_links_to_its_own_graph_and_global_remains_separate(client):
    a = _create_focus(client, 'Argentina')
    b = _create_focus(client, 'Brussels & partners')
    for watch in (a, b):
        key = watch['key']
        focus = client.get(f'/focus/{key}')
        assert f'/graphs?view=focus&amp;focus={key}' in focus.text
        page = client.get('/graphs', params={'view': 'focus', 'focus': key})
        assert page.status_code == 200
        assert f'data-endpoint="/api/graphs?view=focus&amp;focus={key}"' in page.text
        assert f'<option value="{key}" selected>' in page.text
        assert 'Brussels &amp; partners' in page.text
        assert 'cosmos-graph-3.6.0.min.js' in page.text
        assert 'cytoscape.min.js' not in page.text
        graph = client.get('/api/graphs', params={'view': 'focus', 'focus': key})
        assert graph.status_code == 200
        assert graph.json()['view'] == 'focus'
    global_page = client.get('/graphs')
    assert 'data-endpoint="/api/graphs?view=global"' in global_page.text
    assert client.get('/api/graphs').json()['view'] == 'global'
    expanded = client.get('/graphs', params={'limit': 480})
    assert 'data-endpoint="/api/graphs?view=global&amp;limit=480"' in expanded.text
    assert client.get('/api/graphs', params={'limit': 480}).json()['node_limit'] == 480


def test_missing_deleted_or_invalid_graphs_have_explicit_responses(client):
    empty = client.get('/graphs', params={'view': 'focus'})
    assert '还没有可查看的焦点图' in empty.text
    assert client.get('/api/graphs', params={'view': 'focus'}).status_code == 422
    assert client.get('/api/graphs', params={'limit': 1000}).status_code == 422
    assert client.get('/api/graphs', params={'view': 'raw'}).status_code == 422
    watch = _create_focus(client, 'Archived focus')
    key = watch['key']
    result = client.request('DELETE', f'/api/focus/{key}', json={'version': watch['version']})
    assert result.status_code == 200, result.text
    for url in ('/graphs', '/api/graphs'):
        assert client.get(url, params={'view': 'focus', 'focus': key}).status_code == 404
