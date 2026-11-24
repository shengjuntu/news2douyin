"""Rebuild through the real API and worker from exact Events, Mentions and GKG keys."""

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from news2douyin.desk import focus_event_runtime as runtime, service as desk
from news2douyin.desk.focus_event_models import FocusEventGraph
from news2douyin.desk.focus_models import FocusLead
from news2douyin.desk.models import GkgSource, Watch, WatchEvent
from news2douyin.extraction.models import FocusPoint
from news2douyin.intake.official_models import OfficialEvent, OfficialMention
from news2douyin.intake.official_projection import mention_key
from news2douyin.server.app import create_app
from news2douyin.storage.articles import identity_key
from news2douyin.storage.models import Article, ArticleEventLink, ArticleIdentity, Event, EventState
from news2douyin.storage.utils import dumps
from test_content_foundation import drain_graph, live_graph
from test_postgres_integration import postgres_url

KEY = 'w_spine'
SOURCE = 'https://press.example/story?id=7'
BAD = 'https://press.example/story?id=7&extra=1'
BUILD = '/api/graphs/focus/' + KEY + '/rebuild'
WINDOW = {'days':30, 'until':'2026-11-22'}
QUERIES = {'view':'focus', 'focus':KEY, 'limit':100}


def event(ident, day='2026-11-20'):
    return OfficialEvent(event_id=ident, event_date=day, event_code='043', event_root_code='04',
        actor1_name='RUSSIA', actor2_name='UKRAINE', actor1_country='RUS',
        action_place='Abu Dhabi', action_country_iso2='AE', latitude=24.45, longitude=54.38,
        feed='events_english', stamp='20261120000000', row_index=2)


def mention(ident, url):
    return OfficialMention(key=mention_key(ident,url), event_id=ident, doc_identifier=url,
        domain='press.example', source_name='Wire', last_seen='2026-11-20T10:00:00Z', confidence=85,
        first_feed='mentions_english', first_stamp='20261120100000', first_row_index=3,
        last_feed='mentions_english', last_stamp='20261120100000', last_row_index=8)


def lead(ident, *, focus=KEY, decision='pending', version=1):
    return FocusLead(key=ident, watch=focus, scope_version=version, candidate='gdelt:' + ident,
                    observed_at='2026-11-22T00:00:00Z', title='会谈线索', url=SOURCE,
                    provider='gdelt', decision=decision)


def seed(engine):
    with Session(engine) as session:
        for k,n in ((KEY,'俄乌焦点'),('w_other','其他焦点')):
            session.add(Watch(key=k, name=n, kind='事件', scope=n, question='原来的研究问题'))
        session.add(FocusPoint(key='outline_point', watch=KEY, parent=KEY, text='研究提纲保留', created='2026-11-20'))
        a = Article(article_key='a', url=SOURCE, title='会谈报道')
        e = Event(event_key='local_event', event_title='旧本地事件')
        session.add_all([a,e])
        session.flush()
        session.add(ArticleIdentity(identity_key=identity_key({'url':SOURCE}), article_key='a'))
        session.add(ArticleEventLink(article_key='a', event_key=e.event_key))
        session.add(GkgSource(article_key='a', record_id='gkg-777', attributes=dumps({
            'document_identifier':SOURCE, 'intake_feed':'gkg_english', 'intake_batch':'20261120100000',
            'intake_row':11, 'persons':['Alice','Bob'], 'orgs':['Delegation']})))
        session.add(WatchEvent(watch=KEY, event=e.event_key, choice='include'))
        session.add(EventState(event_key=e.event_key, source_notes_json=dumps({
            'a':{'source_ref':{'provider':'gdelt','event_id':'101'}}})))
        session.flush()
        session.add(desk.snapshot(session, e))
        session.add_all([event(i) for i in ('101','102','103','104','105','106')])
        session.add(event('107', '2025-10-28'))
        session.add_all([mention('102', SOURCE), mention('101', SOURCE), mention('104', SOURCE), mention('106', BAD)])
        session.add_all([lead('103'),lead('104',decision='exclude'), lead('105',focus='w_other'),
                         lead('106'), lead('107',version=0)])
        session.commit()


@pytest.fixture(params=['sqlite','postgres'])
def app(request, tmp_path, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', '')
    url = (request.getfixturevalue('postgres_url') if request.param == 'postgres'
           else f'sqlite:///{tmp_path / "test.db"}')
    app = create_app(db_url=url, storage_root=str(tmp_path/'runs'))
    try:
        seed(app.state.engine)
        yield app
    finally:
        app.state.engine.dispose()


def test_rebuild_selects_official_events_and_both_lenses_share_exact_sources(app):
    client = TestClient(app)
    try:
        assert client.get('/api/graphs',params=QUERIES).json()['state']=='unbuilt'
        html = client.get('/graphs',params=QUERIES).text
        assert '重建焦点图' in html and BUILD in html
        assert client.post(BUILD+'?days=31').status_code==422
        assert client.post(BUILD,params=WINDOW,headers={'Origin':'https://outside.test'}).status_code==403
        assert client.post(BUILD,params=WINDOW).status_code==202
        engine, mirror = app.state.engine, app.state.desk.graph
        with Session(engine) as s:
            token=s.get(FocusEventGraph,KEY).token
        assert client.post(BUILD,params=WINDOW).json()['state']=='queued'
        with Session(engine) as s:
            assert s.get(FocusEventGraph,KEY).token==token
        assert runtime.step(engine,mirror)
        built = client.get('/api/graphs',params=QUERIES).json()
        assert built['state']=='ready' and built['counts']==dict(events=4,reports=2,gkg_reports=1,people=2,organizations=1)
        assert built['window']=={'date_from':'2026-10-24','date_to':'2026-11-22'}
        roots = {n['details']['external_id']:n for n in built['nodes'] if n['kind']=='SourceEvent'}
        assert set(roots)=={'101','102','103','106'}
        assert roots['101']['details']['basis']=='焦点材料保存的官方编号'
        assert roots['102']['details']['basis'].endswith('待核')
        assert {n['kind'] for n in built['nodes']}=={
            'SourceEvent','GdeltActor','GdeltPlace','GdeltDocument','Person','Organization'}
        doc = next(n for n in built['nodes'] if n['url']==SOURCE)
        near = next(n for n in built['nodes'] if n['url']==BAD)
        assert doc['details']['gkg_record']=='gkg-777' and 'gkg_record' not in near['details']
        asserted = [e for e in built['edges'] if e['kind']=='GKG_MENTIONS']
        assert {e['source'] for e in asserted}=={doc['id']}
        assert all(e['details']['row']==11 for e in asserted)
        assert any(e['kind']=='MENTIONED_IN' and e['target']==doc['id'] and e['details']['row']==8 for e in built['edges'])
        people=client.get('/api/graphs',params={**QUERIES,'lens':'people'}).json()
        assert {e['id'] for e in people['edges']} <= {e['id'] for e in built['edges']}
        assert {n['title'] for n in people['nodes'] if n['kind']=='Person'}=={'Alice','Bob'}
        assert set(e['kind'] for e in people['edges'])=={'MENTIONED_IN','GKG_MENTIONS'}
        with Session(engine) as s:
            assert s.get(FocusPoint,'outline_point').text=='研究提纲保留'
            assert s.get(Watch,KEY).question=='原来的研究问题'
        assert client.get('/api/graphs',params={**QUERIES,'focus':'w_other'}).json()['state']=='unbuilt'
    finally:
        client.close()


def test_rebuild_failure_keeps_completed_graph_and_can_retry(app, monkeypatch):
    engine, mirror=app.state.engine,app.state.desk.graph
    with Session(engine) as session:
        runtime.request(session, KEY, **WINDOW)
    assert runtime.step(engine,mirror)
    with Session(engine) as session:
        first=runtime.read(session,KEY,lens='all',limit=100)
        runtime.request(session,KEY,days=30,until='2025-10-29')
    with monkeypatch.context() as context:
        def fail(*_):
            raise RuntimeError('controlled worker failure')
        context.setattr(runtime,'build',fail)
        with pytest.raises(RuntimeError,match='controlled'):
            runtime.step(engine,mirror)
    with Session(engine) as session:
        waiting=runtime.read(session,KEY,lens='all',limit=100)
        assert waiting['state']=='error' and waiting['nodes']==first['nodes']
        runtime.request(session,KEY,days=30,until='2025-10-29')
    assert runtime.step(engine,mirror)
    with Session(engine) as session:
        revised=runtime.read(session,KEY,lens='all',limit=100)
        assert revised['state']=='ready' and revised['nodes']==[]


def test_native_property_graph_retires_only_the_selected_legacy_projection(app, live_graph, monkeypatch):
    from news2douyin.desk.graph import GraphMirror

    engine, mirror = app.state.engine, GraphMirror()
    mirror.bootstrap(engine)
    mirror.query('focus_'+KEY, 'CREATE (:Question {id:"legacy"})')
    mirror.query('focus_w_other', 'CREATE (:Question {id:"other"})')
    with Session(engine) as session:
        runtime.request(session, KEY, **WINDOW)
    with monkeypatch.context() as context:
        def fail_after_first_write(mirror, stage, _key, _snapshot, _lease):
            mirror.query(stage, 'CREATE (:Incomplete)')
            raise RuntimeError('interrupted native write')
        context.setattr(runtime, '_mirror', fail_after_first_write)
        with pytest.raises(RuntimeError, match='interrupted native'):
            runtime.step(engine, mirror)
    assert live_graph.exists(mirror.prefix+'_focus_'+KEY)
    with Session(engine) as session:
        assert runtime.status(session,KEY)['state']=='error'
        runtime.request(session, KEY, **WINDOW)
    assert runtime.step(engine, mirror)
    with Session(engine) as session:
        data=runtime.read(session,KEY,lens='all',limit=100)
    name=data['graph']
    assert mirror.read(name,'MATCH (n:SourceEvent) RETURN count(n)')==[[4]]
    assert mirror.read(name,'MATCH (:GdeltDocument)-[r:GKG_MENTIONS]->(:Person) RETURN count(r)')==[[2]]
    assert mirror.read(name,'MATCH (n:Question) RETURN count(n)')==[[0]]
    assert mirror.read(name,'MATCH (n:Incomplete) RETURN count(n)')==[[0]]
    assert runtime.step(engine, mirror)
    assert not live_graph.exists(mirror.prefix+'_focus_'+KEY)
    assert live_graph.exists(mirror.prefix+'_focus_w_other')
    assert live_graph.exists(mirror.prefix+'_active_global')
    # A subsequent change to the original focus updates only global tracking.
    app.state.desk.graph = mirror
    with Session(engine) as session:
        desk.enqueue_graph(session, 'watch/'+KEY)
        session.commit()
    drain_graph(app)
    assert not live_graph.exists(mirror.prefix+'_focus_'+KEY)
    assert mirror.read('active_global',
        'MATCH (:Focus {id:$w})-[:TRACKS]->(e:Event) RETURN e.id', w=KEY)==[['local_event']]
