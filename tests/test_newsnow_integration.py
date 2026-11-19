"""Real local HTTP → durable collection → honest discovery material.

SQLite and opt-in PostgreSQL fixtures own their own disposable data. The
private NewsNow deployment is never contacted by these tests.
"""
from copy import deepcopy
from html.parser import HTMLParser
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlmodel import Session,select

from news2douyin.collect import service as collect
from news2douyin.server.app import create_app
from news2douyin.storage.models import Article,ArticleVersion,ArticleEventLink,Event,RunRecord
from news2douyin.tasks.control import TaskWorkerStopping
from newsnow_test_support import newsnow_http,snapshot
from test_postgres_integration import postgres_url


@pytest.fixture
def upstream():
    with newsnow_http() as fixture:yield fixture


@pytest.fixture(params=['sqlite','postgres'])
def app(request,tmp_path,monkeypatch):
    for flag in ('NEWS2DOUYIN_GKG_STREAM_ENABLED','NEWS2DOUYIN_GKG_PROJECTION_ENABLED',
                 'NEWS2DOUYIN_GDELT_STREAM_ENABLED','NEWS2DOUYIN_GDELT_PROJECTION_ENABLED'):
        monkeypatch.setenv(flag,'0')
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL','')
    url=request.getfixturevalue('postgres_url') if request.param=='postgres' else 'sqlite:///'+str(tmp_path/'app.db')
    value=create_app(db_url=url,storage_root=str(tmp_path/'runs'))
    try:yield value,TestClient(value),tmp_path/'runs'
    finally:value.state.engine.dispose()


def profile(client,upstream,*,name='NewsNow integration',sources=None,max_items=1200):
    response=client.post('/api/profiles',json=dict(name=name,provider='newsnow',
        categories=[],keywords_include=[],keywords_exclude=[],country='',language='',max_items=max_items,
        extra=dict(base_url=upstream.base,newsnow_sources=sources or ['zhihu','cls','fastbull','broken'],filter_mode='rules')))
    assert response.status_code==200,response.text
    return response.json()


def execute(app,client,request):
    assert request.status_code==202,request.text
    tid=request.json()['task_id']
    claimed=app.state.tasks.claim()
    assert claimed is not None and claimed.task_id==tid
    app.state.worker.execute(claimed)
    data=client.get('/api/tasks/'+tid+'/diagnostics')
    assert data.status_code==200,data.text
    return tid,data.json()


def count(session,model):return session.exec(select(func.count()).select_from(model)).one()


def archive_texts(storage):
    return [p.read_text(encoding='utf-8') for p in (storage/'newsnow'/'snapshots').rglob('*')
            if p.is_file() and p.suffix in ('.json','.body')]


def test_trial_then_collect_preserves_source_states_and_separates_snapshot_time(app,upstream):
    application,client,storage=app
    saved=profile(client,upstream)
    assert saved['extra']['newsnow_sources']==['zhihu','cls','fastbull','broken']
    assert saved['extra']['filter_mode']=='rules'
    _,trial=execute(application,client,client.post('/api/profiles/NewsNow%20integration/test',json={}))
    assert trial['task']['status']=='succeeded',trial
    assert trial['stats']['dry_run'] and trial['stats']['fetched']==3 and trial['stats']['after_filter']==3
    assert trial['stats']['filter_mode']=='rules'
    with Session(application.state.engine) as session:
        assert count(session,Article)==count(session,Event)==0
    snapshots=list(storage.rglob('newsnow-snapshots.json'))
    assert len(snapshots)==1
    first=json.loads(snapshots[0].read_text(encoding='utf-8'))
    assert first.get('snapshots'),'Manifest must contain retrievable raw source references.'
    assert any('3761 万热度' in body for body in archive_texts(storage))
    tid,done=execute(application,client,client.post('/api/tasks/collect',json={'profile_name':saved['name']}))
    assert done['task']['status']=='succeeded',done
    assert done['stats']['stored_articles']==3 and done['stats']['events_delta']==0
    with Session(application.state.engine) as session:
        rows=session.exec(select(Article).order_by(Article.url)).all()
        assert len(rows)==3 and all(row.provider=='newsnow' for row in rows)
        zhihu=next(row for row in rows if row.source_domain=='www.zhihu.com' and row.content)
        assert zhihu.published_at==''
        raw=json.loads(zhihu.raw_json)
        assert raw['material_kind']=='newsnow_discovery' and raw['requires_original_text'] is True
        assert '2026-11-14' in json.dumps(raw['newsnow'])
        cls=next(row for row in rows if row.source_domain=='www.cls.cn')
        assert cls.published_at=='2026-11-14T06:45:58Z' and cls.content==''
        assert count(session,Event)==0 and count(session,ArticleVersion)==3
        key=zhihu.article_key
    html=client.get('/articles/'+key)
    assert html.status_code==200 and 'NewsNow' in html.text
    assert '摘要' in html.text and '未取得原文' in html.text
    diagnostics=client.get('/tasks/'+tid+'/diagnostics')
    assert diagnostics.status_code==200 and 'broken' in diagnostics.text and 'fastbull' in diagnostics.text


def test_repeated_and_reordered_cached_lists_do_not_create_fake_article_versions(app,upstream):
    application,client,storage=app
    saved=profile(client,upstream,sources=['zhihu','cls'])
    for _ in range(2):
        _,done=execute(application,client,client.post('/api/tasks/collect',json={'profile_name':saved['name']}))
        assert done['task']['status']=='succeeded',done
    assert done['stats']['skipped_existing']==3 and done['stats']['updated_articles']==0
    current=deepcopy(upstream.responses['zhihu']);current['items'].reverse();upstream.set('zhihu',current)
    _,done=execute(application,client,client.post('/api/tasks/collect',json={'profile_name':saved['name']}))
    assert done['stats']['skipped_existing']==3 and done['stats']['updated_articles']==0
    with Session(application.state.engine) as session:
        assert count(session,Article)==count(session,ArticleVersion)==3
        assert count(session,Event)==0
    snapshots=list(storage.rglob('newsnow-snapshots.json'))
    assert len(snapshots)==3
    payloads=[value for value in archive_texts(storage) if 'question-1' in value and 'question-2' in value]
    assert any(value.index('question-2')<value.index('question-1') for value in payloads)
    assert any(value.index('question-1')<value.index('question-2') for value in payloads)


def test_all_failures_and_successful_empty_are_distinct(app,upstream):
    application,client,storage=app
    saved=profile(client,upstream,name='All failed',sources=['broken','html'])
    tid,failed=execute(application,client,client.post('/api/tasks/collect',json={'profile_name':saved['name']}))
    assert failed['task']['status']=='failed' and 'NewsNow' in failed['task']['error_text'],failed
    assert client.get('/tasks/'+tid+'/diagnostics').status_code==200
    snapshots=list(storage.rglob('newsnow-snapshots.json'))
    assert snapshots,'All-failed intake must preserve the per-source failure receipt.'
    saved=profile(client,upstream,name='Empty success',sources=['fastbull'])
    _,empty=execute(application,client,client.post('/api/tasks/collect',json={'profile_name':saved['name']}))
    assert empty['task']['status']=='succeeded' and empty['stats']['fetched']==0,empty
    assert empty['stats']['stored_articles']==0
    with Session(application.state.engine) as session:assert count(session,Article)==0


def test_capacity_does_not_skip_tail_source_and_raw_snapshots_keep_all_rows(app,upstream):
    application,client,storage=app
    saved=profile(client,upstream,sources=['zhihu','cls'],max_items=2)
    _,done=execute(application,client,client.post('/api/tasks/collect',json={'profile_name':saved['name']}))
    assert done['task']['status']=='succeeded' and done['stats']['stored_articles']==2,done
    assert upstream.calls==['zhihu','cls']
    with Session(application.state.engine) as session:
        domains={row.source_domain for row in session.exec(select(Article))}
    assert domains=={'www.zhihu.com','www.cls.cn'}
    body='\n'.join(archive_texts(storage))
    assert 'question-1' in body and 'question-2' in body and '2534448' in body


def test_worker_resume_reuses_frozen_batch_without_refetch_or_losing_receipts(app,upstream,monkeypatch):
    application,client,storage=app
    saved=profile(client,upstream,sources=['zhihu','cls'])
    original=collect.llm_filter_items
    attempts=[]
    def once(*args,**kwargs):
        attempts.append(True)
        if len(attempts)==1:raise TaskWorkerStopping('simulated worker restart after complete NewsNow response')
        return original(*args,**kwargs)
    monkeypatch.setattr(collect,'llm_filter_items',once)
    response=client.post('/api/tasks/collect',json={'profile_name':saved['name']})
    tid=response.json()['task_id'];claimed=application.state.tasks.claim();application.state.worker.execute(claimed)
    assert application.state.tasks.get(tid)['status']=='queued'
    assert upstream.calls==['zhihu','cls']
    resumed=application.state.tasks.claim();assert resumed.task_id==tid
    application.state.worker.execute(resumed)
    done=client.get('/api/tasks/'+tid+'/diagnostics').json()
    assert done['task']['status']=='succeeded' and done['stats']['stored_articles']==3,done
    assert upstream.calls==['zhihu','cls'],'A durable resume must use the saved batch, not a later live ranking.'
    assert len(list(storage.rglob('newsnow-snapshots.json')))>=1


def test_snapshot_only_stock_rows_are_archived_without_becoming_news(app,upstream):
    application,client,storage=app
    saved=profile(client,upstream,sources=['xueqiu'])
    _,done=execute(application,client,client.post('/api/tasks/collect',json={'profile_name':saved['name']}))
    assert done['task']['status']=='succeeded',done
    with Session(application.state.engine) as session:
        assert count(session,Article)==count(session,Event)==0
    assert any('NVDA' in text for text in archive_texts(storage))


def test_newsnow_excerpt_never_overwrites_existing_full_article(app,upstream,monkeypatch):
    application,client,storage=app
    url='https://www.zhihu.com/question/1001'
    monkeypatch.setitem(collect.PROVIDERS,'worldnewsapi',lambda _config:[dict(url=url,
        title='完整报道的标题',content='这是已经读取并保存的完整原文，不应被榜单摘要覆盖。',
        published_at='2026-11-13T06:00:00Z',source=dict(domain='www.zhihu.com'),language='zh')])
    response=client.post('/api/profiles',json=dict(name='Saved full article',provider='worldnewsapi',
        extra=dict(filter_mode='rules'),categories=[],keywords_include=[]))
    assert response.status_code==200
    _,full=execute(application,client,client.post('/api/tasks/collect',json={'profile_name':'Saved full article'}))
    assert full['task']['status']=='succeeded',full
    with Session(application.state.engine) as session:
        original=session.exec(select(Article)).one()
        before=(original.provider,original.title,original.content,original.published_at)
        before_versions=count(session,ArticleVersion)
    upstream.set('zhihu',snapshot('zhihu',[upstream.responses['zhihu']['items'][0]]))
    saved=profile(client,upstream,sources=['zhihu'])
    _,collected=execute(application,client,client.post('/api/tasks/collect',json={'profile_name':saved['name']}))
    assert collected['task']['status']=='succeeded',collected
    with Session(application.state.engine) as session:
        after=session.exec(select(Article)).one()
        assert (after.provider,after.title,after.content,after.published_at)==before
        assert count(session,ArticleVersion)==before_versions
    assert any('3761 万热度' in text for text in archive_texts(storage))


def test_restart_between_report_and_raw_checkpoint_can_refetch_consistently(app,upstream,monkeypatch):
    from news2douyin.tasks.service import TaskContext
    application,client,storage=app
    saved=profile(client,upstream,sources=['zhihu','cls'])
    original=TaskContext.save_checkpoint
    stopped=[]
    def interrupt(self,name,value,**kwargs):
        if name=='raw' and not stopped:
            stopped.append(True)
            raise TaskWorkerStopping('simulated stop between NewsNow receipt and frozen raw pointer')
        return original(self,name,value,**kwargs)
    monkeypatch.setattr(TaskContext,'save_checkpoint',interrupt)
    response=client.post('/api/tasks/collect',json={'profile_name':saved['name']})
    tid=response.json()['task_id']
    application.state.worker.execute(application.state.tasks.claim())
    assert application.state.tasks.get(tid)['status']=='queued'
    application.state.worker.execute(application.state.tasks.claim())
    result=client.get('/api/tasks/'+tid+'/diagnostics').json()
    assert result['task']['status']=='succeeded' and result['stats']['stored_articles']==3,result
    assert upstream.calls==['zhihu','cls','zhihu','cls']
    with Session(application.state.engine) as session:assert count(session,Article)==count(session,ArticleVersion)==3


def test_later_real_document_is_a_candidate_after_newsnow_discovery(app,upstream,monkeypatch):
    from news2douyin.candidates.models import SourceReport

    application,client,_storage=app
    item=upstream.responses['zhihu']['items'][0]
    upstream.set('zhihu',snapshot('zhihu',[item]))
    saved=profile(client,upstream,sources=['zhihu'])
    _,discovery=execute(application,client,client.post('/api/tasks/collect',json={'profile_name':saved['name']}))
    assert discovery['task']['status']=='succeeded'
    with Session(application.state.engine) as session:assert count(session,Article)==1 and count(session,Event)==0
    monkeypatch.setitem(collect.PROVIDERS,'worldnewsapi',lambda _config:[dict(url=item['url'],
        title='补读到的正式新闻标题',content='这是后续通过真实新闻源补充的完整报道正文。',
        published_at='2026-11-14T07:00:00Z',source=dict(domain='www.zhihu.com'),language='zh')])
    response=client.post('/api/profiles',json=dict(name='Later full text',provider='worldnewsapi',
        extra=dict(filter_mode='rules'),categories=[],keywords_include=[]))
    assert response.status_code==200
    _,upgraded=execute(application,client,client.post('/api/tasks/collect',json={'profile_name':'Later full text'}))
    assert upgraded['task']['status']=='succeeded',upgraded
    with Session(application.state.engine) as session:
        row=session.exec(select(Article)).one()
        assert row.provider=='worldnewsapi' and row.content=='这是后续通过真实新闻源补充的完整报道正文。'
        assert count(session,ArticleVersion)==2
        candidate=session.exec(select(SourceReport)).one()
        assert candidate.provider=='worldnewsapi' and candidate.url==item['url']
        assert candidate.language=='zh' and candidate.content==row.content
        assert count(session,ArticleEventLink)==count(session,Event)==0


def test_newsnow_model_extraction_uses_fixed_version_material_kind(app,upstream,monkeypatch):
    from news2douyin.extraction.models import VersionReading
    application,client,_storage=app
    item=upstream.responses['zhihu']['items'][0]
    upstream.set('zhihu',snapshot('zhihu',[item]))
    saved=profile(client,upstream,sources=['zhihu'])
    _,done=execute(application,client,client.post('/api/tasks/collect',json={'profile_name':saved['name']}))
    assert done['task']['status']=='succeeded'
    with Session(application.state.engine) as session:
        row=session.exec(select(Article)).one();key=row.article_key
        first=session.exec(select(ArticleVersion).where(ArticleVersion.article_key==key)).one()
        reading=session.get(VersionReading,first.id)
        original_state=reading.state
    blocked=client.post('/api/desk/articles/'+key+'/1/model',json={})
    assert blocked.status_code==422,blocked.text
    with Session(application.state.engine) as session:
        assert session.get(VersionReading,first.id).state==original_state=='metadata_only'
    page=client.get('/articles/'+key)
    assert page.status_code==200
    assert '未取得原文' in page.text
    class ReaderButtons(HTMLParser):
        def __init__(self):super().__init__();self.buttons=[]
        def handle_starttag(self,tag,attrs):
            values=dict(attrs)
            if tag=='button' and values.get('id')=='run-reader':self.buttons.append(values)
    parsed=ReaderButtons();parsed.feed(page.text)
    assert all('disabled' in attrs or 'hidden' in attrs for attrs in parsed.buttons)
    monkeypatch.setitem(collect.PROVIDERS,'worldnewsapi',lambda _config:[dict(url=item['url'],title='已读取原文',
        content='Donald Trump announced a tariff decision in this saved full report.',published_at='2026-11-14T07:00:00Z',
        source=dict(domain='www.zhihu.com'),language='en')])
    assert client.post('/api/profiles',json=dict(name='Upgrade for extraction',provider='worldnewsapi',
        extra=dict(filter_mode='rules'),categories=[],keywords_include=[])).status_code==200
    _,upgraded=execute(application,client,client.post('/api/tasks/collect',json={'profile_name':'Upgrade for extraction'}))
    assert upgraded['task']['status']=='succeeded'
    # The old metadata revision stays protected even though the mutable Article
    # now belongs to a genuine full-text provider.
    assert client.post('/api/desk/articles/'+key+'/1/model',json={}).status_code==422
    full=client.post('/api/desk/articles/'+key+'/2/model',json={})
    assert full.status_code==200,full.text
    with Session(application.state.engine) as session:
        version=session.exec(select(ArticleVersion).where(ArticleVersion.article_key==key,ArticleVersion.revision==2)).one()
        reading=session.get(VersionReading,version.id)
        assert reading.state=='model_queued' or (reading.state=='baseline' and reading.note=='model_after_baseline')
