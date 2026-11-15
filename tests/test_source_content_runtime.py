"""Original-source title lookup and bounded durable enrichment contracts."""
from datetime import datetime,timedelta,timezone
import json
from threading import Barrier
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event as sql_event,func
from sqlalchemy.exc import OperationalError
from sqlmodel import Session,select

from news2douyin.selection.source_content import resolve_previews,preview_hash
from news2douyin.selection.source_models import SourcePreview,SourcePreviewJob
from news2douyin.selection.source_runtime import SourceContentWorker,enqueue_events,status
from news2douyin.storage.articles import identity_key
from news2douyin.storage.db import make_engine,init_db
from news2douyin.storage.models import Article,ArticleIdentity
from news2douyin.intake.official_models import OfficialEvent,OfficialMention
from test_postgres_integration import postgres_url

URL='https://media.example/article'


@pytest.fixture(params=['sqlite','postgres'])
def source_case(request,tmp_path,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_SOURCE_CONTENT_ENABLED','1')
    url=request.getfixturevalue('postgres_url') if request.param=='postgres' else 'sqlite:///'+str(tmp_path/'source.db')
    engine=make_engine(url);init_db(engine)
    try:yield engine,tmp_path/'runs'
    finally:engine.dispose()


def article(s,title='President announces new tariff plan',content='A source-supplied report excerpt.',url=URL):
    row=Article(article_key='local-'+identity_key({'url':url})[:12],provider='gkg',url=url,title=title,content=content,language='en')
    s.add(row);s.add(ArticleIdentity(identity_key=identity_key({'url':url}),article_key=row.article_key));return row


def official(s,key='111',url=URL):
    now=datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
    s.add(OfficialEvent(event_id=key,event_date='2026-11-14',last_seen=now,source_url=url,
        feed='events_english',stamp='20261114120000',row_index=1));return key


def preview(title='President announces new tariff plan',excerpt='A source-supplied report excerpt.'):
    return dict(title=title,excerpt=excerpt,language='en',final_url=URL,domain='media.example',
        title_origin='og:title',excerpt_origin='og:description')


def test_local_lookup_uses_identity_hashes_and_bounded_text_without_network(source_case,monkeypatch):
    engine,_=source_case
    with Session(engine) as s:row=article(s,content='A'*30000);s.commit()
    statements=[]
    def audit(_c,_cur,sql,*_):statements.append(sql.lower())
    sql_event.listen(engine,'before_cursor_execute',audit)
    try:
        with Session(engine) as s:value=resolve_previews(s,[URL,URL+'?utm_source=tracked'])
    finally:sql_event.remove(engine,'before_cursor_execute',audit)
    assert value[URL]['title']=='President announces new tariff plan'
    assert len(value[URL]['excerpt'])==1200 and value[URL]['origin']=='local_article'
    assert value[URL]['translation']['kind']=='source_preview'
    assert value[URL]['key']==value[URL+'?utm_source=tracked']['key']
    assert not any('canonical_url =' in q or 'raw_json' in q or 'articleversion' in q for q in statements)
    assert any('substr' in q and 'article.content' in q for q in statements)
    assert all(q.lstrip().startswith('select') for q in statements)


def test_canonical_aliases_enqueue_once_and_share_preview(source_case,monkeypatch):
    engine,root=source_case
    with Session(engine) as s:official(s);official(s,'222',URL+'?utm_source=x');s.commit()
    result=enqueue_events(engine,['111','222'])
    assert result['queued']==1
    from news2douyin.selection import source_fetch
    calls=[]
    monkeypatch.setattr(source_fetch,'fetch_preview',lambda url:(calls.append(url) or preview()))
    worker=SourceContentWorker(engine,root);worker._discover_at=float('inf')
    assert worker.step() and not worker.step()
    with Session(engine) as s:
        assert len(s.exec(select(SourcePreviewJob)).all())==1
        values=resolve_previews(s,[URL,URL+'?utm_source=x'])
        assert all(v['ready'] and v['excerpt'] for v in values.values())
    assert calls==[URL]


def test_remote_fetch_has_no_sql_connection_and_local_late_title_wins(source_case,monkeypatch):
    engine,root=source_case
    with Session(engine) as s:official(s);s.commit()
    enqueue_events(engine,['111'])
    from news2douyin.selection import source_fetch
    def fetch(url):
        assert engine.pool.checkedout()==0
        with Session(engine) as s:article(s,title='Local report denies the tariff plan',content='New local source text.');s.commit()
        return preview()
    monkeypatch.setattr(source_fetch,'fetch_preview',fetch)
    worker=SourceContentWorker(engine,root);worker._discover_at=float('inf');worker.step()
    with Session(engine) as s:value=resolve_previews(s,[URL])[URL]
    assert value['title']=='Local report denies the tariff plan' and value['excerpt']=='New local source text.'
    assert value['translation']['kind']=='source_preview'


def test_title_only_gets_one_supplement_and_no_old_excerpt_after_title_change(source_case,monkeypatch):
    engine,root=source_case
    with Session(engine) as s:official(s);article(s,content='');s.commit()
    with Session(engine) as s:assert resolve_previews(s,[URL])[URL]['status']=='title_only'
    assert enqueue_events(engine,['111'])['queued']==1
    from news2douyin.selection import source_fetch
    calls=[];monkeypatch.setattr(source_fetch,'fetch_preview',lambda url:(calls.append(url) or preview()))
    worker=SourceContentWorker(engine,root);worker._discover_at=float('inf');worker.step()
    with Session(engine) as s:
        value=resolve_previews(s,[URL])[URL]
        assert value['title']=='President announces new tariff plan' and value['excerpt']
        assert value['mixed_origin'] and value['translation']['kind']=='source_preview'
        row=s.exec(select(Article)).one();row.title='President denies new tariff plan';s.add(row);s.commit()
    with Session(engine) as s:
        value=resolve_previews(s,[URL])[URL]
        assert value['status']=='title_only' and value['excerpt']==''
    assert enqueue_events(engine,['111'])['queued']==1
    assert calls==[URL]


def test_failed_fetch_is_durable_bounded_and_explicit_retry_does_not_advance_events(source_case,monkeypatch):
    engine,root=source_case
    with Session(engine) as s:official(s);s.commit()
    enqueue_events(engine,['111'])
    from news2douyin.selection import source_fetch
    calls=[]
    def blocked(url):calls.append(url);raise source_fetch.PreviewFetchError('blocked_page','来源暂不可读')
    monkeypatch.setattr(source_fetch,'fetch_preview',blocked)
    worker=SourceContentWorker(engine,root);worker._discover_at=float('inf')
    for index in range(3):
        assert worker.step()
        with Session(engine) as s:
            row=s.get(SourcePreviewJob,identity_key({'url':URL}));row.retry_at=0;s.add(row);s.commit()
    assert not worker.step() and len(calls)==3
    with Session(engine) as s:
        row=s.get(SourcePreviewJob,identity_key({'url':URL}));assert row.state=='unavailable'
        assert s.get(OfficialEvent,'111').source_url==URL
    assert enqueue_events(engine,['111'],retry=True)['queued']==1


def test_expired_lease_and_stale_publisher_are_fenced(source_case):
    engine,root=source_case
    with Session(engine) as s:official(s);s.commit()
    enqueue_events(engine,['111'])
    first=SourceContentWorker(engine,root);second=SourceContentWorker(engine,root)
    job=first._claim();assert second._claim() is None
    with Session(engine) as s:
        row=s.get(SourcePreviewJob,job['key']);row.lease_until=0;s.add(row);s.commit()
    current=second._claim();assert current['token']!=job['token']
    assert first._publish(job,preview()) is False
    assert second._publish(current,preview())
    with Session(engine) as s:assert s.get(SourcePreviewJob,job['key']).state=='ready'


def test_only_official_urls_can_be_queued_and_status_is_count_free(source_case):
    engine,_=source_case
    with pytest.raises(ValueError):enqueue_events(engine,['https://injected.example'])
    assert enqueue_events(engine,['123'])['missing']==['123']
    statements=[]
    def audit(_c,_cur,sql,*_):statements.append(sql.lower())
    sql_event.listen(engine,'before_cursor_execute',audit)
    try:state=status(engine)
    finally:sql_event.remove(engine,'before_cursor_execute',audit)
    assert state['pending']==0
    assert all(q.lstrip().startswith('select') and 'count(' not in q for q in statements)


def test_concurrent_enqueue_is_idempotent_and_local_title_failure_is_not_retried(source_case,monkeypatch):
    engine,root=source_case
    with Session(engine) as s:official(s);article(s,content='');s.commit()
    barrier=Barrier(2)
    def submit():barrier.wait();return enqueue_events(engine,['111'])
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:submit(),range(2)))
    assert sum(row['queued'] for row in results)==1
    from news2douyin.selection import source_fetch
    calls=[]
    def failed(url):calls.append(url);raise source_fetch.PreviewFetchError('blocked_page','报道摘要尚不可读')
    monkeypatch.setattr(source_fetch,'fetch_preview',failed)
    worker=SourceContentWorker(engine,root);worker._discover_at=float('inf');assert worker.step()
    assert enqueue_events(engine,['111'])['queued']==0 and not worker.step()
    with Session(engine) as s:
        value=resolve_previews(s,[URL])[URL]
        assert value['title'] and value['status']=='title_only' and value['fetch_status']=='unavailable'
        row=s.get(SourcePreviewJob,value['key']);assert row.attempts==1
    assert calls==[URL]


def test_crashed_third_attempt_becomes_explicit_unavailable_without_fourth_claim(source_case):
    engine,root=source_case
    with Session(engine) as s:
        s.add(SourcePreviewJob(key=identity_key({'url':URL}),source_url=URL,state='running',attempts=3,
            lease='abandoned',lease_until=0));s.commit()
    worker=SourceContentWorker(engine,root)
    assert worker._claim() is None
    with Session(engine) as s:
        row=s.get(SourcePreviewJob,identity_key({'url':URL}))
        assert row.state=='unavailable' and row.error_kind=='interrupted' and row.attempts==3


def test_hot_same_timestamp_mentions_use_index_limit_without_tie_sort(source_case):
    """The raw work is bounded, rather than merely truncating returned rows."""
    from news2douyin.selection.source_runtime import _event_urls
    engine,_=source_case
    with Session(engine) as s:
        official(s,url='')
        s.execute(OfficialMention.__table__.insert(),[
            dict(key='hot-'+str(n),event_id='111',doc_identifier=f'https://media.example/hot/{n}',
                last_seen='2026-11-14T12:00:00Z',first_feed='mentions_english',first_stamp='20261114120000',
                first_row_index=n+1,last_feed='mentions_english',last_stamp='20261114120000',last_row_index=n+1)
            for n in range(3000)])
        s.commit()
    with engine.begin() as c:c.exec_driver_sql('ANALYZE officialmention')
    statements=[]
    def audit(_c,_cur,sql,parameters,*_):
        if sql.lstrip().upper().startswith('SELECT') and 'officialmention' in sql.lower():statements.append((sql,parameters))
    sql_event.listen(engine,'before_cursor_execute',audit)
    try:
        with Session(engine) as s:urls,found=_event_urls(s,['111'])
    finally:sql_event.remove(engine,'before_cursor_execute',audit)
    assert found=={'111'} and len(urls)==2 and len(statements)==1
    sql,parameters=statements[0]
    with engine.connect() as c:
        if engine.dialect.name=='sqlite':
            plan=c.exec_driver_sql('EXPLAIN QUERY PLAN '+sql,parameters).all()
            lines=[str(row[-1]).upper() for row in plan]
            assert not any('TEMP B-TREE' in line for line in lines),lines
            assert any('IX_OFFICIALMENTION_EVENT_RECENT' in line for line in lines),lines
        else:
            plan=c.exec_driver_sql('EXPLAIN (ANALYZE,FORMAT JSON) '+sql,parameters).scalar_one()[0]['Plan']
            def nodes(row):
                yield row
                for child in row.get('Plans',[]):yield from nodes(child)
            steps=list(nodes(plan))
            assert not any('Sort' in row['Node Type'] for row in steps),steps
            scans=[row for row in steps if row.get('Relation Name')=='officialmention'
                and row['Node Type'] in ('Index Scan','Index Only Scan')]
            assert scans and all(row['Actual Rows']<=2 and row.get('Rows Removed by Filter',0)==0
                for row in scans),steps
