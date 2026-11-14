"""Maintenance controls preserve scan checkpoints and never touch graph data."""
from fastapi.testclient import TestClient
from sqlmodel import Session

from news2douyin.desk import graph_gc
from news2douyin.desk.graph import GraphMirror
from news2douyin.desk.graph_maintenance_models import GraphGcState
from news2douyin.desk.models import GraphPending
from test_content_foundation import app_at


def test_pause_resume_is_persistent_same_origin_and_preserves_work(tmp_path,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_GRAPH_GC_ENABLED','1')
    app=app_at(tmp_path);client=TestClient(app);mirror=app.state.desk.graph
    mirror.url='redis://graph.invalid'
    mirror._maintenance_error='TimeoutError'
    def forbidden(*_args,**_kwargs):
        raise AssertionError('HTTP maintenance control contacted FalkorDB')
    for method in ('query','read','explain'):
        monkeypatch.setattr(mirror,method,forbidden)
    with Session(app.state.engine) as session:
        row=GraphGcState(key='gc-fixture',namespace=mirror.prefix,graph='active_global',label='Article',
            cursor=942,through_key=1654,cycle=3,status='sweeping',phase='batch',scanned=500,deleted=7,
            cycle_scanned=25,cycle_deleted=2,next_due=0)
        session.add(row);session.add(GraphPending(key='event/pending',token='new',written='old'));session.commit()
        before=session.get(GraphGcState,'gc-fixture').model_dump()
    root='/api/desk/graph-maintenance/'
    for action in ('pause','resume'):
        assert client.get(root+action).status_code==405
        assert client.post(root+action,headers={'Origin':'https://evil.example'}).status_code==403
    assert graph_gc.control(app.state.engine,mirror)['paused'] is False
    response=client.post(root+'pause')
    assert response.status_code==200,response.text
    assert response.json()['paused'] is True
    assert client.post(root+'pause').json()['paused'] is True
    other=GraphMirror();other.url=mirror.url;other.prefix=mirror.prefix
    assert graph_gc.control(app.state.engine,other)['paused'] is True
    response=client.get('/api/knowledge/status')
    assert response.status_code==200,response.text
    assert response.json()['gc']['state']=='paused'
    assert response.json()['gc']['complete_labels']==0
    assert '图清理：已暂停' in client.get('/knowledge').text
    response=client.post(root+'resume')
    assert response.status_code==200,response.text
    assert response.json()['paused'] is False
    with Session(app.state.engine) as session:
        assert session.get(GraphGcState,'gc-fixture').model_dump()==before
        pending=session.get(GraphPending,'event/pending')
        assert pending.token=='new' and pending.written=='old'
    app.state.engine.dispose()


def test_resume_does_not_override_environment_off_switch(tmp_path,monkeypatch):
    app=app_at(tmp_path);client=TestClient(app);mirror=app.state.desk.graph
    mirror.url='redis://graph.invalid'
    monkeypatch.setenv('NEWS2DOUYIN_GRAPH_GC_ENABLED','0')
    assert client.post('/api/desk/graph-maintenance/pause').json()['paused'] is True
    response=client.post('/api/desk/graph-maintenance/resume')
    assert response.status_code==409
    control=graph_gc.control(app.state.engine,mirror)
    assert control['enabled'] is False and control['paused'] is True
    assert client.get('/api/knowledge/status').json()['gc']['state']=='disabled'
    mirror.url=''
    for action in ('pause','resume'):
        assert client.post('/api/desk/graph-maintenance/'+action).status_code==409
    app.state.engine.dispose()
