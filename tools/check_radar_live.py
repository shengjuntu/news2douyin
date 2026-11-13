"""Real browser/FalkorDB validation using labelled synthetic news and grounded fixtures."""
import json,os,sys,tempfile,threading,subprocess,time,sqlite3
from pathlib import Path
import uvicorn,requests
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'tests'))
from test_content_foundation import app_at,article,process,drain_graph,report_fixture
from check_workbench_live import port
from sqlmodel import Session,select
from sqlalchemy import delete
from news2douyin.storage.models import Article,ArticleEventLink,GdeltDiscovery
from news2douyin.storage.articles import latest_version
from news2douyin.extraction.models import TextMention,VersionReading
from news2douyin.extraction.schema import Candidate,Argument,passages,grounded,cluster_key
from news2douyin.desk.models import DeskDirty,GkgSource
from news2douyin.desk.service import save_watch
from news2douyin.desk.rules import Scope,PRESETS


def seed(app):
    examples=[('a','Donald Trump signed the treaty in Paris yesterday.','signing',None,None),
        ('b','Donald Trump signed the treaty in Paris yesterday.','signing',None,None),
        ('c','Germany met Poland in Paris yesterday.','meeting','Germany','Poland'),
        ('d','Poland met France in Paris yesterday.','meeting','Poland','France'),
        ('e','France met United Kingdom in Paris yesterday.','meeting','France','United Kingdom'),
        ('f','United States met Germany in Paris yesterday.','meeting','United States','Germany'),
        ('g','China met France in Paris yesterday.','meeting','China','France'),
        ('h','Germany attacked Poland in Paris yesterday.','attack','Germany','Poland'),
        ('i','France attacked Germany in Paris yesterday.','attack','France','Germany')]
    for key,body,kind,actor,target in examples:article(app,key,body,'演示材料（虚构）：'+body.split(' in ')[0])
    process(app)
    with Session(app.state.engine) as s:
        for index,(key,body,kind,actor,target) in enumerate(examples):
            art=s.exec(select(Article).where(Article.article_key==key)).one();art.provider=['worldnewsapi','gkg','gdelt'][index%3];s.add(art)
            if art.provider=='gkg':s.add(GkgSource(article_key=key,record_id='QA-'+key,attributes=json.dumps({'mention_iso':['US','CN']})))
            if art.provider in ('gkg','gdelt'):s.add(GdeltDiscovery(article_key=key,seen_at='2026-11-03T10:00:00Z'))
            if actor:
                v=latest_version(s,key);doc=json.loads(v.payload_json);blocks=passages(doc);block=next(b for b in blocks.values() if b.field=='content')
                row=grounded(Candidate(block=block.id,quote=body,trigger='met' if kind=='meeting' else 'attacked',kind=kind,status='reported',time='yesterday',arguments=[Argument(role='actor',text=actor,entity_type='organization'),Argument(role='participant' if kind=='meeting' else 'target',text=target,entity_type='organization')]),blocks,key,v.revision,art.published_at)
                s.exec(delete(TextMention).where(TextMention.version_id==v.id));s.add(TextMention(id=row['id'],version_id=v.id,article=key,revision=v.revision,action_date=row['date'],signature=cluster_key(row),data=json.dumps(row)))
                job=s.get(VersionReading,v.id);job.method='fixture';job.state='ready';s.add(job)
            s.merge(DeskDirty(key='e_'+key))
        # One event has both a WorldNewsAPI article and a GKG article.
        s.add(ArticleEventLink(event_key='e_a',article_key='b'));s.commit()
    process(app)


def main():
    os.environ['NEWS2DOUYIN_GKG_STREAM_ENABLED']='0'
    import redislite
    from redis import Redis
    out=Path(sys.argv[1]).resolve();out.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='n2d-radar-') as folder:
        dbdir=Path(folder);gp,hp=port(),port();binary=Path(redislite.__file__).parent/'bin'
        with (out/'falkor.log').open('w') as log:
            proc=subprocess.Popen([str(binary/'redis-server'),'--loadmodule',str(binary/'falkordb.so'),'--port',str(gp),'--bind','127.0.0.1','--dir',folder,'--save','','--appendonly','no'],stdout=log,stderr=subprocess.STDOUT)
            server=None
            try:
                client=Redis(port=gp,socket_connect_timeout=.2)
                for _ in range(100):
                    try:
                        if client.ping():break
                    except Exception:time.sleep(.05)
                os.environ.update(NEWS2DOUYIN_FALKOR_URL=f'redis://127.0.0.1:{gp}',NEWS2DOUYIN_GRAPH_PREFIX='radar_qa',NEWS2DOUYIN_EVENT_READER='rules')
                app=app_at(dbdir);seed(app)
                with Session(app.state.engine) as s:focus=save_watch(s,Scope(**PRESETS[0])).key
                drain_graph(app)
                @app.get('/qa/focus')
                def get_focus():return {'key':focus}
                @app.post('/qa/complete-research/{key}')
                def seed_report(key:str):report_fixture(app,key);return {'fixture':True}
                server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=hp,log_level='warning'))
                thread=threading.Thread(target=server.run,daemon=True);thread.start()
                for _ in range(100):
                    try:
                        if requests.get(f'http://127.0.0.1:{hp}/api/health',timeout=.3).ok:break
                    except Exception:time.sleep(.05)
                env=dict(os.environ,N2D_QA_URL=f'http://127.0.0.1:{hp}',N2D_QA_OUTPUT=str(out))
                result=subprocess.run(['node',str(ROOT/'tools/check_radar_browser.cjs')],env=env,timeout=180)
                if result.returncode:raise SystemExit(result.returncode)
                with sqlite3.connect(dbdir/'news.db') as c:assert c.execute('PRAGMA integrity_check').fetchall()==[('ok',)]
                print('RADAR_BROWSER_REAL_FALKOR_PASS; synthetic extraction fixtures; SQLite integrity ok')
            finally:
                if server:server.should_exit=True;thread.join(timeout=30)
                proc.terminate()
                try:proc.wait(timeout=5)
                except subprocess.TimeoutExpired:proc.kill();proc.wait()


if __name__=='__main__':main()
