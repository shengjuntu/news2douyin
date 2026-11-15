"""A removed topic stops tracking without deleting shared evidence or results."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.desk import service as desk, focus_jobs
from news2douyin.desk.focus_lifecycle import is_deleted, visible_watch
from news2douyin.desk.focus_models import FocusBackfill, FocusCandidate, FocusTrash
from news2douyin.desk.leases import acquire
from news2douyin.desk.models import Watch, WatchEvent, WatchReview
from news2douyin.research.models import ResearchCase
from news2douyin.storage.models import Article, Event
from news2douyin.workbench.models import ContentTask
from test_content_foundation import app_at, article, process, live_graph, drain_graph
from test_postgres_migration import target


@pytest.fixture(params=('sqlite','postgres'))
def focus(tmp_path, monkeypatch, request):
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', '')
    monkeypatch.setenv('NEWS2DOUYIN_GKG_STREAM_ENABLED', '0')
    from news2douyin.server.app import create_app
    url=request.getfixturevalue('target') if request.param=='postgres' else 'sqlite:///'+str(tmp_path/'news.db')
    app=create_app(db_url=url,storage_root=str(tmp_path/'runs'))
    client = TestClient(app)
    made = client.post('/api/focus', json=dict(name='特朗普跟踪', scope='Trump'))
    assert made.status_code == 201, made.text
    yield app, client, made.json()['key']
    app.state.engine.dispose()


def remove(client, key, version=1, **kw):
    return client.request('DELETE', '/api/focus/'+key, json=dict(version=version), **kw)


def test_recoverable_remove_hides_topic_but_preserves_shared_material_and_work(focus):
    app, client, key = focus
    article(app, 'a', 'Trump visited Paris yesterday.', 'Trump visit')
    process(app)
    assert client.post('/api/focus/'+key+'/links', json=dict(event='e_a', action='include')).status_code == 200
    made = client.post('/api/workbench', json=dict(request_id='focus-task-kept', watch=key))
    assert made.status_code == 201, made.text
    task = made.json()['key']
    with Session(app.state.engine) as s:
        pending = desk.make_review(s, s.get(Watch,key), '', 'summary-before-delete')
        review = pending.key
        s.commit()
    response = remove(client,key)
    assert response.status_code == 200, response.text
    assert remove(client,key).status_code == 200  # A lost acknowledgement is safe.
    assert client.get('/focus/'+key).status_code == 404
    page = client.get('/focus')
    assert page.status_code == 200 and 'id="focus-trash"' in page.text
    assert ('data-focus-restore="'+key+'"') in page.text
    assert ('href="/focus/'+key+'"') not in page.text
    assert client.get('/api/focus/'+key+'/tracking').status_code == 404
    assert client.get('/api/knowledge/graph?watch='+key).status_code == 404
    assert client.post('/api/focus/'+key+'/seen',json={}).status_code == 404
    assert client.post('/api/focus/'+key+'/links',json=dict(event='e_a',action='include')).status_code == 404
    assert client.get('/api/focus/trash').json()['items'][0]['key'] == key
    assert client.get('/workbench/'+task+'/selection').status_code == 200
    with Session(app.state.engine) as s:
        w=s.get(Watch,key)
        assert is_deleted(s,key) and not w.enabled and not w.auto_research
        assert not s.exec(select(Watch).where(visible_watch())).all()
        assert s.get(WatchReview,review).status == 'cancelled'
        assert s.get(Article,1) is not None
        assert s.exec(select(Event).where(Event.event_key=='e_a')).first() is not None
        assert s.get(ContentTask,task) is not None
        assert s.get(WatchEvent,(key,'e_a')) is not None
        assert s.exec(select(ResearchCase)).all()
        assert desk.memberships_many(s,['e_a'])['e_a'] == []
    restored = client.post('/api/focus/'+key+'/restore',json={'version':2})
    assert restored.status_code == 200 and restored.json()['enabled'] is False
    assert client.get('/focus/'+key).status_code == 200
    assert client.get('/api/focus/trash').json()['items'] == []
    with Session(app.state.engine) as s:
        assert not is_deleted(s,key) and not s.get(Watch,key).enabled
        assert s.get(FocusBackfill,key).status == 'paused'
        assert s.get(WatchReview,review).status == 'cancelled'
        assert s.get(WatchEvent,(key,'e_a')) is not None


def test_remove_checks_version_origin_and_does_not_silently_start_after_restore(focus):
    app, client, key = focus
    assert remove(client,key,version=99).status_code == 409
    assert remove(client,key,headers={'Origin':'https://evil.example'}).status_code == 403
    assert remove(client,key).status_code == 200
    assert client.post('/api/focus/'+key+'/restore',json={'version':2},headers={'Origin':'https://evil.example'}).status_code == 403
    assert client.post('/api/focus/'+key+'/restore',json={'version':2}).status_code == 200
    # Old edit/delete tabs cannot restart or remove a restored generation.
    assert remove(client,key).status_code == 409
    with Session(app.state.engine) as s:
        assert s.get(Watch,key).version == 3
        assert s.get(Watch,key).enabled is False


def test_submission_lease_and_pending_candidates_are_fenced(focus):
    app, client, key = focus
    with acquire(app.state.engine,'watch-lifecycle/'+key,120) as lease:
        assert lease is not None
        blocked = remove(client,key)
        assert blocked.status_code == 409 and '提交研究' in blocked.json()['detail']
    with Session(app.state.engine) as s:
        s.add(FocusCandidate(key='candidate',watch=key,event='event',scope_version=1,event_fingerprint='fp',status='running'))
        s.commit()
    assert remove(client,key).status_code == 200
    with Session(app.state.engine) as s:
        assert s.get(FocusCandidate,'candidate').status == 'superseded'
    assert focus_jobs.judge_step(app.state.engine) is False


def test_deleted_build_result_and_name_cannot_recreate_topic(focus):
    app, client, key = focus
    assert remove(client,key).status_code == 200
    duplicate = client.post('/api/focus',json=dict(name='特朗普跟踪',scope='Trump'))
    assert duplicate.status_code == 422 and '回收站' in duplicate.json()['detail']
    assert client.put('/api/focus/'+key,json=dict(version=2,name='复活',scope='Trump')).status_code == 404
    assert client.post('/api/focus/'+key+'/tracking/retry',json={}).status_code == 404
    assert client.post('/api/focus/'+key+'/outline',json=dict(text='新问题')).status_code == 404


def test_delete_dialog_is_escaped_and_has_explicit_recovery(focus):
    app, client, key = focus
    page = client.get('/focus/'+key)
    assert page.status_code == 200
    for control in ('focus-delete','focus-delete-dialog','focus-delete-confirm','focus-delete-cancel'):
        assert ('id="'+control+'"') in page.text
    assert '移入回收站后停止跟踪' in page.text
    assert '已提交的独立研究任务' in page.text


def test_live_graph_archive_preserves_material_and_restore_clears_marker(tmp_path,live_graph,monkeypatch):
    from news2douyin.desk import graph_indexes
    monkeypatch.setattr(graph_indexes,'POLL_INTERVAL',0)
    app=app_at(tmp_path)
    client=TestClient(app)
    article(app,'a','Trump visited Paris yesterday.','Trump visit')
    process(app)
    key=client.post('/api/focus',json=dict(name='图存档',scope='Trump')).json()['key']
    client.post('/api/focus/'+key+'/links',json=dict(event='e_a',action='include'))
    drain_graph(app)
    mirror=app.state.desk.graph
    assert mirror.read('focus_'+key,'MATCH (e:Event) RETURN e.id') == [['e_a']]
    assert remove(client,key).status_code==200
    drain_graph(app)
    assert mirror.read('active_global','MATCH (f:Focus {id:$id}) WHERE f.archived=true AND f.enabled=false RETURN f.id',id=key)==[[key]]
    assert mirror.read('focus_'+key,'MATCH (q:Question {id:$id}) WHERE q.archived=true RETURN q.id',id=key)==[[key]]
    assert mirror.read('focus_'+key,'MATCH (e:Event) RETURN e.id') == [['e_a']]
    assert mirror.read('active_global','MATCH (e:Event {id:"e_a"}) RETURN e.id') == [['e_a']]
    assert client.post('/api/focus/'+key+'/restore',json={'version':2}).status_code==200
    drain_graph(app)
    assert mirror.read('active_global','MATCH (f:Focus {id:$id}) WHERE f.archived=false AND f.enabled=false RETURN f.id',id=key)==[[key]]
    assert mirror.read('focus_'+key,'MATCH (q:Question {id:$id}) WHERE q.archived=false RETURN q.id',id=key)==[[key]]
    assert mirror.read('focus_'+key,'MATCH (e:Event) RETURN e.id') == [['e_a']]
    app.state.engine.dispose()


def test_restored_paused_topic_retains_accepted_membership_baseline(focus):
    from news2douyin.desk.focus_models import FocusDefinition
    app,client,key=focus
    article(app,'a','Trump visited Paris yesterday.','Trump visit')
    process(app)
    with Session(app.state.engine) as s:
        s.add(FocusDefinition(watch=key,description='Trump',spec_json='{}',scope_version=1))
        s.merge(WatchEvent(watch=key,event='e_a',choice='auto',relevance=2,reason='已核对'))
        s.add(FocusCandidate(key='accepted',watch=key,event='e_a',scope_version=1,event_fingerprint='f',
            status='done',decision='core'))
        s.commit()
        assert desk.focus_entries(s,key)[0]==1
    assert remove(client,key).status_code==200
    assert client.post('/api/focus/'+key+'/restore',json={'version':2}).status_code==200
    with Session(app.state.engine) as s:
        assert desk.focus_entries(s,key)[0]==1
        # An edit/resume must expire the preserved baseline.
        watch=s.get(Watch,key);watch.version+=1;s.add(watch);s.commit()
        assert desk.focus_entries(s,key)[0]==0


def test_deletion_while_model_is_running_cannot_publish_new_membership(focus,monkeypatch):
    from news2douyin.desk import intent
    from test_focus_jobs import watch as natural_watch,card,candidate
    app,client,_legacy=focus
    key=natural_watch(app.state.engine,monkeypatch)
    event=card(app.state.engine,7)
    candidate(app.state.engine,key,event)
    observed=[]
    def judge(*args):
        observed.append(True)
        response=remove(client,key)
        assert response.status_code==200,response.text
        return dict(decision='core',method='model',reason='本应因删除丢弃',evidence=[],questions=[])
    monkeypatch.setattr(intent,'judge',judge)
    assert focus_jobs.judge_step(app.state.engine)
    assert observed
    with Session(app.state.engine) as s:
        assert is_deleted(s,key)
        assert s.get(WatchEvent,(key,event)) is None
        rows=s.exec(select(FocusCandidate).where(FocusCandidate.watch==key)).all()
        assert rows and all(row.status=='superseded' for row in rows)


def test_delayed_restore_cannot_revive_a_later_deletion(focus):
    from concurrent.futures import ThreadPoolExecutor
    app,client,key=focus
    assert remove(client,key).status_code==200
    assert client.post('/api/focus/'+key+'/restore',json={}).status_code==422
    initial={'version':2}
    assert client.post('/api/focus/'+key+'/restore',json=initial).status_code==200
    assert client.post('/api/focus/'+key+'/restore',json=initial).status_code==200
    with Session(app.state.engine) as s:
        assert s.get(Watch,key).version==3
    # A delayed retry from the old recycle-bin page may arrive on either side
    # of a new removal. In both serializations the new removal stays removed.
    with ThreadPoolExecutor(max_workers=2) as pool:
        newer=pool.submit(remove,client,key,3)
        stale=pool.submit(client.post,'/api/focus/'+key+'/restore',json=initial)
        assert newer.result().status_code==200
        assert stale.result().status_code in (200,409)
    assert client.post('/api/focus/'+key+'/restore',json=initial).status_code==409
    with Session(app.state.engine) as s:
        assert is_deleted(s,key)
        assert s.get(Watch,key).version==4 and s.get(Watch,key).enabled is False
    current=client.get('/api/focus/trash').json()['items'][0]
    assert current['version']==4
    assert client.post('/api/focus/'+key+'/restore',json={'version':current['version']}).status_code==200
