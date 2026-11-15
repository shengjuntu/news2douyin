"""Source event -> actual report preview -> reading and frozen editorial task.

Both databases use disposable state. Real HTTP is loopback-only via a controlled
public-IP transport mapping; none of these synthetic reports assert real news.
"""
from datetime import datetime,timezone
import html
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sql_event,func
from sqlmodel import Session,select

from event_content_test_support import (public_http_transport,HOST,LOCAL_URL,REMOTE_URL,BLOCKED_URL,
    LOCAL_TITLE,LOCAL_TEXT,REMOTE_TITLE,REMOTE_EXCERPT,REMOTE_HTML)
from news2douyin.intake.official_models import OfficialEvent,OfficialMention
from news2douyin.server.app import create_app
from news2douyin.storage.articles import identity_key
from news2douyin.storage.models import Article,ArticleIdentity,Event
from news2douyin.topics.models import TopicSource
from news2douyin.translations import service as translations
from news2douyin.tasks.worker import TaskWorker
from sql_readonly_audit import is_read_only_execution
from test_postgres_integration import postgres_url


@pytest.fixture(params=['sqlite','postgres'])
def content_case(request,tmp_path,monkeypatch):
    for name in ('NEWS2DOUYIN_GKG_STREAM_ENABLED','NEWS2DOUYIN_GKG_PROJECTION_ENABLED',
                 'NEWS2DOUYIN_GDELT_STREAM_ENABLED','NEWS2DOUYIN_GDELT_PROJECTION_ENABLED'):
        monkeypatch.setenv(name,'0')
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL','')
    monkeypatch.setenv('NEWS2DOUYIN_SOURCE_CONTENT_ENABLED','1')
    url=request.getfixturevalue('postgres_url') if request.param=='postgres' else 'sqlite:///'+str(tmp_path/'content.db')
    app=create_app(db_url=url,storage_root=str(tmp_path/'runs'))
    client=TestClient(app)  # Workers advance explicitly; no real source discovery.
    stamp=datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
    with Session(app.state.engine) as session:
        session.add(Article(article_key='local-report',provider='gkg',url=LOCAL_URL,canonical_url=LOCAL_URL,
            title=LOCAL_TITLE,content=LOCAL_TEXT,language='fr',source_domain=HOST))
        session.add(ArticleIdentity(identity_key=identity_key({'url':LOCAL_URL}),article_key='local-report'))
        for key,source in [('101',LOCAL_URL),('102',LOCAL_URL),('103',''),('104',REMOTE_URL),
                           ('105',REMOTE_URL+'?utm_source=other'),('106',BLOCKED_URL)]:
            session.add(OfficialEvent(event_id=key,event_date='2026-11-14',first_seen=stamp,last_seen=stamp,
                actor1_name='ZUNI',actor2_name='MOROCCO',event_code='190',event_base_code='190',event_root_code='19',
                action_country_iso2='ma',action_place='Morocco',source_url=source,
                feed='events_english',stamp='20261114000000',row_index=int(key)))
        session.add(OfficialMention(key='report-mention',event_id='103',doc_identifier=LOCAL_URL,domain=HOST,
            first_seen=stamp,last_seen=stamp,confidence=90,first_feed='mentions_english',first_stamp='20261114000000',
            first_row_index=1,last_feed='mentions_english',last_stamp='20261114000000',last_row_index=1))
        session.commit()
    try:yield app,client
    finally:client.close();app.state.engine.dispose()


def count(session,model):return session.exec(select(func.count()).select_from(model)).one()


def detail(client,key):
    response=client.get('/api/gdelt/events/'+key)
    assert response.status_code==200,response.text
    return response.json()['event']


def request_preview(client,*keys,retry=False):
    result=client.post('/api/gdelt/content/enrich',json=dict(event_ids=list(keys),retry=retry))
    assert result.status_code==202,result.text
    return result.json()


def run_until(app,predicate,limit=30):
    for _ in range(limit):
        if predicate():return
        app.state.source_content.step()
    assert predicate(),'Source preview did not reach the expected bounded fixture state'


def test_local_and_mention_reports_are_visible_on_first_read_without_side_effects(content_case,monkeypatch):
    app,client=content_case
    def forbidden(*args,**kwargs):raise AssertionError('A content GET opened the network or raw archive')
    from news2douyin.research import sources
    from news2douyin.selection import source_fetch
    from news2douyin.intake.lake import Lake
    monkeypatch.setattr(sources,'safe_fetch',forbidden)
    monkeypatch.setattr(source_fetch,'safe_fetch',forbidden)
    monkeypatch.setattr(Lake,'__init__',forbidden)
    executions=[]
    def audit(_c,_cursor,statement,_parameters,context,_many):executions.append((statement,is_read_only_execution(statement,context)))
    sql_event.listen(app.state.engine,'before_cursor_execute',audit)
    try:
        page=client.get('/radar?source=gdelt&period=all');assert page.status_code==200,page.text
        headings=[html.unescape(value) for value in re.findall(r'<h2[^>]*>(.*?)</h2>',page.text,re.S)]
        assert any(LOCAL_TITLE in heading for heading in headings),'Real report headline was hidden in JSON instead of displayed'
        assert LOCAL_TEXT in page.text
        for key in ('101','102','103'):
            data=detail(client,key)
            assert data['display_title']==LOCAL_TITLE
            assert data['report_preview']['title']==LOCAL_TITLE and data['excerpt']==LOCAL_TEXT
            assert data['source_language']['code']=='fr'
            assert data['coded_title']=='ZUNI · 交战 · MOROCCO'
            assert '多个事件' in data['content_note'] and '核实' in data['content_note']
        assert detail(client,'103')['report_preview']['relation']=='mention'
        assert detail(client,'104')['display_title']=='报道内容待补充'
        start=len(executions)
        cards=client.get('/api/gdelt/content/cards?event_ids=101,103,104')
        assert cards.status_code==200,cards.text
        state=client.get('/api/gdelt/content/status');assert state.status_code==200,state.text
        assert not any('count(' in sql.lower() for sql,_ in executions[start:])
        assert all(read for _,read in executions),executions
        assert not any('articleversion' in sql.lower() or 'raw_json' in sql.lower() for sql,_ in executions)
    finally:sql_event.remove(app.state.engine,'before_cursor_execute',audit)
    from news2douyin.selection.source_models import SourcePreviewJob
    with Session(app.state.engine) as session:
        assert count(session,SourcePreviewJob)==0
        assert count(session,Article)==1 and count(session,Event)==0


def test_http_enrichment_canonical_aliases_share_one_job_and_source_title(content_case,monkeypatch):
    app,client=content_case
    from news2douyin.selection.source_models import SourcePreview,SourcePreviewJob
    with public_http_transport(monkeypatch,app.state.engine) as upstream:
        assert detail(client,'104')['report_preview'] is None
        made=request_preview(client,'104','105')
        assert made['queued']==1,made
        with Session(app.state.engine) as session:assert count(session,SourcePreviewJob)==1
        run_until(app,lambda:detail(client,'104')['report_preview'] is not None)
        for key in ('104','105'):
            card=detail(client,key)
            assert card['display_title']==REMOTE_TITLE and card['excerpt']==REMOTE_EXCERPT
            assert card['source_language']['code']=='en' and card['content_status']=='ready'
            assert card['report_preview']['requires_original_text'] is True
            assert card['coded_title']!=card['display_title']
        before=upstream['calls'].count('/remote')
        request_preview(client,'104','105')
        app.state.source_content.step()
        assert upstream['calls'].count('/remote')==before==1
    with Session(app.state.engine) as session:
        row=session.get(SourcePreview,identity_key({'url':REMOTE_URL}))
        assert row.title==REMOTE_TITLE and row.excerpt==REMOTE_EXCERPT and row.content_hash
        assert count(session,Article)==1 and count(session,Event)==0


def test_failed_source_is_a_visible_gap_and_explicit_retry_recovers_without_inventing_content(content_case,monkeypatch):
    app,client=content_case
    from news2douyin.selection.source_models import SourcePreviewJob
    key=identity_key({'url':BLOCKED_URL})
    with public_http_transport(monkeypatch,app.state.engine) as upstream:
        request_preview(client,'106')
        def failed():
            with Session(app.state.engine) as session:
                row=session.get(SourcePreviewJob,key)
                return bool(row and row.state in ('failed','unavailable'))
        run_until(app,failed)
        gap=detail(client,'106')
        assert gap['display_title']=='报道内容待补充' and gap['report_preview'] is None
        assert gap['content_status'] in ('failed','unavailable')
        assert 'Access Denied' not in json.dumps(gap,ensure_ascii=False)
        upstream['responses']['/blocked']=(200,'text/html; charset=utf-8',REMOTE_HTML)
        request_preview(client,'106',retry=True)
        run_until(app,lambda:detail(client,'106')['report_preview'] is not None)
        assert detail(client,'106')['display_title']==REMOTE_TITLE
    with Session(app.state.engine) as session:assert count(session,Event)==0


class RecordingRuntime:
    def __init__(self):self.calls=[]
    def enqueue(self,case_id,**kwargs):self.calls.append((case_id,kwargs))


def test_selection_freezes_real_preview_but_keeps_it_a_research_lead(content_case,monkeypatch):
    app,client=content_case
    from news2douyin.selection.source_models import SourcePreview
    from news2douyin.topics import service as topics
    with public_http_transport(monkeypatch,app.state.engine):
        request_preview(client,'104');run_until(app,lambda:detail(client,'104')['report_preview'] is not None)
    payload=dict(request_id='source-content-selection-fixture',angle='先核对原文与历史背景')
    result=client.post('/api/gdelt/events/104/select',json=payload)
    assert result.status_code==200,result.text
    made=result.json();assert client.post('/api/gdelt/events/104/select',json=payload).json()==made
    assert client.get(made['url']).status_code==200
    state=client.get('/api/topics/'+made['key']).json()
    assert state['title']==REMOTE_TITLE
    assert state['materials']['can_write_from_sources'] is False
    with Session(app.state.engine) as session:
        sources=session.exec(select(TopicSource).where(TopicSource.workspace_id==made['key'])).all()
        frozen={r.source_id:(r.document_json,r.content_hash) for r in sources}
        docs=[json.loads(r.document_json) for r in sources]
        report=next(d for d in docs if d['source_kind']=='search_snippet')
        assert report['title']==REMOTE_TITLE and report['content']==REMOTE_EXCERPT
        assert report['requires_original_text'] is True and report['source_preview']['content_hash']
        row=session.get(SourcePreview,identity_key({'url':REMOTE_URL}));row.title='Later source headline';row.excerpt='Later source description';session.add(row);session.commit()
    runtime=RecordingRuntime()
    with pytest.raises(ValueError,match='先补材或研究'):
        topics.handoff(app.state.engine,runtime,made['key'],dict(revision=state['revision'],request_id='blocked-source-only-draft',action='short'))
    assert runtime.calls==[]
    with Session(app.state.engine) as session:
        sources=session.exec(select(TopicSource).where(TopicSource.workspace_id==made['key'])).all()
        assert {r.source_id:(r.document_json,r.content_hash) for r in sources}==frozen
        assert count(session,Event)==0


def test_source_preview_translation_uses_the_saved_report_text_and_freezes_input(content_case,monkeypatch):
    app,client=content_case
    from news2douyin.selection.source_models import SourcePreview
    with public_http_transport(monkeypatch,app.state.engine):
        request_preview(client,'104');run_until(app,lambda:detail(client,'104')['report_preview'] is not None)
    calls=[]
    monkeypatch.setattr(translations,'translate',lambda original,target:(calls.append(original) or '港口工人宣布举行一天罢工。'))
    key=identity_key({'url':REMOTE_URL})
    queued=client.post('/api/translations',json=dict(kind='source_preview',key=key,scope='document'))
    assert queued.status_code==200,queued.text
    data=queued.json()
    assert data['original']==REMOTE_TITLE+'\n\n'+REMOTE_EXCERPT
    assert not calls and 'ZUNI' not in data['original'] and data['source_language']=='en'
    with Session(app.state.engine) as session:
        row=session.get(SourcePreview,key);row.title='Changed after queue';session.add(row);session.commit()
    worker=TaskWorker(app.state.engine,str(app.state.storage_root));task=worker.service.claim();assert task
    worker.execute(task)
    done=client.get('/api/translations/'+data['id']).json()
    assert done['status']=='succeeded' and calls==[REMOTE_TITLE+'\n\n'+REMOTE_EXCERPT]
    again=client.post('/api/translations',json=dict(kind='source_preview',key=key,scope='document')).json()
    assert again['id']!=data['id'] and again['original'].startswith('Changed after queue')


def test_content_mutations_are_same_origin_and_cannot_request_arbitrary_urls(content_case):
    app,client=content_case
    path='/api/gdelt/content/enrich'
    assert client.get(path).status_code==405
    assert client.post(path,json=dict(event_ids=['104']),headers={'Origin':'https://foreign.example'}).status_code==403
    assert client.post(path,json=dict(event_ids=['104'],url='http://127.0.0.1/secrets')).status_code==422
    assert client.post(path,json=dict(event_ids=['1']*31)).status_code==422
    assert client.post(path,json=dict(event_ids=['not-an-id'])).status_code==422


def test_page_content_resolution_queries_stay_bounded_and_use_identity_hashes(content_case):
    app,_=content_case
    from news2douyin.selection import service
    now=datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
    with Session(app.state.engine) as session:
        for key in range(200,240):
            session.add(OfficialEvent(event_id=str(key),event_date='2026-11-14',last_seen=now,
                actor1_name='QA ACTOR',event_root_code='04',source_url=LOCAL_URL,
                feed='events_english',stamp='20261114000000',row_index=key))
        session.commit()
    def read(limit):
        rows=[]
        def audit(_c,_cursor,statement,*args):rows.append(statement.lower())
        sql_event.listen(app.state.engine,'before_cursor_execute',audit)
        try:
            with Session(app.state.engine) as session:
                data=service.list_candidates(session,service.checked(period='all'),limit=limit)
            assert len(data['items'])==limit
            return rows
        finally:sql_event.remove(app.state.engine,'before_cursor_execute',audit)
    one,many=read(1),read(30)
    assert len(many)<=len(one)+1 and len(many)<=16,(len(one),len(many),many)
    assert all(sql.lstrip().startswith('select') for sql in many)
    assert any('articleidentity' in sql and ' in (' in sql for sql in many)
    assert not any('raw_json' in sql or 'articleversion' in sql for sql in many)
    assert not any('article.canonical_url =' in sql or 'article.url =' in sql for sql in many)


def test_late_local_article_wins_over_an_inflight_remote_preview(content_case,monkeypatch):
    app,client=content_case
    from news2douyin.selection import source_fetch
    original=source_fetch.fetch_preview
    late_title='Local corrected source headline'
    late_text='The newsroom corrected this report after its initial source metadata appeared.'
    def fetch_then_save_local(url):
        value=original(url)
        if url.split('?',1)[0]==REMOTE_URL:
            with Session(app.state.engine) as session:
                session.add(Article(article_key='late-local',provider='industry.monitor',url=REMOTE_URL,
                    canonical_url=REMOTE_URL,title=late_title,content=late_text,language='en',source_domain=HOST))
                session.add(ArticleIdentity(identity_key=identity_key({'url':REMOTE_URL}),article_key='late-local'))
                session.commit()
        return value
    monkeypatch.setattr(source_fetch,'fetch_preview',fetch_then_save_local)
    with public_http_transport(monkeypatch,app.state.engine):
        request_preview(client,'104')
        run_until(app,lambda:detail(client,'104')['display_title']==late_title)
    card=detail(client,'104')
    assert card['excerpt']==late_text and card['report_preview']['origin']=='local_article'
    with Session(app.state.engine) as session:
        article=session.exec(select(Article).where(Article.article_key=='late-local')).one()
        assert article.title==late_title and article.content==late_text
        assert count(session,Event)==0
