"""Check public navigation and graph routing on the actual application."""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from news2douyin.desk.models import WatchEvent
from news2douyin.server.app import create_app
from news2douyin.server.desk import safe_back
from news2douyin.storage.models import Event, EventMoment


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
    for url in ('/graphs', '/api/graphs'):
        assert client.get(url, params={'view': 'global', 'lens': 'people'}).status_code == 422
        assert client.get(url, params={'view': 'focus', 'lens': 'invalid'}).status_code == 422
        assert client.get(url, params={'view': 'focus', 'lens': 'people', 'days': 31}).status_code == 422
        assert client.get(url, params={'view': 'focus', 'lens': 'people', 'until': '2026-02-30'}).status_code == 422
    watch = _create_focus(client, 'Archived focus')
    key = watch['key']
    result = client.request('DELETE', f'/api/focus/{key}', json={'version': watch['version']})
    assert result.status_code == 200, result.text
    for url in ('/graphs', '/api/graphs'):
        assert client.get(url, params={'view': 'focus', 'focus': key}).status_code == 404
        assert client.get(url, params={'view': 'focus', 'focus': key, 'lens': 'people'}).status_code == 404


def test_focus_graph_investigations_are_saved_under_the_selected_focus(client, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_ATTRIBUTION_BASE_URL', 'http://model.test/v1')
    monkeypatch.setenv('NEWS2DOUYIN_ATTRIBUTION_MODEL', 'fixture-model')
    focus = _create_focus(client, '焦点甲')['key']
    other = _create_focus(client, '焦点乙')['key']
    with Session(client.app.state.engine) as session:
        session.add(Event(event_key='evt_root', event_title='用于溯源的事件'))
        session.add(EventMoment(moment_key='root_date', event_key='evt_root', title='发生',
                                reviewed=True, time_kind='occurred', certainty='exact', date_start='2026-11-18'))
        session.add(WatchEvent(watch=focus, event='evt_root', choice='include'))
        session.commit()

    base = '/api/causal/investigations'
    root = client.post(base, json={'text': '用于溯源的事件', 'focus_key': focus, 'event_key': 'evt_root'})
    assert root.status_code == 202, root.text
    case = root.json()
    assert case['event_at'] == '2026-11-18' and case['focus_key'] == focus
    assert case['nodes'][0]['event_key'] == 'evt_root'
    prose = client.post(base, json={'text': '自由输入的描述', 'focus_key': other})
    assert prose.status_code == 202
    assert prose.json()['focus_key'] == other and prose.json()['nodes'][0]['event_key'] == ''
    assert client.post(base, json={'text': '其他焦点的事件', 'focus_key': other, 'event_key': 'evt_root'}).status_code == 422
    assert client.post(base, json={'text': '不存在的焦点', 'focus_key': 'missing'}).status_code == 404
    assert [item['key'] for item in client.get(base, params={'focus_key': focus}).json()['investigations']] == [case['key']]
    assert client.get(base + '/' + case['key'], params={'focus_key': other}).status_code == 404
    assert client.get(base + '/' + case['key']).status_code == 200

    graph_path = f"/graphs?view=focus&focus={focus}&investigation={case['key']}"
    assert safe_back(graph_path) == graph_path
    assert safe_back(f'https://outside.test{graph_path}') == '/radar'
    graph = client.get(graph_path)
    assert graph.status_code == 200 and 'id="causal-form"' in graph.text and case['key'] in graph.text
    assert 'id="causal-form"' not in client.get('/graphs').text
    assert '>查找前因</a>' not in client.get('/radar').text
    event = client.get('/events/evt_root', params={'back': graph_path})
    assert event.status_code == 200 and '返回焦点图谱' in event.text
    assert 'id="causal-event-link"' not in event.text
