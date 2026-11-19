"""Selected-source graph scope, exact identifiers, bounded replay and raw provenance."""
import json

import pytest

from sqlmodel import Session, select

from news2douyin.desk.graph import GraphMirror
from news2douyin.desk.models import GraphPending, GkgSource
from news2douyin.desk.official_graph import DOCUMENT_LIMIT, refresh_tracked, request, status, track
from news2douyin.intake.official_models import OfficialEvent, OfficialMention
from news2douyin.intake.official_projection import mention_key
from news2douyin.storage.articles import identity_key
from news2douyin.storage.models import Article, ArticleIdentity
from test_attention_graph import settle
from test_content_foundation import app_at, live_graph
from test_postgres_integration import postgres_url


@pytest.fixture(params=['sqlite','postgres'])
def source_app(request,tmp_path):
    if request.param=='sqlite':
        app=app_at(tmp_path)
    else:
        from news2douyin.server.app import create_app
        app=create_app(db_url=request.getfixturevalue('postgres_url'),storage_root=str(tmp_path/'runs'))
    try:
        yield app
    finally:
        app.state.engine.dispose()


def coded(key):
    return OfficialEvent(event_id=key,event_date='20261114',event_code='043',event_root_code='04',
                         actor1_name='ALPHA',actor2_name='BETA',actor1_country='USA',action_place='London',
                         feed='events_english',stamp='20261114090000',row_index=7)


def mentioned(key,url,num):
    return OfficialMention(key=mention_key(key,url),event_id=key,doc_identifier=url,domain='paper.example',
        first_seen='2026-11-14T08:00:00Z',last_seen=f'2026-11-14T09:{num:02d}:00Z',confidence=86,
        first_feed='mentions_english',first_stamp='20261114090000',first_row_index=1,
        last_feed='mentions_english',last_stamp='20261114091500',last_row_index=num+1)


def test_invalidation_enrolls_only_per_explicit_intent(tmp_path):
    engine=app_at(tmp_path).state.engine
    with Session(engine) as s:
        s.add(coded('111'));s.add(coded('222'));s.commit()
        refresh_tracked(s,['111','222']);s.commit()
        assert status(s,'111')['state']=='not_requested'
        assert not track(s,'333')
        assert request(s,'111')['state']=='queued'
        s.commit()
        row=s.get(GraphPending,'official/111')
        row.written=row.token;s.commit()
        assert status(s,'111')['state']=='ready'
        refresh_tracked(s,['111','222']);s.commit()
        assert status(s,'111')['state']=='pending' and status(s,'222')['state']=='not_requested'


def test_selected_projection_ids_dedup_exact_link_evidence_and_recovery(tmp_path,live_graph,monkeypatch):
    engine=app_at(tmp_path).state.engine
    mirror=GraphMirror()
    url='https://paper.example/news/story'
    with Session(engine) as s:
        for key in ('111','222','333'):
            s.add(coded(key))
            s.add(mentioned(key,url,50))
        for n in range(DOCUMENT_LIMIT+2):
            s.add(mentioned('111',url+'/'+str(n),n))
        source=Article(article_key='art',url=url)
        s.add(source)
        s.add(ArticleIdentity(identity_key=identity_key({'url':url}),article_key='art'))
        s.add(GkgSource(article_key='art',record_id='gkg-record',attributes=json.dumps(
            dict(document_identifier=url,intake_feed='gkg_english',intake_batch='20261114090000',intake_row=4,
                 persons=['Only in this article'],orgs=['Only one organization']))))
        s.add(mentioned('222',url+'?utm_source=relay',52))
        s.commit()
        request(s,'111');request(s,'222');s.commit()
    from types import SimpleNamespace
    app=SimpleNamespace(state=SimpleNamespace(engine=engine))
    settle(app,mirror,monkeypatch)
    def read(query,**params):
        return mirror.read('active_global',query,**params)
    assert read('MATCH (e:SourceEvent) RETURN e.external_id ORDER BY e.external_id') == [['111'],['222']]
    assert read('MATCH (e:SourceEvent {key:"gdelt/111"}) RETURN e.projected_documents,e.has_more_documents') == [[DOCUMENT_LIMIT,'true']]
    assert read('MATCH (e:SourceEvent)-[r:MENTIONED_IN]->(d:GdeltDocument {identifier:$url}) '
                'RETURN count(e),count(DISTINCT d),max(r.last_row),max(r.confidence)',url=url) == [[2,1,51,86]]
    assert read('MATCH (d:GdeltDocument {identifier:$url}) RETURN d.gkg_record_id,d.gkg_row',url=url) == [['gkg-record',4]]
    assert read('MATCH (d:GdeltDocument {identifier:$url}) RETURN d.gkg_record_id',url=url+'?utm_source=relay') == [[None]]
    assert read('MATCH (:SourceEvent)-[:CODED_ACTOR1]->(a:GdeltActor) RETURN DISTINCT a.name') == [['ALPHA']]
    assert read('MATCH (:GdeltDocument {identifier:$url})-[:GKG_MENTIONS]->(n:GkgSubject) '
                'RETURN n.kind,n.name ORDER BY n.kind',url=url) == [['organization','Only one organization'],['person','Only in this article']]
    assert read('MATCH (:SourceEvent)-->(n:GkgSubject) RETURN count(n)') == [[0]]
    assert read('MATCH (:SourceEvent)-[:CODED_ACTION_AT]->(p:GdeltPlace) RETURN DISTINCT p.name') == [['London']]
    assert read('MATCH (e:Event) RETURN count(e)') == [[0]]
    assert read('MATCH (a:Article) RETURN count(a)') == [[0]]
    with Session(engine) as s:
        assert status(s,'111')['state']=='ready'
        for doc in s.exec(select(OfficialMention).where(OfficialMention.event_id=='111')):
            s.delete(doc)
        event=s.get(OfficialEvent,'111');event.actor1_name='GAMMA';s.add(event)
        refresh_tracked(s,['111']);s.commit()
    settle(app,mirror,monkeypatch)
    assert read('MATCH (e:SourceEvent {key:"gdelt/111"})-[r:MENTIONED_IN]->() RETURN count(r)') == [[0]]
    assert read('MATCH (d:GdeltDocument) RETURN count(d)') == [[2]]
    assert read('MATCH (:SourceEvent {key:"gdelt/111"})-[:CODED_ACTOR1]->(a:GdeltActor) RETURN a.name') == [['GAMMA']]
    assert read('MATCH (:SourceEvent {key:"gdelt/222"})-[:MENTIONED_IN]->(d) RETURN count(d)') == [[2]]
    assert read('MATCH (:GdeltDocument)-[:GKG_MENTIONS]->(n:GkgSubject) RETURN count(n)') == [[2]]
    live_graph.execute_command('GRAPH.DELETE',mirror.prefix+'_active_global')
    mirror.bootstrapped=False
    settle(app,mirror,monkeypatch)
    assert read('MATCH (e:SourceEvent) RETURN count(e)') == [[2]]
    assert read('MATCH (:SourceEvent)-[:MENTIONED_IN]->(d) RETURN count(d)') == [[2]]

    with Session(engine) as s:
        for doc in s.exec(select(OfficialMention).where(OfficialMention.event_id=='222')):
            s.delete(doc)
        refresh_tracked(s,['222']);s.commit()
    settle(app,mirror,monkeypatch)
    assert read('MATCH (d:GdeltDocument) RETURN count(d)') == [[0]]
    assert read('MATCH (n:GkgSubject) RETURN count(n)') == [[0]]


def test_delayed_gkg_archive_only_refreshes_selected_roots(tmp_path,live_graph,monkeypatch,source_app):
    from news2douyin.intake.projection import project_step
    from news2douyin.knowledge.service import signature
    from test_intake_projection import archive,line

    app=source_app
    engine=app.state.engine
    mirror=GraphMirror()
    stamp='20261114123000'
    url='https://media.example/article/late'
    with Session(engine) as s:
        s.add(coded('111'));s.add(coded('222'))
        s.add(mentioned('111',url,5));s.add(mentioned('222',url,5));s.commit()
        request(s,'111');s.commit()
    settle(app,mirror,monkeypatch)
    with Session(engine) as s:
        before=signature(s)
        assert status(s,'111')['state']=='ready'
    assert mirror.read('active_global','MATCH (d:GdeltDocument) RETURN d.gkg_record_id') == [[None]]
    archive(engine,tmp_path/'intake-root',[line(stamp,1,'A later GKG report',url)],stamp=stamp)
    assert project_step(engine,tmp_path/'intake-root')
    with Session(engine) as s:
        assert status(s,'111')['state']=='pending'
        assert status(s,'222')['state']=='not_requested'
        assert signature(s)==before
    settle(app,mirror,monkeypatch)
    assert mirror.read('active_global','MATCH (d:GdeltDocument)-[:GKG_MENTIONS]->(n:GkgSubject) '
                       'RETURN d.gkg_record_id,n.kind,n.name ORDER BY n.kind') == [
                           [stamp+'-1','organization','Example Organization'],[stamp+'-1','person','Alice']]
    with Session(engine) as s:
        assert signature(s)==before
