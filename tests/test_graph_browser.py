import pytest
from fastapi.testclient import TestClient
from news2douyin.server.app import create_app
from news2douyin.server.graph_browser import browser_url


@pytest.mark.parametrize('value',['javascript:alert(1)','http://name:secret@host/','//host/path','http://host:invalid','https://host/\nfoo','https://host\\foo'])
def test_browser_entry_rejects_unsafe_urls(value):
    with pytest.raises(ValueError):browser_url(value)


def test_browser_entry_persists_without_changing_database_connection(tmp_path,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL','')
    app=create_app(db_url='sqlite:///'+str(tmp_path/'news.db'),storage_root=str(tmp_path/'runs'))
    client=TestClient(app)
    r=client.post('/api/knowledge/browser-setting',json={'url':'https://ai03.example:13000/'})
    assert r.status_code==200 and app.state.desk.graph.url==''
    page=client.get('/knowledge/browser')
    assert 'https://ai03.example:13000/' in page.text and 'n2d_active_global' in page.text
    assert client.post('/api/knowledge/browser-setting',json={'url':'http://ai03:13000'},headers={'Origin':'https://evil.example'}).status_code==403
    assert client.post('/api/knowledge/browser-setting',json={'url':'javascript:alert(1)'}).status_code==422
    assert client.post('/api/knowledge/browser-setting',json={'url':''}).json()=={'url':''}
    assert 'id="browser-open" hidden' in client.get('/knowledge/browser').text
