"""The status page must remain usable with a single DB connection."""
from fastapi.testclient import TestClient
from sqlalchemy import event

from news2douyin.server.app import create_app


def test_configured_graph_status_reuses_one_connection(tmp_path,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_DB_POOL_SIZE','1')
    monkeypatch.setenv('NEWS2DOUYIN_DB_MAX_OVERFLOW','0')
    monkeypatch.setenv('NEWS2DOUYIN_DB_POOL_TIMEOUT','0')
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL','redis://127.0.0.1:1')
    app=create_app(db_url='sqlite:///'+str(tmp_path/'status.db'),storage_root=str(tmp_path/'runs'))
    client=TestClient(app)
    checked=0;maximum=0
    def checkout(*args):
        nonlocal checked,maximum
        checked+=1;maximum=max(maximum,checked)
    def checkin(*args):
        nonlocal checked
        checked-=1
    def forbidden(*args,**kwargs):raise AssertionError('GET must not contact graph server')
    monkeypatch.setattr(app.state.desk.graph,'query',forbidden)
    monkeypatch.setattr(app.state.desk.graph,'read',forbidden)
    event.listen(app.state.engine,'checkout',checkout)
    event.listen(app.state.engine,'checkin',checkin)
    try:
        for path in ('/api/knowledge/status','/knowledge','/radar','/focus'):
            response=client.get(path)
            assert response.status_code==200,(path,response.text)
        assert maximum==1 and checked==0
    finally:
        event.remove(app.state.engine,'checkout',checkout)
        event.remove(app.state.engine,'checkin',checkin)
        client.close();app.state.engine.dispose()
