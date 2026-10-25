"""Real API/SQLite/MCP integration; RunDesk/model are contract fixtures."""
import json
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlmodel import Session,select
from test_research import research,FakeRunDesk,rpc,value
from news2douyin.server.app import create_app
from fastapi.testclient import TestClient
from news2douyin.topics.models import TopicWorkspace,TopicMessage,TopicSource,TopicHandoff
from news2douyin.research.models import ResearchRun,ResearchCase,ResearchSubmission,ResearchSource
from news2douyin.storage.models import Article,Event,ArticleEventLink
from news2douyin.topics import service as ts
from news2douyin.research import service as rs


def make(research,scope=None):
 app,c,f=research
 r=c.post('/api/topics',json=dict(request_id='create-one',scope=scope or {}));assert r.status_code==201,r.text
 return r.json()


def setup(research):
 app,c,f=research;assert c.post('/api/research/setup').status_code==200
 return app,c,f


def send(research,w,text='看看有哪些值得做短视频的方向',request_id='message-one'):
 app,c,f=research
 r=c.post('/api/topics/'+w['workspace_id']+'/messages',json=dict(revision=w['revision'],request_id=request_id,text=text));assert r.status_code==202,r.text
 app.state.research.dispatch(r.json()['run_id'])
 return r.json()['run_id']


def tool(research,name,run,**kw):return value(rpc(research,name,dict(turn_id=run,**kw)))


def test_workspace_history_dates_pagination_and_restart(research,tmp_path):
 app,c,f=research
 with Session(app.state.engine) as s:
  for i in range(4):s.add(Article(article_key='old-'+str(i),title='历史价格消息 '+str(i),content='旧价格的原始报道',published_at='2020-03-01T00:00:00Z'))
  s.add(Event(event_key='old-event',event_title='价格调整专题'));s.add(ArticleEventLink(event_key='old-event',article_key='old-0'));s.commit()
 w=make(research,dict(period='custom',date_from='2020-01-01',date_to='2020-12-31',query='价格'))
 url='/api/topics/'+w['workspace_id']
 data=c.get(url+'/search',params={'limit':2}).json();assert data['total']==4 and data['has_more']
 assert len(c.get(url+'/search',params={'limit':2,'offset':2}).json()['items'])==2
 events=c.get(url+'/search',params={'kind':'events'}).json();assert events['items'][0]['event_key']=='old-event'
 # Rendering never starts a model or creates a research run.
 for page in ['/','/topics','/topics?workspace='+w['workspace_id']]:assert c.get(page).status_code==200
 assert f.sessions=={}
 with Session(app.state.engine) as s:assert s.exec(select(ResearchRun)).all()==[]
 assert c.get('/api/research/cases').json()==[]
 same=c.post('/api/topics',json={'request_id':'create-one','scope':w['scope']});assert same.status_code==201
 # All fields defaulted in the original input, so the same canonical scope matches.
 assert same.json()['workspace_id']==w['workspace_id']
 restarted=create_app(db_url=str(app.state.engine.url),storage_root=str(tmp_path/'runs'))
 assert TestClient(restarted).get(url).json()['scope']==w['scope']
 assert c.get(url+'/search',params={'scope':json.dumps(dict(period='custom',date_from='2021-01-01',date_to='2020-01-01'))}).status_code==400


def test_edit_conflict_and_immutable_selected_sources(research):
 app,c,f=research;w=make(research);url='/api/topics/'+w['workspace_id']
 added=c.post(url+'/sources',json={'revision':1,'article_key':'seed'});assert added.status_code==200,added.text
 w=added.json();sid=w['source_ids'][0]
 assert c.post(url+'/sources',json={'revision':1,'article_key':'seed'}).status_code==400
 with Session(app.state.engine) as s:
  a=s.exec(select(Article).where(Article.article_key=='seed')).one();a.content='CHANGED AFTER SELECTION';s.add(a);s.commit()
 assert 'introduced in 2023' in c.get(url+'/sources/'+sid).json()['content']
 other=c.post('/api/topics',json={'request_id':'different-workspace'}).json()
 assert c.get('/api/topics/'+other['workspace_id']+'/sources/'+sid).status_code==400
 bad=dict(revision=w['revision'],title='other',angle='',scope={},source_ids=[sid])
 assert c.put('/api/topics/'+other['workspace_id'],json=bad).status_code==400


def test_selection_conversation_mcp_candidates_and_same_session_followup(research):
 app,c,f=setup(research);w=make(research);run=send(research,w)
 calls=[x for x in f.calls if x[0]=='POST' and x[1].endswith('/turns')]
 assert 'topic_get_workspace' in calls[-1][2]['text'] and calls[-1][2]['skills']==[]
 ctx=tool(research,'topic_get_workspace',run);assert ctx['workspace_id']==w['workspace_id']
 results=tool(research,'topic_search_library',run,query='policy',period='all');assert results['library_only'] and results['total']==1
 source=tool(research,'topic_read_article',run,article_key='seed');sid=source['source_id'];assert source['paragraphs']
 candidate=dict(title='政策变化',angle='面向消费者',summary='旧政策与新政策的变化',source_ids=[sid],gaps=['实际影响待核实'])
 first=tool(research,'topic_save_candidates',run,candidates=[candidate]);assert first==tool(research,'topic_save_candidates',run,candidates=[candidate])
 tool(research,'topic_save_reply',run,text='已整理一个可讲方向，实际影响仍待核实。',source_ids=[sid])
 assert c.get('/api/topics/'+w['workspace_id']).json()['source_ids']==[]
 assert rpc(research,'research_search',dict(run_id=run,query='policy',scope='web'))['isError']
 sid_remote=next(iter(f.sessions));f.sessions[sid_remote]['status']='completed'
 app.state.research.sync(run)
 updated=c.get('/api/topics/'+w['workspace_id']).json();assert updated['messages'][0]['status']=='completed'
 assert updated['messages'][0]['reply']['source_ids']==[sid]
 assert c.get('/api/research/cases').json()==[]
 # Original message retries return the old run before checking busy/revision.
 again=c.post('/api/topics/'+w['workspace_id']+'/messages',json={'revision':1,'request_id':'message-one','text':'看看有哪些值得做短视频的方向'})
 assert again.status_code==202 and again.json()['run_id']==run
 follow=send(research,updated,'继续解释对消费者的影响','message-two');assert follow!=run
 assert len(f.sessions)==1
 with Session(app.state.engine) as s:
  assert s.get(ResearchRun,follow).session_id==s.get(ResearchRun,run).session_id
 assert c.post('/api/topics/'+w['workspace_id']+'/messages',json={'revision':1,'request_id':'message-three','text':'重叠消息'}).status_code==400
 assert rpc(research,'topic_save_reply',dict(turn_id=run,text='过期写入',source_ids=[]))['isError']
 assert not any(x[1].endswith('/environments') for x in f.calls)


def test_topic_tools_reject_unread_sources_and_stopped_writes(research):
 app,c,f=setup(research);w=make(research);run=send(research,w)
 assert rpc(research,'topic_save_reply',dict(turn_id=run,text='冒充阅读',source_ids=['not-read']))['isError']
 assert rpc(research,'topic_save_candidates',dict(turn_id=run,candidates=[dict(title='标题',summary='无依据',source_ids=['unknown'])]))['isError']
 assert rpc(research,'topic_read_article',dict(turn_id=run,article_key='seed',offset=-1))['isError']
 assert c.post('/api/topics/'+w['workspace_id']+'/stop').status_code==200
 assert rpc(research,'topic_save_reply',dict(turn_id=run,text='停止后的写入',source_ids=[]))['isError']


@pytest.mark.parametrize('action,write_script,expected_searches,short',[
 ('short',True,0,True),('materials',True,3,True),('materials',False,3,False),('deep',True,10,False)])
def test_handoff_keeps_selected_version_and_recovers_original_task(research,action,write_script,expected_searches,short):
 app,c,f=setup(research);w=make(research);url='/api/topics/'+w['workspace_id']
 w=c.post(url+'/sources',json={'revision':1,'article_key':'seed'}).json()
 with Session(app.state.engine) as s:
  a=s.exec(select(Article).where(Article.article_key=='seed')).one();a.content='UPDATED AFTER TOPIC SELECTION';s.add(a);s.commit()
 payload=dict(revision=w['revision'],request_id='handoff-one',action=action,write_script=write_script)
 r=c.post(url+'/handoffs',json=payload);assert r.status_code==202,r.text
 case=c.get('/api/research/cases/'+r.json()['case_id']).json()
 assert case['strategy']['max_searches']==expected_searches
 assert bool(case['short_script'])==short
 with Session(app.state.engine) as s:
  source=s.exec(select(ResearchSource).where(ResearchSource.case_id==case['case_id'])).first()
  assert 'introduced in 2023' in source.paragraphs_json and 'UPDATED AFTER' not in source.paragraphs_json
  assert json.loads(source.provenance_json)['topic_source_id']==w['source_ids'][0]
  run=s.get(ResearchRun,case['active_run_id']);run.status='completed';s.add(run);s.commit()
 retry=c.post(url+'/handoffs',json=payload);assert retry.status_code==202 and retry.json()==r.json()
 resumed=c.post(url+'/handoffs/'+r.json()['handoff_id']+'/resume');assert resumed.status_code==202
 with Session(app.state.engine) as s:
  assert len(list(s.exec(select(ResearchRun).where(ResearchRun.case_id==case['case_id']))))==1
 assert c.post(url+'/handoffs',json={**payload,'action':'deep' if action!='deep' else 'short'}).status_code==400


def test_concurrent_message_and_handoff_receipts(research):
 app,c,f=setup(research);w=make(research);url='/api/topics/'+w['workspace_id']
 def message(_):return c.post(url+'/messages',json={'revision':1,'request_id':'same-concurrent-message','text':'查查旧政策'}).json()
 with ThreadPoolExecutor(max_workers=3) as pool:rows=list(pool.map(message,range(3)))
 assert len({r['run_id'] for r in rows})==1
 w=c.post(url+'/sources',json={'revision':1,'article_key':'seed'}).json()
 payload={'revision':w['revision'],'request_id':'same-concurrent-handoff','action':'materials'}
 def handoff(_):
  r=c.post(url+'/handoffs',json=payload);assert r.status_code==202,r.text;return r.json()
 with ThreadPoolExecutor(max_workers=3) as pool:rows=list(pool.map(handoff,range(3)))
 assert len({r['case_id'] for r in rows})==1
 with Session(app.state.engine) as s:
  assert len(list(s.exec(select(TopicHandoff))))==1
  assert len(list(s.exec(select(ResearchRun).where(ResearchRun.case_id==rows[0]['case_id']))))==1


def test_handoff_is_accepted_even_before_environment_configuration(research):
 app,c,f=research;w=make(research);url='/api/topics/'+w['workspace_id']
 w=c.post(url+'/sources',json={'revision':1,'article_key':'seed'}).json()
 payload={'revision':w['revision'],'request_id':'not-yet-configured','action':'materials'}
 r=c.post(url+'/handoffs',json=payload);assert r.status_code==202 and r.json()['pending']
 assert c.post(url+'/handoffs',json=payload).json()==r.json()
 assert len(c.get(url).json()['tasks'])==1
 assert not c.get(url).json()['tasks'][0]['started']
 assert c.post('/api/research/setup').status_code==200
 resumed=c.post(url+'/handoffs/'+r.json()['handoff_id']+'/resume');assert resumed.status_code==202
 assert c.get(url).json()['tasks'][0]['started']


def test_stop_and_uncertain_submit_resume_after_process_restart(research,tmp_path):
 app,c,f=setup(research);w=make(research)
 f.timeout_turn=True
 run=send(research,w)
 with Session(app.state.engine) as s:
  first=s.get(ResearchRun,run);session_id=first.session_id;assert first.status=='uncertain'
 # Replace runtime object (the same durable SQLite data and fake remote receipts).
 from news2douyin.research.runtime import ResearchRuntime
 restored=ResearchRuntime(app.state.engine,app.state.research_settings,client_factory=lambda cfg:f)
 restored.sync(run)
 with Session(app.state.engine) as s:
  assert s.get(ResearchRun,run).session_id==session_id
  assert s.get(ResearchRun,run).status=='running'
 assert len(f.sessions)==1
 assert c.post('/api/topics/'+w['workspace_id']+'/stop').status_code==200
 with Session(app.state.engine) as s:assert s.get(ResearchRun,run).status=='paused'
 assert rpc(research,'topic_read_article',dict(turn_id=run,article_key='seed'))['isError']


def test_library_selection_does_not_require_exa_in_local_mode(research):
 app,c,f=setup(research);f.servers.pop('exa',None);w=make(research);run=send(research,w)
 with Session(app.state.engine) as s:assert s.get(ResearchRun,run).status=='running'
 assert tool(research,'topic_search_library',run,query='policy')['total']==1


def test_daily_state_is_not_rewritten_and_relative_bounds_are_fixed(research):
 app,c,f=research
 before=c.get('/api/daily/selections',params={'day':'2020-01-01','timezone':'Asia/Shanghai'}).json()
 w=make(research,{'period':'last7'})
 assert w['scope']['period']=='custom' and w['scope']['date_from'] and w['scope']['date_to']
 assert c.get('/api/daily/selections',params={'day':'2020-01-01','timezone':'Asia/Shanghai'}).json()==before
