import json
import os
import socket
import subprocess
import time
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from news2douyin.server.app import create_app
from news2douyin.storage.models import Article, ArticleVersion, ArticleEventLink, Event, ScriptRevision
from news2douyin.storage.articles import record_version
from news2douyin.extraction import service as extraction
from news2douyin.desk import service as desk
from news2douyin.desk.models import WatchEvent, GraphPending
from news2douyin.knowledge import service as knowledge
from news2douyin.knowledge.models import GraphMetric, GraphRelation
from news2douyin.workbench import service as work
from news2douyin.workbench.models import ContentBinding
from news2douyin.research.models import ResearchCase,ResearchSource,ResearchClaim,ResearchReport


def app_at(path):
    return create_app(db_url='sqlite:///'+str(path/'news.db'),storage_root=str(path/'runs'))


def article(app,key,body,title=None,day='2026-11-03T10:00:00Z'):
    with Session(app.state.engine) as s:
        a=s.exec(select(Article).where(Article.article_key==key)).first()
        if not a:
            a=Article(article_key=key,title=title or key,content=body,published_at=day,source_domain=key+'.example')
            s.add(Event(event_key='e_'+key,event_title=title or key))
            s.add(ArticleEventLink(article_key=key,event_key='e_'+key))
        a.content=body;s.add(a);record_version(s,a);s.commit()


def process(app):
    while extraction.step(app.state.engine):pass
    while desk.drain(app.state.engine):pass


def report_fixture(app,task):
    with Session(app.state.engine) as s:
        c=ResearchCase(case_id='case_fixture',title='Aid research',goal='History',strategy_json='{}',cutoff='2026-11-04T00:00:00Z')
        s.add(c)
        source=ResearchSource(source_id='source_1',case_id=c.case_id,title='Statement',url='https://one.example/a',kind='article',content_hash='immutable',
            paragraphs_json=json.dumps([dict(id='P1',text='Donald Trump signed the treaty in Paris yesterday.')]))
        s.add(source)
        ev=[dict(source_id=source.source_id,paragraph_id='P1',quote='signed the treaty',relation='supports')]
        s.add(ResearchClaim(claim_id='claim_1',case_id=c.case_id,run_id='run_fixture',text='据这份报道，特朗普签署了条约。',kind='attributed',evidence_json=json.dumps(ev)))
        r=ResearchReport(report_id='report_fixture',case_id=c.case_id,run_id='run_fixture',version=1,title='Policy history',
            sections_json=json.dumps([dict(heading='Change',body='An attributed action.',claim_ids=['claim_1'])]),
            gaps_json='[]',completeness='complete',cutoff=c.cutoff,content_hash='report-hash')
        s.add(r);work.bind(s,task,'case',c.case_id);s.commit()


def test_event_task_report_to_script_and_provenance(tmp_path):
    app=app_at(tmp_path);c=TestClient(app)
    article(app,'a','Donald Trump signed the treaty in Paris yesterday.','Treaty signing')
    process(app)
    body=dict(request_id='content-request-1',event='e_a',angle='Explain the historical context')
    created=c.post('/api/workbench',json=body)
    assert created.status_code==201,created.text
    task=created.json();key=task['key']
    assert len(task['sources'])==1 and task['event']=='e_a'
    assert c.post('/api/workbench',json=body).json()['key']==key
    assert c.post('/api/workbench',json=dict(body,angle='Different')).status_code==409
    for stage in ('selection','research','creation','works'):
        page=c.get('/workbench/'+key+'/'+stage)
        assert page.status_code==200,page.text
        assert '内容任务阶段' in page.text and 'Explain the historical context' in page.text
    assert '形成选题' in c.get('/events/e_a').text
    assert 'Treaty signing' in c.get('/workbench').text
    assert c.get('/topics',follow_redirects=False).headers['location']=='/workbench'
    report_fixture(app,key)
    request=dict(report_id='report_fixture',request_id='draft-request-1',version=1)
    made=c.post('/api/workbench/'+key+'/from-report',json=request)
    assert made.status_code==200,made.text
    assert c.post('/api/workbench/'+key+'/from-report',json=request).json()==made.json()
    assert c.get(made.json()['url']).status_code==200
    with Session(app.state.engine) as s:
        revision=s.exec(select(ScriptRevision).where(ScriptRevision.package_key==made.json()['package'])).one()
        saved=json.loads(revision.payload_json)
        assert saved['editorial']['research']['report_id']=='report_fixture'
        assert saved['sources'][0]['paragraphs'][0]['text'].endswith('yesterday.')
    article(app,'a','Correction: Donald Trump did not sign the treaty in Paris yesterday.')
    unchanged=c.get('/api/topics/'+key).json()['sources'][0]
    assert unchanged['revision']==1
    assert c.post('/api/workbench/'+key+'/phase',json={'phase':'works','version':1}).status_code==409
    assert c.post('/api/workbench/'+key+'/from-report',json=request,headers={'Origin':'https://evil.example'}).status_code==403
    other=c.post('/api/workbench',json={'request_id':'content-request-2','title':'Other'}).json()['key']
    assert c.post('/api/workbench/'+other+'/from-report',json=request).status_code==422
    assert c.get('/scripts/'+made.json()['package'],follow_redirects=False).headers['location']==made.json()['url']


@pytest.fixture
def live_graph(tmp_path,monkeypatch):
    redislite=pytest.importorskip('redislite')
    from redis import Redis
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
    binary=Path(redislite.__file__).parent/'bin'
    log=(tmp_path/'falkor.log').open('w')
    proc=subprocess.Popen([str(binary/'redis-server'),'--loadmodule',str(binary/'falkordb.so'),
        '--port',str(port),'--bind','127.0.0.1','--dir',str(tmp_path),'--save','','--appendonly','no'],stdout=log,stderr=subprocess.STDOUT)
    client=Redis(host='127.0.0.1',port=port,decode_responses=True,socket_connect_timeout=.2,socket_timeout=4)
    try:
        for _ in range(80):
            try:
                if client.ping():break
            except Exception:time.sleep(.05)
        else:pytest.fail((tmp_path/'falkor.log').read_text())
        monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL',f'redis://127.0.0.1:{port}')
        monkeypatch.setenv('NEWS2DOUYIN_GRAPH_PREFIX','test_live')
        yield client
    finally:
        proc.terminate()
        try:proc.wait(timeout=5)
        except subprocess.TimeoutExpired:proc.kill();proc.wait()
        log.close()


def drain_graph(app):
    for _ in range(2000):
        if app.state.desk.graph.step(app.state.engine):continue
        if getattr(app.state.desk.graph,'work_state','')=='indexes':
            time.sleep(.02)
            continue
        if not knowledge.refresh(app.state.engine,app.state.desk.graph):break
    else:raise AssertionError('graph queue did not settle')


def test_real_graph_analysis_correction_focus_and_loss_recovery(tmp_path,live_graph):
    app=app_at(tmp_path);c=TestClient(app)
    watch=c.post('/api/focus',json=dict(name='特朗普活动',kind='人物',scope='特朗普',question='What changed?')).json()['key']
    article(app,'a','Donald Trump signed the treaty in Paris yesterday.')
    article(app,'b','Donald Trump signed the treaty in Paris yesterday.')
    article(app,'c','Donald Trump did not sign the treaty in Paris yesterday.')
    article(app,'d','Alice visited London yesterday.')
    process(app);drain_graph(app)
    with Session(app.state.engine) as s:
        assert len(s.exec(select(GraphMetric)).all())==4
        edge=s.exec(select(GraphRelation).where(GraphRelation.source=='e_a',GraphRelation.target=='e_b')).one()
        assert any(e['kind']=='same_action_candidate' for e in json.loads(edge.evidence))
        denied=json.loads(s.get(GraphMetric,'e_c').data)
        assert denied['impact']==0 and denied['statuses']==['denied']
        assert s.get(WatchEvent,(watch,'e_d')) is None
    view=c.get('/api/knowledge/graph').json()
    assert view['state']['state']=='ready' and len(view['nodes'])==4
    assert '来源去重组' in c.get('/radar').text
    assert c.get('/focus/'+watch).status_code==200
    # Changing an article removes old statements and relation edges from current graphs.
    article(app,'b','Alice visited London yesterday.');process(app);drain_graph(app)
    with Session(app.state.engine) as s:
        assert s.exec(select(GraphRelation).where(GraphRelation.source=='e_a',GraphRelation.target=='e_b')).first() is None
    assert c.post('/api/focus/'+watch+'/links',json={'event':'e_c','action':'exclude'}).status_code==200
    drain_graph(app)
    assert not app.state.desk.graph.read('focus_'+watch,'MATCH (e:Event {id:$id}) RETURN e.id',id='e_c')
    live_graph.execute_command('GRAPH.DELETE','test_live_active_global')
    app.state.desk.graph.bootstrap(app.state.engine)
    drain_graph(app)
    assert len(app.state.desk.graph.read('active_global','MATCH (e:Event) RETURN e.id'))==4
    assert c.get('/api/knowledge/status').json()['state']=='ready'
    live_graph.execute_command('GRAPH.DELETE','test_live_focus_'+watch)
    app.state.desk.graph.bootstrap(app.state.engine)
    assert c.get('/api/knowledge/status').json()['state']=='pending'
    drain_graph(app)
    assert app.state.desk.graph.read('focus_'+watch,'MATCH (q:Question {id:$id}) RETURN q.id',id=watch)==[[watch]]
    other_dir=tmp_path/'another';other_dir.mkdir()
    other=app_at(other_dir)
    with pytest.raises(ValueError,match='another_database'):
        other.state.desk.graph.bootstrap(other.state.engine)
    assert len(app.state.desk.graph.read('active_global','MATCH (e:Event) RETURN e.id'))==4


def test_graph_algorithms_reposts_and_propagation():
    from news2douyin.knowledge.algorithms import pagerank,analyze
    rank=pagerank(['a','b','c'],[('a','b',2),('b','c',1)])
    assert abs(sum(rank.values())-1)<1e-8 and rank['b']>rank['c']
    rows=[['a',f'm{i}','signing','reported','2026-11-03','same',f'article{i}','wire1','domain'+str(i)] for i in range(8)]
    metrics,_,_=analyze([['a','x','2026-11-03T00:00:00Z']],rows,[])
    assert metrics['a']['source_groups']==1 and metrics['a']['novelty']==1


def test_model_backfill_transient_retry_and_evidence_retention(tmp_path,monkeypatch):
    from news2douyin.extraction.models import VersionReading,TextMention
    app=app_at(tmp_path)
    article(app,'a','Donald Trump signed the treaty in Paris yesterday.')
    process(app)
    with Session(app.state.engine) as s:
        baseline=s.exec(select(TextMention.id)).all()
        assert baseline
    monkeypatch.setenv('NEWS2DOUYIN_EVENT_READER','model')
    extraction.initialize(app.state.engine)
    with Session(app.state.engine) as s:
        assert s.exec(select(VersionReading)).one().state=='model_queued'
    def unavailable(*args):raise TimeoutError('a private endpoint')
    monkeypatch.setattr(extraction.reader,'extract',unavailable)
    assert extraction.step(app.state.engine,True)
    with Session(app.state.engine) as s:
        job=s.exec(select(VersionReading)).one()
        assert job.state=='model_wait' and job.until>time.time()
        assert 'private' not in job.note
        assert s.exec(select(TextMention.id)).all()==baseline
        job.until=0;s.add(job);s.commit()
    monkeypatch.setattr(extraction.reader,'extract',lambda *args:([],0))
    assert extraction.step(app.state.engine,True)
    extraction.initialize(app.state.engine)
    with Session(app.state.engine) as s:
        assert s.exec(select(VersionReading)).one().state=='ready'
        assert s.exec(select(TextMention.id)).all()==baseline


def test_persistent_focus_prioritizes_uncovered_question_once_per_day(tmp_path,monkeypatch):
    from news2douyin.desk.models import Watch,WatchReview
    from news2douyin.extraction.models import FocusPoint,PointReview
    from news2douyin.knowledge.models import PointMaterial
    from news2douyin.storage.models import utc_now_iso
    app=app_at(tmp_path);c=TestClient(app)
    key=c.post('/api/focus',json=dict(name='研究议题',kind='长期事件',scope='treaty',question='How did this develop?')).json()['key']
    now=utc_now_iso()
    with Session(app.state.engine) as s:
        w=s.get(Watch,key);w.auto_research=True;w.next_review='2000-01-01T00:00:00Z';s.add(w)
        for p in ('covered','gap'):
            s.add(FocusPoint(key=p,watch=key,parent=key,text=p+' history?',created=now))
        s.add(PointMaterial(key='material',point='covered',event='old_event',reason='hint',generation='test'))
        s.commit()
    dispatched=[]
    monkeypatch.setattr(app.state.desk,'dispatch',lambda ticket:dispatched.append(ticket))
    app.state.desk.review_step()
    with Session(app.state.engine) as s:
        ticket=s.exec(select(WatchReview)).one()
        assert ticket.question=='gap history?' and ticket.event==''
        assert s.get(PointReview,ticket.key).point=='gap'
        assert s.get(Watch,key).next_review>now
        ticket.status='submitted';s.add(ticket)
        w=s.get(Watch,key);w.next_review='2000-01-01T00:00:00Z';s.add(w);s.commit()
    app.state.desk.review_step()
    with Session(app.state.engine) as s:
        assert len(s.exec(select(WatchReview)).all())==1
    assert len(dispatched)==1
