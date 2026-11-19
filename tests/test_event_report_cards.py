"""Real source text improves event discovery without asserting coded facts."""
from datetime import datetime,timezone
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import event as sql_event,func
from sqlmodel import Session,select

from news2douyin.intake.official_models import OfficialEvent,OfficialMention
from news2douyin.selection import service
from news2douyin.selection.models import SelectionOrigin
from news2douyin.storage.articles import identity_key
from news2douyin.storage.db import init_db,make_engine
from news2douyin.storage.models import Article,ArticleIdentity,Event
from news2douyin.topics import service as topics
from news2douyin.topics.models import TopicSource,TopicWorkspace
from test_postgres_integration import postgres_url


def now():return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')


@pytest.fixture(params=['sqlite','postgres'])
def reports_case(request,tmp_path):
    url=request.getfixturevalue('postgres_url') if request.param=='postgres' else 'sqlite:///'+str(tmp_path/'reports.db')
    engine=make_engine(url);init_db(engine)
    with Session(engine) as s:
        for ident,url in [('1001','https://wire.example/first'),('1002','https://wire.example/second')]:
            s.add(OfficialEvent(event_id=ident,event_date='2026-11-14',first_seen=now(),last_seen=now(),
                actor1_name='ZUNI',actor2_name='MOROCCO',event_code='190',event_base_code='190',event_root_code='19',
                source_url=url,feed='events_english',stamp='20261114090000',row_index=int(ident)))
        s.commit()
    try:yield engine
    finally:engine.dispose()


def article(engine,url,title='合成报道：当地举行公共交通听证会',body='这是来源中已有的报道正文，仅截取作预览，并未核实GDELT自动编码。',key='article-1'):
    with Session(engine) as s:
        s.add(Article(article_key=key,provider='industry.fixture',url=url,canonical_url=url,
            title=title,content=body,language='zh-CN',source_domain='wire.example',published_at=now()))
        s.add(ArticleIdentity(identity_key=identity_key(dict(url=url)),article_key=key));s.commit()


def mention(engine,key,event_id,url,confidence=80):
    with Session(engine) as s:
        s.add(OfficialMention(key=key,event_id=event_id,doc_identifier=url,domain='wire.example',
            confidence=confidence,first_seen=now(),last_seen=now(),first_feed='mentions_english',
            first_stamp='20261114090000',first_row_index=1,last_feed='mentions_english',
            last_stamp='20261114090000',last_row_index=1));s.commit()


def test_missing_report_is_explicit_and_never_coding_as_the_visible_headline(reports_case):
    with Session(reports_case) as s:
        result=service.list_candidates(s,service.checked(period='all'))
        event=result['items'][0]
        assert event['display_title']=='报道内容待补充'
        assert event['report_preview'] is None and not event['excerpt']
        assert event['coded_title']=='ZUNI · 交战 · MOROCCO'
        assert event['content_status']=='missing'
        assert s.exec(select(func.count()).select_from(Event)).one()==0


def test_existing_source_headline_and_excerpt_are_card_content_but_still_leads(reports_case):
    article(reports_case,'https://wire.example/first')
    with Session(reports_case) as s:
        event=service.detail(s,'1001')['event'];preview=event['report_preview']
        assert event['display_title']=='合成报道：当地举行公共交通听证会'
        assert event['title']==event['display_title']
        assert '已有的报道正文' in event['excerpt']
        assert event['coded_title']=='ZUNI · 交战 · MOROCCO'
        assert preview['origin']=='local_article' and preview['relation']=='source_url'
        assert preview['requires_original_text'] and preview['source_kind']=='search_snippet'
        assert '多个事件' in preview['relation_note']
        assert event['source_language']['code']=='zh-cn'
        assert s.exec(select(func.count()).select_from(Event)).one()==0


def test_same_document_for_two_events_is_shared_report_not_verified_event_summary(reports_case):
    url='https://wire.example/shared-report'
    article(reports_case,url,key='shared')
    mention(reports_case,'m1','1001',url)
    mention(reports_case,'m2','1002',url)
    with Session(reports_case) as s:
        items=service.list_candidates(s,service.checked(period='all'))['items']
        assert len(items)==2 and len({i['report_preview']['key'] for i in items})==1
        assert all(i['report_preview']['relation']=='mention' for i in items)
        assert all('自动编码已核实' in i['content_note'] for i in items)
        assert all(i['report_preview']['requires_original_text'] for i in items)
        detail=service.detail(s,'1001')
        assert detail['mentions'][0]['preview']['title']==items[0]['display_title']
        assert detail['mentions'][0]['preview']['requires_original_text'] is True
        assert detail['mentions'][0]['preview']['source_kind']=='search_snippet'


def test_card_limits_mentions_per_event_but_detail_preserves_additional_report_context(reports_case):
    for n in range(1,9):mention(reports_case,'mention-'+str(n),'1001','https://wire.example/m'+str(n),100-n)
    article(reports_case,'https://wire.example/m4',title='第四条提及有标题，但不在卡片候选上限内',key='fourth')
    with Session(reports_case) as s:
        event=service.content_cards(s,['1001'])['items'][0]
        assert event['report_preview'] is None
        assert event['content_scope']['mentions_per_event']==3
        assert len(event['reports'])==4
        detail=service.detail(s,'1001')
        assert detail['mentions'][3]['preview']['title']=='第四条提及有标题，但不在卡片候选上限内'


def test_content_refresh_is_bounded_readonly_and_preserves_requested_order(reports_case,monkeypatch):
    from news2douyin.selection import source_fetch
    def forbidden(*args,**kwargs):raise AssertionError('GET-like content read performed HTTP')
    monkeypatch.setattr(source_fetch,'fetch_preview',forbidden)
    article(reports_case,'https://wire.example/first')
    statements=[]
    def record(_conn,_cursor,statement,*_):statements.append(statement.lower())
    sql_event.listen(reports_case,'before_cursor_execute',record)
    try:
        with Session(reports_case) as s:
            data=service.content_cards(s,['1002','1001','1002','9999'])
    finally:sql_event.remove(reports_case,'before_cursor_execute',record)
    assert [i['event_id'] for i in data['items']]==['1002','1001']
    assert data['missing']==['9999']
    assert statements and all(sql.lstrip().startswith('select') for sql in statements)
    assert not any('count(' in sql.replace(' ','') for sql in statements)
    assert not any('articleversion' in sql or 'raw_json' in sql for sql in statements)
    source_queries=[sql for sql in statements if 'from officialmention' in sql]
    assert len(source_queries)==1 and 'limit' in source_queries[0]
    assert 'row_number()' not in source_queries[0]
    assert 'order by officialmention.last_seen desc' in source_queries[0]
    assert 'order by officialmention.confidence' not in source_queries[0]
    assert not any('canonical_url =' in sql or 'article.url =' in sql for sql in statements)
    # The optional reconstructed context adds one bounded batch lookup across
    # all visible event IDs. Keep the existing six-query publisher-preview budget.
    ngram_queries=[sql for sql in statements if 'from ngramtarget' in sql]
    assert len(ngram_queries)<=1
    assert all('limit' in sql and 'ngramtarget.event_id = officialevent.event_id' in sql for sql in ngram_queries)
    assert len(statements)-len(ngram_queries)<=6,statements
    with Session(reports_case) as s:
        assert service.content_cards(s,[])['items']==[]
        with pytest.raises(ValueError):service.content_cards(s,['1']*31)
        with pytest.raises(ValueError):service.content_cards(s,['not/an/id'])
        with pytest.raises(ValueError):service.content_cards(s,[1])


def test_selected_report_previews_freeze_and_cannot_bypass_original_text_guard(reports_case):
    article(reports_case,'https://wire.example/first')
    made=service.select_candidate(reports_case,'1001','reported-event-selection')
    with Session(reports_case) as s:
        workspace=s.get(TopicWorkspace,made['key'])
        assert workspace.title=='合成报道：当地举行公共交通听证会'
        sources=s.exec(select(TopicSource).where(TopicSource.workspace_id==workspace.workspace_id)).all()
        assert len(sources)==2
        docs=[json.loads(row.document_json) for row in sources]
        assert {d['source_kind'] for d in docs}=={'event_metadata','search_snippet'}
        assert all(d['requires_original_text'] for d in docs)
        frozen={row.source_id:row.document_json for row in sources}
        snap=json.loads(s.get(SelectionOrigin,workspace.workspace_id).snapshot_json)
        assert snap['event']['report_preview']['content_hash']
        row=s.exec(select(Article)).one();row.title='后来修订的来源标题';s.add(row);s.commit()
        revision=workspace.revision
    assert service.select_candidate(reports_case,'1001','reported-event-selection')==made
    runtime=SimpleNamespace(enqueue=lambda *args,**kwargs: (_ for _ in ()).throw(AssertionError('Should reject before enqueue')))
    with pytest.raises(ValueError,match='先补材或研究'):
        topics.handoff(reports_case,runtime,made['key'],dict(revision=revision,
            request_id='reported-event-short-guard',action='short'))
    with Session(reports_case) as s:
        current=s.exec(select(TopicSource).where(TopicSource.workspace_id==made['key'])).all()
        assert {row.source_id:row.document_json for row in current}==frozen
        assert service.detail(s,'1001')['event']['display_title']=='后来修订的来源标题'


def test_detail_confidence_order_is_only_within_31_recent_indexed_candidates(reports_case):
    from datetime import timedelta
    clock=datetime.now(timezone.utc)
    def item(index,old=False):
        key=('old-' if old else 'recent-')+str(index)
        at=clock-timedelta(days=1,seconds=index) if old else clock-timedelta(seconds=index)
        return dict(key=key,event_id='1001',doc_identifier='https://wire.example/'+key,
            domain='wire.example',confidence=100 if old else index,last_seen=at.isoformat(),
            first_feed='mentions_english',first_stamp='20261114090000',first_row_index=index,
            last_feed='mentions_english',last_stamp='20261114090000',last_row_index=index)
    with reports_case.begin() as connection:
        connection.execute(OfficialMention.__table__.insert(),[item(n,True) for n in range(1000)]
            +[item(n) for n in range(35)])
    reads=[]
    def audit(_conn,_cursor,statement,parameters,context,_many):
        if context.execution_options.get('n2d_query_name')=='selection.mentions.detail':
            reads.append((statement,parameters))
    sql_event.listen(reports_case,'before_cursor_execute',audit)
    try:
        with Session(reports_case) as s:result=service.detail(s,'1001')
    finally:sql_event.remove(reports_case,'before_cursor_execute',audit)
    assert len(result['mentions'])==30 and result['mentions_limited'] is True
    assert all('/recent-' in row['doc_identifier'] for row in result['mentions'])
    assert result['mentions'][0]['doc_identifier'].endswith('/recent-30')
    assert result['mentions_scope']['recent_candidate_limit']==31
    assert result['mentions_scope']['ranking_scope']=='recent_candidates_not_full_history'
    assert len(reads)==1 and 'limit' in reads[0][0].lower()
    assert 'order by officialmention.confidence' not in reads[0][0].lower()
    with reports_case.connect() as connection:
        assert len(connection.exec_driver_sql(*reads[0]).all())==31
        if reports_case.dialect.name=='sqlite':
            plan=connection.exec_driver_sql('EXPLAIN QUERY PLAN '+reads[0][0],reads[0][1]).all()
            text=' '.join(str(row[-1]) for row in plan)
            assert 'ix_officialmention_event_recent' in text and 'TEMP B-TREE' not in text,text
