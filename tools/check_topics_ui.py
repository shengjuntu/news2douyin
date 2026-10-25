"""Exercise the shipped UI against a real local ASGI/SQLite server.
RunDesk/model is a contract fixture. Install jsdom separately for this check.
"""
import json,os,re,socket,subprocess,sys,tempfile,threading,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
import requests,uvicorn
from fastapi.testclient import TestClient
from sqlmodel import Session,select
from test_research import FakeRunDesk
from news2douyin.server.app import create_app
from news2douyin.storage.models import Article
from news2douyin.topics.models import TopicMessage
from news2douyin.research.models import ResearchRun

def main():
 with tempfile.TemporaryDirectory() as temp:
  app=create_app(db_url='sqlite:///'+temp+'/app.db',storage_root=temp+'/runs')
  config=app.state.research_settings.get();config.update(runtime_mode='local',search_provider='rundesk');app.state.research_settings.write(config)
  fake=FakeRunDesk();app.state.research.client_factory=lambda config:fake
  client=TestClient(app);assert client.post('/api/research/setup').status_code==200
  with Session(app.state.engine) as s:
   s.add(Article(article_key='ui-seed',title='Example historical policy',content='The old policy was introduced in 2023. The new policy was announced in 2026.',published_at='2023-01-01T00:00:00Z'));s.commit()
  def tool(name,run,**kwargs):
   response=client.post('/mcp/research',headers={'Authorization':'Bearer '+config['mcp_token']},json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':{'turn_id':run,**kwargs}}}).json()['result']
   assert not response['isError'],response
   return json.loads(response['content'][0]['text'])
  def model():
   # Only complete editorial turns. Handoffs remain genuine queued research tasks.
   with Session(app.state.engine) as s:
    latest=s.exec(select(TopicMessage).join(ResearchRun,ResearchRun.run_id==TopicMessage.run_id).where(ResearchRun.status=='starting').order_by(TopicMessage.created_at.desc())).first()
   if not latest:return
   context=tool('topic_get_workspace',latest.run_id)['context']
   tool('topic_search_library',latest.run_id,query='policy',period='all')
   source=tool('topic_read_article',latest.run_id,**({'source_id':context['sources'][0]['source_id']} if context['sources'] else {'article_key':'ui-seed'}))
   tool('topic_save_candidates',latest.run_id,candidates=[dict(title='政策变化选题',angle='面向普通观众比较前后变化',summary='围绕历史政策形成短视频选题，实际影响需核实。',source_ids=[source['source_id']])])
   tool('topic_save_reply',latest.run_id,text='已阅读历史报道，可以从政策前后变化切入。',source_ids=[source['source_id']])
   # Simulate a later library update; the already-selected snapshot stays fixed.
   with Session(app.state.engine) as s:
    a=s.exec(select(Article).where(Article.article_key=='ui-seed')).one();a.content='UPDATED LIBRARY COPY';s.add(a);s.commit()
   for session in fake.sessions.values():session['status']='completed'
  fake.on_turn=model
  sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
  server=uvicorn.Server(uvicorn.Config(app,log_level='error'));thread=threading.Thread(target=lambda:server.run(sockets=[sock]),daemon=True);thread.start()
  try:
   base='http://127.0.0.1:'+str(port)
   for _ in range(100):
    try:
     if requests.get(base+'/api/health',timeout=1).ok:break
    except requests.RequestException:pass
    time.sleep(.05)
   subprocess.run(['node',str(ROOT/'tools/topics-ui.cjs'),base,str(ROOT/'src/news2douyin/server/static/topics.js')],check=True,env=os.environ)
  finally:server.should_exit=True;thread.join(8);sock.close()
if __name__=='__main__':main()
