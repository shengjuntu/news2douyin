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
        a = Article(article_key='a', url=SOURCE, title='会谈报道', published_at='2026-11-20T10:00:00Z')
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
        assert built['state']=='ready'
        assert built['counts']==dict(events=5, local_events=1, official_events=4, reports=2, report_leads=0,
                                     gkg_reports=1, people=2, organizations=1, providers={'worldnewsapi':1,'gdelt':1})
        assert built['window']=={'date_from':'2026-10-24','date_to':'2026-11-22'}
        roots = {n['details']['external_id']:n for n in built['nodes'] if n['kind']=='SourceEvent'}
        assert set(roots)=={'101','102','103','106'}
        assert roots['101']['details']['basis']=='焦点材料保存的官方编号'
        assert roots['102']['details']['basis'].endswith('待核')
        assert {n['kind'] for n in built['nodes']}=={
            'SourceEvent','GdeltActor','GdeltPlace','GdeltDocument','Person','Organization','Event','Article'}
        doc = next(n for n in built['nodes'] if n['details'].get('source_url')==SOURCE)
        near = next(n for n in built['nodes'] if n['url']==BAD)
        assert doc['kind']=='Article' and 'gkg_record' not in near['details']
        asserted = [e for e in built['edges'] if e['kind']=='GKG_MENTIONS']
        assert {e['source'] for e in asserted}=={doc['id']}
        assert any(e['kind']=='HAS_OFFICIAL_SOURCE' and e['target']==roots['101']['id'] for e in built['edges'])
        assert all(e['details']['row']==11 for e in asserted)
        assert any(e['kind']=='MENTIONED_IN' and e['target']==doc['id'] and e['details']['row']==8 for e in built['edges'])
        people=client.get('/api/graphs',params={**QUERIES,'lens':'people'}).json()
        assert {e['id'] for e in people['edges']} <= {e['id'] for e in built['edges']}
        assert {n['title'] for n in people['nodes'] if n['kind']=='Person'}=={'Alice','Bob'}
        assert set(e['kind'] for e in people['edges'])=={'CITES','MENTIONED_IN','GKG_MENTIONS'}
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
    assert mirror.read(name,'MATCH (:Article)-[r:GKG_MENTIONS]->(:Person) RETURN count(r)')==[[2]]
    assert mirror.read(name,'MATCH (:Event)-[:CITES]->(a:Article) RETURN a.provider')==[['worldnewsapi']]
    assert mirror.read(name,'MATCH (:Event)-[:HAS_OFFICIAL_SOURCE]->(e:SourceEvent) RETURN e.external_id')==[['101']]
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


def test_worldnewsapi_focus_without_any_gdelt_keeps_events_and_distinguishes_report_leads(app):
    from news2douyin.extraction.models import MentionReview, TextMention
    from news2douyin.extraction.schema import Candidate, grounded, passages
    from news2douyin.storage.articles import latest_version, record_version

    engine = app.state.engine
    # This focus has no shared articles with the official-event fixture.
    key = 'w_nvidia'
    quoted = '英伟达宣布了新产品。'
    with Session(engine) as session:
        session.add(Watch(key=key, name='黄仁勋与英伟达', kind='事件', scope='英伟达'))
        article = Article(article_key='wn', provider='worldnewsapi', title='黄仁勋与英伟达',
                          url='https://nvidia.example/news', content=quoted, published_at='2026-11-20T10:00:00Z')
        session.add(article)
        session.flush()
        record_version(session, article)
        session.flush()
        version = latest_version(session, 'wn')
        good = grounded(Candidate(block='P2', quote=quoted, trigger='宣布', kind='statement',
            status='reported', time='', arguments=[dict(role='actor', text='英伟达', entity_type='organization')]),
            passages({'title':article.title, 'content':quoted}), 'wn', version.revision, article.published_at)
        session.add(TextMention(id=good['id'], version_id=version.id, article='wn',
            revision=version.revision, data=dumps(good)))
        session.add(MentionReview(id=good['id'], decision='accepted'))
        for name, day, choice in (('picked','2026-11-20','include'), ('excluded','2026-11-20','exclude'),
                                  ('outdated','2026-10-03','include')):
            item = Event(event_key=name,event_title='新产品发布 '+name)
            session.add(item)
            session.add(ArticleEventLink(event_key=name, article_key='wn'))
            session.add(WatchEvent(watch=key, event=name, choice=choice))
            session.flush()
            card = desk.snapshot(session, item)
            card.seen = day+'T00:00:00Z'
            session.add(card)
        for name, url, version, decision in (
            ('fresh', 'https://nvidia.example/interview', 1, 'pending'),
            ('same-report', article.url, 1, 'core'),
            ('other-version', 'https://nvidia.example/earlier-focus', 0, 'core'),
            ('rejected', 'https://nvidia.example/rejected', 1, 'exclude')):
            session.add(FocusLead(key=name, watch=key, scope_version=version, candidate='article:' + name,
                        observed_at='2026-11-21T01:00:00Z', title='黄仁勋专访', provider='worldnewsapi',
                        url=url, decision=decision))
        session.commit()
    client=TestClient(app)
    try:
        assert client.post('/api/graphs/focus/' + key + '/rebuild', params=WINDOW).status_code==202
        assert runtime.step(engine, app.state.desk.graph)
        query = {**QUERIES, 'focus': key}
        result = client.get('/api/graphs', params=query).json()
        assert result['state']=='ready'
        assert result['counts']==dict(events=1, local_events=1, official_events=0, reports=1, report_leads=1,
            gkg_reports=0, people=0, organizations=1, providers={'worldnewsapi':2})
        assert {row['id'] for row in result['nodes'] if row['kind']=='Event'}=={'Event:picked'}
        assert {row['id'] for row in result['nodes'] if row['kind']=='ArticleLead'}=={'ArticleLead:fresh'}
        report = next(row for row in result['nodes'] if row['id']=='Article:wn')
        assert report['url']=='/articles/wn' and report['details']['provider']=='worldnewsapi'
        people = client.get('/api/graphs', params={**query,'lens':'people'}).json()
        assert {row['kind'] for row in people['nodes']}=={'Event','Organization','Article'}
        found = next(row for row in people['edges'] if row['kind']=='EXTRACTED_MENTION')
        assert found['details']['evidence_path']=='/articles/wn?revision=1#m-'+good['id']
        assert found['details']['quote']==quoted
    finally:
        client.close()


def test_worldnewsapi_lead_without_local_event_is_visible_without_inventing_an_event(app):
    engine = app.state.engine
    key = 'w_only_lead'
    with Session(engine) as session:
        session.add(Watch(key=key,name='未整理报道',kind='事件',scope='英伟达'))
        session.add(FocusLead(key='loose',watch=key,scope_version=1,candidate='article:loose',
            observed_at='2026-11-21T00:00:00Z', title='英伟达简讯',provider='worldnewsapi',
            url='https://nvidia.example/brief'))
        session.flush()
        runtime.request(session, key, **WINDOW)
    assert runtime.step(engine, app.state.desk.graph)
    with Session(engine) as session:
        result=runtime.read(session,key,lens='all',limit=20)
        assert [row['kind'] for row in result['nodes']]==['ArticleLead']
        assert result['counts']['events']==0 and result['counts']['report_leads']==1
        assert result['edges']==[]


def test_rebuild_finds_older_collected_worldnews_report_before_focus_lead_scan(app, tmp_path, monkeypatch):
    from sqlmodel import select

    from news2douyin.candidates import service as candidates
    from news2douyin.candidates.models import SourceReport
    from news2douyin.collect import service as collect
    from news2douyin.desk.focus_leads import _Matcher, _recalled

    engine = app.state.engine
    key = 'w_catalogue'
    available = '2026-11-14T12:00:00Z'

    def report(slug, title):
        return dict(url='https://paper.example/' + slug, title=title,
                    content='生产和可用时间由厂商在发布会上介绍。', language='zh', country='us',
                    published_at=available, fetched_at=available, source={'domain':'paper.example'})

    samples = [
        report('nvidia', '黄仁勋介绍英伟达新产品'),
        report('nvidia-excluded', '英伟达另一则报道'),
        report('unrelated', '其他公司发布新产品'),
    ]
    monkeypatch.setattr(candidates, 'utc_now_iso', lambda: available)
    monkeypatch.setitem(collect.PROVIDERS, 'worldnewsapi', lambda profile: samples)
    with Session(engine) as session:
        collect.create_or_update_profile(session, dict(name='wna-history', provider='worldnewsapi',
                                         filter_mode='rules'))
        collected = collect.run_collection(session, 'wna-history', storage_root=tmp_path/'collected')
        assert collected.status == 'succeeded'
        assert session.exec(select(ArticleEventLink).where(
            ArticleEventLink.article_key.in_(select(Article.article_key).where(
                Article.url.in_([row['url'] for row in samples]))))).first() is None
        catalogue = {row.url: row for row in session.exec(select(SourceReport))}
        assert all(catalogue[item['url']].provider=='worldnewsapi' for item in samples)
        watch = Watch(key=key,name='黄仁勋和英伟达历史',kind='长期事件',scope='黄仁勋，英伟达')
        session.add(watch)
        # Match exactly the identity used by the live matcher and preserve an existing exclusion.
        [excluded] = _recalled(_Matcher([(watch,None)]), candidates._report_candidate(
            catalogue[samples[1]['url']]))
        excluded.decision = 'exclude'
        excluded_key = excluded.key
        source_key = catalogue[samples[0]['url']].key
        session.add(excluded)
        session.commit()
        assert session.exec(select(FocusLead).where(FocusLead.watch==key,
                            FocusLead.decision!='exclude')).first() is None

    client=TestClient(app)
    try:
        assert client.post('/api/graphs/focus/'+key+'/rebuild', params=WINDOW).status_code==202
        assert runtime.step(engine, app.state.desk.graph)
        result=client.get('/api/graphs',params={**QUERIES,'focus':key}).json()
        assert result['state']=='ready'
        assert result['counts']['report_leads']==1
        assert result['counts']['events']==0 and result['counts']['providers']=={'worldnewsapi':1}
        [found] = result['nodes']
        assert found['kind']=='ArticleLead' and found['url']==samples[0]['url']
        assert found['details']['candidate'] == source_key
        assert '待核' in found['details']['basis']
        with Session(engine) as session:
            [original] = session.exec(select(FocusLead).where(FocusLead.watch==key)).all()
            assert original.key==excluded_key and original.decision=='exclude'
        narrow = {'days':30, 'until':'2026-11-13'}
        assert client.post('/api/graphs/focus/'+key+'/rebuild', params=narrow).status_code==202
        assert runtime.step(engine, app.state.desk.graph)
        assert not client.get('/api/graphs',params={**QUERIES,'focus':key}).json()['nodes']
    finally:
        client.close()


def test_focus_recovers_verified_gkg_evidence_before_sql_projection(app):
    from pathlib import Path
    from zipfile import ZipFile

    from news2douyin.intake.lake import Lake
    from news2douyin.intake.projection import IntakeProjectionReceipt
    from sqlmodel import select

    engine, mirror = app.state.engine, app.state.desk.graph
    root = Path(app.state.storage_root)
    root.mkdir(exist_ok=True)
    url = 'https://press.example/unprojected?id=9'
    missing_url = 'https://press.example/outside-window?id=13'
    with Session(engine) as session:
        session.add_all([event('108'), lead('108'), mention('108', url), mention('108', missing_url)])
        session.commit()
        runtime.request(session, KEY, **WINDOW)

    stamps = ('20261120094500', '20261001090000')
    for stamp, documents in ((stamps[0], [url, url + '&variant=1']),
                             (stamps[1], [missing_url])):
        path = root / (stamp + '.zip')
        with ZipFile(path, 'w') as archive:
            raw = []
            for row, document in enumerate(documents, 1):
                fields = [''] * 27
                fields[:5] = [f'{stamp}-{row}', stamp, '1', 'press.example', document]
                fields[11] = 'Archived Person' if row == 1 and stamp == stamps[0] else 'Unrelated Person'
                fields[13] = 'Archived Organization'
                raw.append('\t'.join(fields) + '\n')
            archive.writestr(stamp + '.gkg.csv', ''.join(raw))
        info = Lake(root).ingest_archive('gkg_english', stamp, path)
        if stamp == stamps[0]:
            checksum = info['sha256']

    assert runtime.step(engine, mirror, storage_root=root)
    view = TestClient(app).get('/api/graphs', params={**QUERIES, 'lens':'people'}).json()
    assert view['state'] == 'ready' and view['counts']['gkg_reports'] == 2
    assert view['gkg_archive']['state'] == 'checked'
    assert view['gkg_archive']['used_urls'] == 1
    names = {node['title'] for node in view['nodes']}
    assert {'Archived Person','Archived Organization'} <= names
    assert 'Unrelated Person' not in names
    raw_edges = [edge['details'] for edge in view['edges'] if edge['details'].get('gkg_source') == 'archive']
    assert len(raw_edges) == 2
    assert all(edge['batch'] == stamps[0] and edge['archive_sha256'] == checksum
               and edge['row'] == 1 and len(edge['raw_sha256']) == 64 for edge in raw_edges)
    with Session(engine) as session:
        assert session.get(ArticleIdentity, identity_key({'url':url})) is None
        assert session.exec(select(IntakeProjectionReceipt).limit(1)).first() is None
