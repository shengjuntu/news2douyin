import json
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.events import workbench as work, research
from news2douyin.events.schemas import MomentEdit,ResearchQuery
from news2douyin.storage.articles import record_version,latest_version,lock_news
from news2douyin.storage.models import (Article,ArticleEventLink,ArticleVersion,Event,EventState,EventMoment,
    EventActivity,EventResearch,ScriptPackage,DailySelection,CollectedObservation)
from news2douyin.events.service import refresh_event
from news2douyin.search.service import event_page
from news2douyin.server.app import create_app
from news2douyin.tasks.control import TaskConflict
from news2douyin.collect import service as collect
from news2douyin.collect.providers import search_news as provider


@pytest.fixture
def prepared(engine):
    with Session(engine) as s:
        articles=[Article(article_key='a',title='芯片计划公告',content='团队在九月一日公布芯片研发计划。方案包含三项任务。',url='https://one.test/a',canonical_url='https://one.test/a',source_domain='one.test',published_at='2026-09-02T01:00:00Z',country='cn',language='zh'),
                  Article(article_key='b',title='芯片实验进展',content='团队随后完成首轮实验。实验报告尚未公开。',url='https://two.test/b',canonical_url='https://two.test/b',source_domain='two.test',published_at='2026-09-04T01:00:00Z',country='cn',language='zh'),
                  Article(article_key='c',title='公交线路调整',content='城市发布公共交通线路调整方案。下月开始实施。',url='https://three.test/c',canonical_url='https://three.test/c',source_domain='three.test',published_at='2026-09-05T01:00:00Z',country='cn',language='zh')]
        s.add_all(articles)
        s.add_all([Event(event_key='first',event_title='自动标题'),Event(event_key='second',event_title='另一专题')])
        s.flush()
        for a in articles: record_version(s,a)
        s.add_all([ArticleEventLink(article_key='a',event_key='first'),ArticleEventLink(article_key='b',event_key='first'),ArticleEventLink(article_key='c',event_key='second')])
        s.add(ScriptPackage(package_key='historical',event_key='first',script_text='不得改写的历史稿件'))
        refresh_event(s,'first');refresh_event(s,'second');s.commit()
    return engine


def payload(s,key='first',articles=('a',),**override):
    sources=[]
    for a in articles:
        v=latest_version(s,a);doc=json.loads(v.payload_json)
        sources.append(dict(article_key=a,revision=v.revision,content_hash=v.content_hash,excerpt=doc['content'][:15]))
    data=dict(expected_version=work.workspace(s,key)['version'],title='宣布研发计划',description='根据报道整理的进展。',time_kind='occurred',date_start='2026-09-01',certainty='exact',sources=sources)
    data.update(override)
    return MomentEdit.model_validate(data).model_dump()


def test_moment_freezes_source_and_rejects_unquoted_claim(prepared):
    with Session(prepared) as s:
        before=work.workspace(s,'first')
        data=payload(s)
        saved=work.save_moment(s,'first',data)
        node=saved['moments'][0]
        assert node['date_start']=='2026-09-01' and node['sources'][0]['published_at'].startswith('2026-09-02')
        lock_news(s)
        article=s.exec(select(Article).where(Article.article_key=='a')).one()
        article.content='更新：原研发计划延期，新的实验日期尚未公布。'
        s.add(article);record_version(s,article);refresh_event(s,'first');s.commit()
        now=work.workspace(s,'first')
        assert now['moments'][0]['sources'][0]['revision']==1
        assert now['moments'][0]['sources'][0]['newer_revision_available']
        assert '九月一日' in now['moments'][0]['sources'][0]['excerpt']
        invalid=payload(s)
        invalid['sources'][0]['excerpt']='不存在的报道原文'
        with pytest.raises(ValueError,match='连续片段'):
            work.save_moment(s,'first',invalid)
        s.rollback()
        with pytest.raises(TaskConflict,match='已更新'):
            work.edit_event(s,'first',dict(expected_version=before['version'],title='旧页面覆盖',summary='',topic=''))
        s.rollback()
        assert len(work.workspace(s,'first')['moments'])==1
        bundle=work.export_evidence(s,'first')
        versions={(v['article_key'],v['revision']):v['document'] for v in bundle['source_versions']}
        assert ('a',1) in versions and ('a',2) in versions
        assert '九月一日' in versions['a',1]['content']


def test_node_constraints_dates_unknown_and_excerpts(prepared):
    with Session(prepared) as s:
        data=payload(s,time_kind='unknown',date_start='',certainty='unknown')
        saved=work.save_moment(s,'first',data)
        assert saved['moments'][0]['time_kind']=='unknown'
        for values in [dict(time_kind='unknown',date_start='2026-09-01'),dict(date_start='2026-02-30'),dict(date_end='2026-08-31'),dict(certainty='disputed',time_note='')]:
            with pytest.raises(ValueError): payload(s,**values)
        with pytest.raises(ValueError,match='先加入'):
            work.save_moment(s,'first',payload(s,articles=('c',)))
        s.rollback()
        bad=payload(s);bad['sources'][0]['content_hash']='wrong'
        with pytest.raises(TaskConflict,match='指纹'):
            work.save_moment(s,'first',bad)
        s.rollback()


def test_removal_preserves_news_scripts_and_deleted_node_history(prepared):
    with Session(prepared) as s:
        saved=work.save_moment(s,'first',payload(s))
        node=saved['moments'][0]
        with pytest.raises(TaskConflict,match='引用'):
            work.remove_source(s,'first','a',saved['version'])
        s.rollback()
        saved=work.delete_moment(s,'first',node['moment_key'],saved['version'])
        saved=work.remove_source(s,'first','a',saved['version'])
        assert [a['article_key'] for a in saved['sources']]==['b']
        assert s.exec(select(Article).where(Article.article_key=='a')).one()
        assert latest_version(s,'a').revision==1
        assert s.exec(select(ScriptPackage)).one().script_text=='不得改写的历史稿件'
        assert s.get(EventMoment,node['moment_key']).deleted
        history=json.loads(s.exec(select(EventActivity).where(EventActivity.action=='delete_moment')).one().payload_json)
        assert history['before']['sources'][0]['article_key']=='a'


def test_manual_title_survives_new_collection(prepared,tmp_path,monkeypatch):
    with Session(prepared) as s:
        view=work.workspace(s,'first')
        work.edit_event(s,'first',dict(expected_version=view['version'],title='人工命名的研发专题',summary='人工整理摘要',topic='研发'))
        article=s.exec(select(Article).where(Article.article_key=='a')).one()
        article.title='更新后的新闻标题'
        s.add(article);refresh_event(s,'first');s.commit()
        current=work.workspace(s,'first')
        assert current['title']=='人工命名的研发专题' and current['summary']=='人工整理摘要'
        assert event_page(s,query='人工命名').total==1


def test_split_blocks_cross_group_evidence_then_moves_whole_node(prepared):
    with Session(prepared) as s:
        view=work.save_moment(s,'first',payload(s,articles=('a','b')))
        node=view['moments'][0]
        with pytest.raises(TaskConflict,match='两组'):
            work.split(s,'first',dict(expected_version=view['version'],article_keys=['a'],title='拆出',note='另行跟踪'))
        s.rollback()
        assert len(list(s.exec(select(Event))))==2
        view=work.save_moment(s,'first',payload(s,articles=('a',)),node['moment_key'])
        result=work.split(s,'first',dict(expected_version=view['version'],article_keys=['a'],title='拆出专题',note='公告单独跟踪'))
        target=result['workspace']
        assert [m['moment_key'] for m in target['moments']]==[node['moment_key']]
        assert [a['article_key'] for a in target['sources']]==['a']
        original=work.workspace(s,'first')
        assert [a['article_key'] for a in original['sources']]==['b'] and original['moments']==[]
        assert original['relations'][0]['event_key']==target['event_key']
        assert s.exec(select(ScriptPackage)).one().event_key=='first'


def test_merge_shared_source_and_archive_old_event(prepared):
    with Session(prepared) as s:
        first=work.workspace(s,'first')
        first=work.add_sources(s,'first',dict(expected_version=first['version'],article_keys=['c'],note='参考'))
        first=work.save_moment(s,'first',payload(s))
        second=work.workspace(s,'second')
        result=work.merge(s,'first',dict(expected_version=first['version'],target_event_key='second',target_expected_version=second['version'],note='确认同一专题'))
        assert len(result['workspace']['sources'])==3
        assert len(result['workspace']['moments'])==1
        assert work.workspace(s,'first')['merged_into']=='second'
        assert event_page(s).total==1
        assert s.exec(select(ScriptPackage)).one().event_key=='first'
        with pytest.raises(TaskConflict,match='已并入'):
            work.add_sources(s,'first',dict(expected_version=work.workspace(s,'first')['version'],article_keys=['c']))
        s.rollback()
        assert len(list(s.exec(select(ArticleVersion))))==3


def test_related_event_direction_and_merged_target_resolution(prepared):
    with Session(prepared) as s:
        first=work.workspace(s,'first')
        first=work.relate(s,'first',dict(expected_version=first['version'],target_event_key='second',kind='background',note='此前的背景'))
        assert first['relations'][0]['direction']=='out'
        second=work.workspace(s,'second')
        assert second['relations'][0]['direction']=='in'
        third=work.create_event(s,dict(title='第三专题'))
        work.merge(s,'second',dict(expected_version=second['version'],target_event_key=third['event_key'],target_expected_version=third['version'],note='专题整合'))
        first=work.workspace(s,'first')
        assert first['relations'][0]['event_key']==third['event_key']
        first=work.unrelate(s,'first',first['relations'][0]['relation_key'],first['version'])
        assert first['relations']==[]


def test_concurrent_curators_only_one_edit_commits(prepared):
    with Session(prepared) as s: version=work.workspace(s,'first')['version']
    def edit(title):
        with Session(prepared) as s:
            try: return work.edit_event(s,'first',dict(expected_version=version,title=title,summary='',topic=''))['title']
            except TaskConflict: return 'conflict'
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(edit,['编辑甲','编辑乙']))
    assert results.count('conflict')==1


def test_research_preview_import_provenance_and_idempotency(prepared,monkeypatch):
    item=dict(title='补充实验方案',content='实验小组提出全新的对照实验方法，尚需后续验证。',url='https://external.test/report',source={'domain':'external.test'},published_at='2026-09-08T00:00:00Z',country='cn',language='zh',provider='worldnewsapi')
    monkeypatch.setattr(research,'search_news',lambda q:{'items':[item],'available':10})
    query=ResearchQuery(query='芯片研发计划').model_dump()
    report=research.search(prepared,'first',query)
    with Session(prepared) as s:
        assert len(list(s.exec(select(Article))))==3
        before=work.workspace(s,'first')
        data=dict(expected_version=before['version'],indexes=[0])
        view=research.import_results(s,'first',report['research_key'],data)
        assert len(view['sources'])==3
        record=research.result(s,'first',report['research_key'])
        ref=record['imported']['0'];article_key=ref['article_key']
        assert latest_version(s,article_key).origin=='external_research'
        assert latest_version(s,article_key).run_key==report['research_key']
        assert not list(s.exec(select(CollectedObservation)))
        version=view['version']
        again=research.import_results(s,'first',report['research_key'],dict(expected_version=version,indexes=[0]))
        assert again['version']==version and len(list(s.exec(select(Article))))==4
        work.remove_source(s,'first',article_key,version)
        latest=work.workspace(s,'first')
        assert research.import_results(s,'first',report['research_key'],dict(expected_version=latest['version'],indexes=[0]))['counts']['article_count']==3
        old=s.exec(select(Article).where(Article.article_key==article_key)).one()
        old.content='已经由采集更新的更长正文，不能被旧搜索覆盖。'
        s.add(old);record_version(s,old);s.commit()
    other=research.search(prepared,'second',query)
    with Session(prepared) as s:
        v=work.workspace(s,'second')
        research.import_results(s,'second',other['research_key'],dict(expected_version=v['version'],indexes=[0]))
        assert '不能被旧搜索覆盖' in s.exec(select(Article).where(Article.article_key==article_key)).one().content
        assert len(list(s.exec(select(Article))))==4
        assert latest_version(s,article_key).revision==2


def test_research_failure_is_saved_and_safe(prepared,monkeypatch):
    monkeypatch.setattr(research,'search_news',Mock(side_effect=RuntimeError('secret-key-do-not-emit')))
    report=research.search(prepared,'first',ResearchQuery(query='芯片研发计划').model_dump())
    assert report['status']=='failed' and 'secret-key' not in json.dumps(report)
    with Session(prepared) as s:
        assert len(work.workspace(s,'first')['research'])==1
        with pytest.raises(TaskConflict):
            research.import_results(s,'first',report['research_key'],dict(expected_version=work.workspace(s,'first')['version'],indexes=[0]))


def test_search_provider_contract_normalization_and_key_header(monkeypatch):
    monkeypatch.setenv('API_KEY','private-test-key')
    response=Mock(status_code=200)
    response.json.return_value={'available':4,'news':[{'id':42,'title':'合法新闻','text':'已取得正文','url':'https://source.test/news','publish_date':'2026-09-29 17:30:00','source_country':'cn','language':'zh'},
        {'title':'摘要结果','summary':'只有摘要','url':'https://source.test/summary','publish_date':'unknown'},
        {'title':'无效地址','url':'javascript:alert(1)'}, {'title':'凭据地址','url':'https://user:password@source.test/'}]}
    get=Mock(return_value=response)
    monkeypatch.setattr(provider.requests,'get',get)
    query=ResearchQuery(query='芯片研发计划',date_from='2026-09-30',date_to='2026-09-30',country='cn').model_dump()
    result=provider.search_news(query)
    assert len(result['items'])==2 and result['available']==4
    kw=get.call_args.kwargs
    assert kw['headers']=={'x-api-key':'private-test-key'}
    assert kw['params']['earliest-publish-date']=='2026-09-29 16:00:00'
    assert kw['params']['latest-publish-date']=='2026-09-30 15:59:59'
    assert kw['params']['source-countries']=='cn' and kw['params']['text']=='芯片研发计划'
    assert 'private-test-key' not in json.dumps(result)
    assert result['items'][1]['research_meta']['content_kind']=='summary'
    response.status_code=429
    with pytest.raises(ValueError,match='额度'): provider.search_news(query)


def test_keyword_collection_uses_requested_day_then_filters(engine,tmp_path,monkeypatch):
    from news2douyin.collect.providers import worldnewsapi
    fetch=Mock(return_value={'items':[dict(title='芯片研发进展',content='团队公布芯片技术方案。',url='https://source.test/a',source={'domain':'source.test'},published_at='2026-09-30T00:00:00Z')], 'available':1})
    monkeypatch.setattr(provider,'search_news',fetch)
    monkeypatch.setattr(worldnewsapi,'fetch_top_news',Mock(side_effect=AssertionError('must not call top news')))
    with Session(engine) as s:
        collect.create_or_update_profile(s,dict(name='keyword',provider='worldnewsapi',country='cn',language='zh',extra={'collection_mode':'search','search_query':'芯片研发计划','filter_mode':'rules'}))
        run=collect.run_collection(s,'keyword',storage_root=tmp_path/'runs',override={'date_str':'2026-09-30','timezone':'Asia/Shanghai'})
        assert run.status=='succeeded'
        assert fetch.call_args.args[0]['date_from']==fetch.call_args.args[0]['date_to']=='2026-09-30'
        assert len(list(s.exec(select(Article))))==1


def test_event_api_validation_export_and_pages(tmp_path):
    app=create_app(db_url=f'sqlite:///{tmp_path}/app.db',storage_root=str(tmp_path/'runs'))
    client=TestClient(app)
    response=client.post('/api/events',json={'title':'人工创建专题'})
    assert response.status_code==200
    view=response.json();key=view['event_key']
    assert client.get('/events/'+key).status_code==200
    assert client.get('/events').status_code==200
    assert client.get('/api/events/'+key+'/evidence-export').json()['kind']=='news2douyin.event_evidence'
    assert client.put('/api/events/'+key+'/workspace',json={'title':'新名称','expected_version':999}).status_code==409
    assert client.post('/api/events/'+key+'/research',json={'query':'短'}).status_code==422
    assert client.post('/api/profiles',json={'name':'keywords','extra':{'collection_mode':'search','search_query':'芯片研发计划'},'max_items':101}).status_code==422
    assert client.post('/api/profiles',json={'name':'keywords','extra':{'collection_mode':'search','search_query':'芯片研发计划'},'max_items':100}).status_code==200
    assert client.get('/events/notfound').status_code==404
    app.state.engine.dispose()
