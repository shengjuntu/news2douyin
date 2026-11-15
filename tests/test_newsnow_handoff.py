"""Aggregator headlines/excerpts stay leads until an original page is read."""
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.research import service as research
from news2douyin.research.models import ResearchCase, ResearchSource, ResearchRun
from news2douyin.research.tools import ResearchTools
from news2douyin.server.app import create_app
from news2douyin.storage.articles import record_version
from news2douyin.storage.models import Article, ArticleVersion
from news2douyin.topics import service as topics
from news2douyin.topics.models import TopicSource
from news2douyin.workbench import service as workbench
from test_postgres_integration import postgres_url


@pytest.fixture(params=['sqlite','postgres'])
def newsnow_case(request,tmp_path,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL','')
    monkeypatch.setenv('NEWS2DOUYIN_GDELT_STREAM_ENABLED','0')
    monkeypatch.setenv('NEWS2DOUYIN_GDELT_PROJECTION_ENABLED','0')
    url=request.getfixturevalue('postgres_url') if request.param=='postgres' else 'sqlite:///'+str(tmp_path/'leads.db')
    app=create_app(db_url=url,storage_root=str(tmp_path/'runs'))
    raw=dict(material_kind='newsnow_discovery',content_kind='source_excerpt',requires_original_text=True,
             newsnow=dict(source_id='zhihu',source_type='hottest',snapshot_reported_at='2026-11-14T05:00:00Z',
                          observed_at='2026-11-14T05:05:00Z',list_position=3))
    with Session(app.state.engine) as session:
        row=Article(article_key='newsnow-lead',provider='newsnow',title='待核对的关税消息',
                    content='榜单页面提供的摘要，尚未取得原文。',url='https://source.example/report',
                    published_at='',language='zh',raw_json=json.dumps(raw,ensure_ascii=False))
        session.add(row);session.flush();record_version(session,row)
        original=Article(article_key='full-report',provider='fixture',title='Original report',
                         content='A ministry confirmed the actual action in this saved original.',
                         url='https://ministry.example/report')
        session.add(original);session.flush();record_version(session,original);session.commit()
    client=TestClient(app)
    try:yield app,client,raw
    finally:client.close();app.state.engine.dispose()


def select_lead(app,*,mixed=False):
    return workbench.start(app.state.engine,dict(request_id='newsnow-selected-source-'+str(mixed),
        articles=['newsnow-lead','full-report'] if mixed else ['newsnow-lead']))


class Runtime:
    def __init__(self):self.calls=[]
    def enqueue(self,*args,**kwargs):self.calls.append((args,kwargs))


def test_freeze_preserves_aggregator_provenance_and_ui_material_boundary(newsnow_case):
    app,client,raw=newsnow_case
    task=select_lead(app)
    source=task['sources'][0]
    assert source['source_kind']=='newsnow_metadata'
    assert source['newsnow']==raw['newsnow']
    assert source['published_at']=='' and source['requires_original_text']
    assert source['has_readable_text'] is False and task['materials']['can_write_from_sources'] is False
    assert task['materials']['stage']=='leads'
    page=client.get('/workbench/'+task['key']+'/selection')
    assert page.status_code==200 and 'NewsNow榜单／摘要线索，尚未取得原文' in page.text
    assert 'id="topic-short" class="ghost" disabled' in page.text
    detail=client.get('/api/topics/'+task['key']+'/sources/'+source['source_id']).json()
    assert detail['content']=='榜单页面提供的摘要，尚未取得原文。'
    assert client.get(detail['detail_url']).status_code==200
    with Session(app.state.engine) as session:
        frozen=json.loads(session.get(TopicSource,source['source_id']).document_json)
        assert frozen['source_kind']=='newsnow_metadata' and frozen['requires_original_text']
        row=session.exec(select(Article).where(Article.article_key=='newsnow-lead')).one()
        assert row.content==detail['content'] and json.loads(row.raw_json)==raw


def test_excerpt_only_short_is_rejected_from_handoff_and_direct_case(newsnow_case):
    app,_,_=newsnow_case
    task=select_lead(app);runtime=Runtime()
    with pytest.raises(ValueError,match='先补材或研究'):
        topics.handoff(app.state.engine,runtime,task['key'],dict(revision=task['topic_revision'],
            request_id='newsnow-unsafe-short',action='short'))
    assert runtime.calls==[]
    with pytest.raises(ValueError,match='先补材或研究'):
        research.create_case(app.state.engine,dict(article_keys=['newsnow-lead'],goal='Draft from lead only'),
            preparation=dict(request_key='newsnow-direct-unsafe-short',request_hash='fixed',options={'source_only':True}))


def test_deep_research_accepts_lead_but_requires_original_for_evidence(newsnow_case):
    app,_,raw=newsnow_case
    task=select_lead(app);runtime=Runtime()
    handoff=topics.handoff(app.state.engine,runtime,task['key'],dict(revision=task['topic_revision'],
        request_id='newsnow-deep-handoff',action='deep'))
    assert len(runtime.calls)==1
    with Session(app.state.engine) as session:
        case=session.get(ResearchCase,handoff['case_id'])
        assert '不是已取得的新闻全文' in case.goal
        source=session.exec(select(ResearchSource).where(ResearchSource.case_id==case.case_id)).one()
        assert source.kind=='search_snippet'
        provenance=json.loads(source.provenance_json)
        assert provenance['source_kind']=='newsnow_metadata' and provenance['newsnow']==raw['newsnow']
        paragraph=json.loads(source.paragraphs_json)[0]
        claim=dict(text='关税政策已发生变化。',kind='fact',evidence=[dict(source_id=source.source_id,
            paragraph_id=paragraph['id'],quote=paragraph['text'],relation='supports')])
        with pytest.raises(ValueError,match='搜索摘要'):
            research.save_claim(session,case,SimpleNamespace(run_id='test-run'),claim)
        original=research.add_source(session,case,title='Read original',url='https://source.example/original',
            text='The ministry announced the actual tariff change.',kind='web_page',provenance={'read_by':'fixture'})
        paragraph=json.loads(original.paragraphs_json)[0]
        claim['evidence']=[dict(source_id=original.source_id,paragraph_id=paragraph['id'],quote=paragraph['text'],relation='supports')]
        assert research.save_claim(session,case,SimpleNamespace(run_id='test-run'),claim)['claim_id']


def test_read_article_tool_does_not_promote_excerpt_to_original(newsnow_case):
    app,_,_=newsnow_case
    case=research.create_case(app.state.engine,dict(article_keys=['newsnow-lead'],goal='Read the original before claiming facts'))
    ident='research-newsnow-local-read'
    with Session(app.state.engine) as session:
        row=session.get(ResearchCase,case['case_id']);row.active_run_id=ident;session.add(row)
        session.add(ResearchRun(run_id=ident,case_id=row.case_id,status='waiting'));session.commit()
    result=ResearchTools(app.state.engine,None).call('research_read_source',dict(run_id=ident,article_key='newsnow-lead'))
    assert result['kind']=='search_snippet' and result['provenance']['source_kind']=='newsnow_metadata'
    assert result['provenance']['requires_original_text'] is True


def test_real_article_and_translation_keep_previous_behavior(newsnow_case):
    app,_,_=newsnow_case
    task=select_lead(app,mixed=True)
    assert task['materials']['readable']==1 and task['materials']['can_write_from_sources'] is True
    lead=next(row for row in task['sources'] if row['source_kind']=='newsnow_metadata')
    with Session(app.state.engine) as session:
        before=session.get(TopicSource,lead['source_id']).document_json
        row=session.exec(select(Article).where(Article.article_key=='newsnow-lead')).one()
        row.content='后来变更的聚合摘要。';session.add(row);record_version(session,row);session.commit()
    from news2douyin.translations.service import submit
    translated=submit(app.state.engine,kind='topic_source',key=lead['source_id'])
    assert translated['status']=='queued' and '榜单页面提供的摘要' in translated['original']
    assert '后来变更' not in translated['original']
    with Session(app.state.engine) as session:
        assert session.get(TopicSource,lead['source_id']).document_json==before
        original=next(row for row in task['sources'] if row['article_key']=='full-report')
        assert original['source_kind']=='news_snapshot' and original['has_readable_text']


@pytest.mark.parametrize('state',[None,'ready','metadata_only'])
def test_manual_model_rejects_newsnow_revision_without_touching_queue(newsnow_case,state):
    app,client,_=newsnow_case
    from news2douyin.extraction.models import VersionReading
    with Session(app.state.engine) as session:
        version=session.exec(select(ArticleVersion).where(ArticleVersion.article_key=='newsnow-lead')).one()
        ident=version.id;before=version.payload_json
        if state:
            session.merge(VersionReading(version_id=ident,state=state,note='before-test',method='rules'))
            session.commit()
        else:
            existing=session.get(VersionReading,ident)
            if existing:session.delete(existing);session.commit()
    response=client.post('/api/desk/articles/newsnow-lead/1/model',json={})
    assert response.status_code==422 and '先补充真实原文' in response.json()['detail']
    data=client.get('/api/desk/articles/newsnow-lead/1/mentions').json()
    assert data['lead_only'] and data['model_allowed'] is False
    page=client.get('/articles/newsnow-lead?revision=1')
    assert page.status_code==200 and 'id="run-reader"' not in page.text
    assert '先补充真实原文' in page.text
    with Session(app.state.engine) as session:
        job=session.get(VersionReading,ident)
        assert (job.state if job else None)==state
        assert not job or job.note=='before-test'
        assert session.get(ArticleVersion,ident).payload_json==before


def test_fixed_revision_guard_survives_upgrade_but_real_original_can_queue(newsnow_case):
    app,client,_=newsnow_case
    from news2douyin.extraction.models import VersionReading
    with Session(app.state.engine) as session:
        row=session.exec(select(Article).where(Article.article_key=='newsnow-lead')).one()
        old=session.exec(select(ArticleVersion).where(ArticleVersion.article_key==row.article_key)).one()
        original_payload=old.payload_json
        row.provider='worldnewsapi';row.content='Donald Trump signed the treaty in Paris yesterday.'
        row.raw_json=json.dumps({'material_kind':'connector_document','content_kind':'full_text'})
        session.add(row);record_version(session,row);session.flush()
        new=session.exec(select(ArticleVersion).where(ArticleVersion.article_key==row.article_key,ArticleVersion.revision==2)).one()
        session.merge(VersionReading(version_id=new.id,state='ready',method='rules'));session.commit()
    assert client.post('/api/desk/articles/newsnow-lead/1/model',json={}).status_code==422
    new=client.post('/api/desk/articles/newsnow-lead/2/model',json={})
    assert new.status_code==200 and new.json()['state']=='model_queued'
    page=client.get('/articles/newsnow-lead?revision=1')
    assert 'id="run-reader"' not in page.text
    page=client.get('/articles/newsnow-lead?revision=2')
    assert page.status_code==200 and 'id="run-reader"' in page.text
    with Session(app.state.engine) as session:
        old=session.exec(select(ArticleVersion).where(ArticleVersion.article_key=='newsnow-lead',ArticleVersion.revision==1)).one()
        assert old.payload_json==original_payload


def test_old_queued_excerpt_never_calls_model_or_becomes_confirmed_text(newsnow_case,monkeypatch):
    app,client,_=newsnow_case
    from news2douyin.extraction import service as extraction
    from news2douyin.extraction.models import VersionReading,TextMention,MentionReview
    with Session(app.state.engine) as session:
        version=session.exec(select(ArticleVersion).where(ArticleVersion.article_key=='newsnow-lead')).one()
        ident=version.id
        session.merge(VersionReading(version_id=ident,state='model_queued'))
        session.add(TextMention(id='old-newsnow-mention',version_id=ident,article='newsnow-lead',revision=1,
            data=json.dumps(dict(id='old-newsnow-mention',kind='statement',status='reported',field='content',
                start=0,end=5,quote='榜单页面提供的摘要',trigger='摘要',arguments=[],extractor='model'))))
        session.commit()
    calls=[]
    monkeypatch.setattr(extraction.reader,'extract',lambda *_args,**_kw:calls.append(True))
    assert extraction.step(app.state.engine,model=True) is False
    assert calls==[]
    rejected=client.post('/api/desk/mentions/old-newsnow-mention/review',json={'decision':'accepted'})
    assert rejected.status_code==422 and '先读取真实原文' in rejected.json()['detail']
    assert client.post('/api/desk/mentions/old-newsnow-mention/review',json={'decision':'rejected'}).status_code==200
    with Session(app.state.engine) as session:
        assert session.get(VersionReading,ident).state=='metadata_only'
        assert session.get(MentionReview,'old-newsnow-mention').decision=='rejected'


def test_gkg_manual_extraction_behavior_is_preserved(newsnow_case):
    app,client,_=newsnow_case
    from news2douyin.extraction.models import VersionReading
    with Session(app.state.engine) as session:
        row=Article(article_key='gkg-existing',provider='gkg',title='An existing GKG title.',content='',
                    raw_json=json.dumps({'material_kind':'discovery_metadata'}))
        session.add(row);session.flush();record_version(session,row);session.flush()
        version=session.exec(select(ArticleVersion).where(ArticleVersion.article_key==row.article_key)).one()
        session.merge(VersionReading(version_id=version.id,state='metadata_only'));session.commit()
    response=client.post('/api/desk/articles/gkg-existing/1/model',json={})
    assert response.status_code==200 and response.json()['state']=='model_queued'
