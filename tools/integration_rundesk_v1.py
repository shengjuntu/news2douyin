"""Actual RunDesk 0.6.1 demo-protocol integration, never a live-model test."""
import argparse,json,os,socket,subprocess,sys,tempfile,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
import requests

def port():
 with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
def wait(url):
 for _ in range(100):
  try:
   if requests.get(url,timeout=.5).status_code<500:return
  except requests.RequestException:pass
  time.sleep(.1)
 raise AssertionError('server did not start')

def main():
 p=argparse.ArgumentParser();p.add_argument('--rundesk',required=True);p.add_argument('--news',required=True);p.add_argument('--video',required=True);p.add_argument('--output',required=True);args=p.parse_args()
 out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=True)
 sys.path.insert(0,str(Path(args.news).resolve()/'src'))
 from news2douyin.server.app import create_app
 from news2douyin.research.rundesk import RunDeskClient,bootstrap
 from news2douyin.research.runtime import ResearchRuntime
 from news2douyin.research import service
 from news2douyin.research.models import ResearchRun,ResearchSubmission
 from news2douyin.storage.models import Article
 from sqlmodel import Session
 checks=[];processes=[]
 with tempfile.TemporaryDirectory(prefix='rundesk-adapters-') as td:
  root=Path(td);rdurl='http://127.0.0.1:'+str(port());env=dict(os.environ);env.pop('RUNDESK_TOKEN',None);env.pop('CODEX_BASE_TOKEN',None)
  rd=subprocess.Popen([args.rundesk,'--demo','--data',str(root/'rundesk'),'--listen',rdurl.split('//')[1]],stdout=(out/'rundesk.log').open('w'),stderr=subprocess.STDOUT,env=env);processes.append(rd)
  wait(rdurl+'/api/v1/meta');assert requests.get(rdurl+'/api/v1/meta').json()['demo']
  drops=set();calls=[]
  class Proxy(BaseHTTPRequestHandler):
   def log_message(self,*a):pass
   def forward(self):
    body=self.rfile.read(int(self.headers.get('Content-Length','0')))
    headers={k:v for k,v in self.headers.items() if k.lower() not in ('host','content-length','connection')}
    response=requests.request(self.command,rdurl+self.path,data=body,headers=headers,timeout=25)
    calls.append((self.command,self.path,self.headers.get('Idempotency-Key'),body.decode()))
    if self.command=='POST' and self.path in drops:
     drops.remove(self.path);self.close_connection=True;return
    self.send_response(response.status_code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(response.content)));self.end_headers();self.wfile.write(response.content)
   do_GET=do_POST=do_PUT=forward
  proxy=ThreadingHTTPServer(('127.0.0.1',0),Proxy);threading.Thread(target=proxy.serve_forever,daemon=True).start();url='http://127.0.0.1:'+str(proxy.server_port)
  try:
   app=create_app(db_url='sqlite:///'+str(root/'news.db'),storage_root=str(root/'news'))
   cfg=app.state.research_settings.get();cfg['rundesk_url']=url;app.state.research_settings.write(cfg)
   class DemoFixtureClient(RunDeskClient):
    # ONLY the test bypasses the production demo guard to exercise wire formats.
    def request(self,method,path,*a,**kw):
     value=super().request(method,path,*a,**kw)
     if path=='/meta':value['demo']=False
     return value
   bootstrap(app.state.research_settings,DemoFixtureClient)
   with Session(app.state.engine) as s:s.add(Article(article_key='integration',title='保留的旧新闻',content='只用于协议测试',url='https://example.com/source'));s.commit()
   case=service.create_case(app.state.engine,{'article_keys':['integration'],'goal':'审批，测试背景研究','strategy_id':'background'})
   runtime=ResearchRuntime(app.state.engine,app.state.research_settings,DemoFixtureClient)
   run=runtime.enqueue(case['case_id'],'审批：仅验证 API');rid=run['run_id']
   drops.add('/api/v1/sessions');runtime.dispatch(rid)
   with Session(app.state.engine) as s:
    row=s.get(ResearchRun,rid);assert row.status=='uncertain' and not row.session_id
    op=s.get(ResearchSubmission,rid);receipt=requests.get(rdurl+'/api/v1/requests/'+op.session_key).json();sid=receipt['response']['id']
   drops.add('/api/v1/sessions/'+sid+'/turns')
   runtime=ResearchRuntime(app.state.engine,app.state.research_settings,DemoFixtureClient);runtime.dispatch(rid)
   with Session(app.state.engine) as s:assert s.get(ResearchRun,rid).status=='uncertain'
   runtime=ResearchRuntime(app.state.engine,app.state.research_settings,DemoFixtureClient);runtime.sync(rid)
   with Session(app.state.engine) as s:
    op=s.get(ResearchSubmission,rid);current=s.get(ResearchRun,rid);assert op.rundesk_run_id,(current.status,current.error,requests.get(rdurl+'/api/v1/requests/'+op.turn_key).json())
   assert sum(m=='POST' and k==op.session_key for m,p,k,b in calls)==1
   assert sum(m=='POST' and k==op.turn_key for m,p,k,b in calls)==1
   sessions=requests.get(rdurl+'/api/v1/sessions',params={'appId':'news2douyin','taskId':rid}).json();assert len(sessions)==1 and sessions[0]['id']==sid
   checks+=['news_lost_session_ack_restart','news_lost_turn_ack_restart','news_application_source']
   # Replace the run in RunDesk; the app must not stop the replacement.
   response=requests.post(rdurl+'/api/v1/sessions/'+sid+'/stop',json={'expectedRunId':op.rundesk_run_id});response.raise_for_status()
   for _ in range(100):
    state=requests.get(rdurl+'/api/v1/sessions/'+sid).json()
    if state['status'] not in ['starting','running','waiting','stopping']:break
    time.sleep(.05)
   response=requests.post(rdurl+'/api/v1/sessions/'+sid+'/turns',headers={'Idempotency-Key':'integration-replacement'},json={'text':'审批：别的任务','files':[],'skills':[]});response.raise_for_status();replacement=response.json()['runId']
   assert runtime.cancel(case['case_id'])['status']=='paused'
   current=requests.get(rdurl+'/api/v1/sessions/'+sid).json();assert current['runId']==replacement and current['status'] in ['starting','running','waiting']
   checks.append('news_stale_stop_preserves_replacement')
   # Existing database data survives the additive table creation / reload.
   app.state.engine.dispose();reopened=create_app(db_url='sqlite:///'+str(root/'news.db'),storage_root=str(root/'news'))
   with Session(reopened.state.engine) as s:assert s.get(ResearchSubmission,rid).rundesk_run_id==op.rundesk_run_id
   reopened.state.engine.dispose();checks.append('news_database_restart_keeps_receipts')
   appurl='http://127.0.0.1:'+str(port());data=root/'video'
   def startvideo():
    proc=subprocess.Popen([args.video,'--data',str(data),'--listen',appurl.split('//')[1],'--rundesk',url],stdout=(out/'video.log').open('a'),stderr=subprocess.STDOUT);processes.append(proc);wait(appurl+'/api/environment');return proc
   video=startvideo();token=(data/'access-token').read_text().strip();h={'Authorization':'Bearer '+token}
   def call(method,path,body=None):
    r=requests.request(method,appurl+path,headers=h,json=body,timeout=30);assert r.ok,(r.status_code,r.text);return r.json()
   project=call('POST','/api/projects',{'name':'API 升级验证'});pid=project['id'];base='/api/projects/'+pid
   connected=call('POST',base+'/connect',{});vsid=connected['sessionId']
   config=call('GET',base+'/agent/configuration');assert any(sk['name']=='video-project' and sk['sourceScope']=='instance' for sk in config['skills'])
   state=requests.get(rdurl+'/api/v1/sessions/'+vsid).json();assert state['source']['appId']=='rundesk-video-app' and state['source']['taskId']==pid
   checks+=['video_instance_skill_configuration','video_application_source']
   drops.add('/api/v1/sessions/'+vsid+'/turns');message={'text':'审批：验证丢失响应恢复','requestId':'integration-video-message'}
   response=requests.post(appurl+base+'/messages',headers=h,json=message,timeout=30);assert response.status_code==502,response.text
   video.terminate();video.wait(timeout=5);video=startvideo()
   result=call('POST',base+'/messages',message);assert result['runId']
   assert sum(m=='POST' and p=='/api/v1/sessions/'+vsid+'/turns' for m,p,k,b in calls)==1
   call('POST',base+'/agent/stop',{'expectedRunId':result['runId']})
   checks.append('video_lost_turn_ack_process_restart')
   if os.getenv('BROWSER_BIN'):
    js=Path(__file__).with_name('video-browser.cjs')
    env=dict(os.environ,VIDEO_APP_TEST_TOKEN=token)
    browser=subprocess.run([os.environ['CODEX_PRIMARY_RUNTIME_NODE'],str(js),appurl,str(out)],capture_output=True,text=True,env=env,timeout=120)
    (out/'browser.log').write_text(browser.stdout+browser.stderr)
    assert browser.returncode==0,browser.stdout+browser.stderr
    checks+=json.loads(browser.stdout.strip().splitlines()[-1])['checks']
   result={'mode':'RunDesk 0.6.1 --demo; no live model or real search','productionDemoGuardUnchanged':True,'passed':len(checks),'checks':checks}
   (out/'integration.json').write_text(json.dumps(result,ensure_ascii=False,indent=2));print(json.dumps(result,ensure_ascii=False))
  finally:
   proxy.shutdown()
   for process in reversed(processes):
    if process.poll() is None:process.terminate()
    try:process.wait(timeout=5)
    except subprocess.TimeoutExpired:process.kill();process.wait()
if __name__=='__main__':main()
