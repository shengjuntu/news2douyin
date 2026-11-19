import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
from sqlmodel import Session, select
from news2douyin.desk import service as desk
from news2douyin.desk.models import WatchReview, WatchEvent
from news2douyin.desk.rules import PRESETS
from news2douyin.extraction import reader, service
from news2douyin.extraction.models import VersionReading
from news2douyin.extraction.schema import normalize_date
from news2douyin.server.app import create_app
from news2douyin.storage.articles import record_version
from news2douyin.storage.models import Article, Event, ArticleEventLink, ArticleVersion


def text_rows(body,published='2025-11-03T09:00:00Z'):
    version=SimpleNamespace(article_key='A',revision=1)
    return reader.extract(version,{'content':body,'published_at':published},'rules')[0]


def new_app(path):
    return create_app(db_url='sqlite:///'+str(path/'base.db'),storage_root=str(path/'runs'))


def write_article(app,key,title,body,published='2025-11-03T00:00:00Z'):
    with Session(app.state.engine) as s:
        art=s.exec(select(Article).where(Article.article_key==key)).first()
        if not art:
            art=Article(article_key=key,title=title,content=body,published_at=published)
            s.add(Event(event_key='e_'+key,event_title=title))
            s.add(ArticleEventLink(article_key=key,event_key='e_'+key))
        art.title, art.content=title,body
        s.add(art)
        record_version(s,art)
        s.commit()


def process(app):
    while service.step(app.state.engine):
        pass
    while desk.drain(app.state.engine):
        pass


def test_split_roles_dates_stance_and_scope():
    body='11月3日，甲国外长在巴黎表示，双方计划下月在华沙会谈。'
    a,b=text_rows(body)
    assert (a['kind'],a['status'],a['date'])==('statement','reported','2025-11-03')
    assert {(v['role'],v['text']) for v in a['arguments']}=={('speaker','甲国外长'),('location','巴黎')}
    assert (b['kind'],b['status'],b['date'])==('meeting','planned','2025-12')
    assert {(v['role'],v['text']) for v in b['arguments']}=={('source','甲国外长'),('location','华沙')}
    assert all(body[m['start']:m['end']]==m['quote'] for m in (a,b))
    assert text_rows('Trump did not sign the treaty yesterday.')[0]['status']=='denied'
    assert text_rows('If Alice signs the deal tomorrow.')[0]['status']=='conditional'
    assert text_rows('巴黎警方表示特朗普没有出席。')[0]['status']=='reported'
    assert not text_rows('法国驻波兰记者。')
    assert text_rows('Donald Trump will meet Alice next month.','')[0]['date']==''
    assert normalize_date('下月','2025-12-31T20:00:00Z')=='2026-01'
    assert normalize_date('2月30日','2025-02-02')==''


def test_backfill_revision_correction_exclusion_and_person_focus(tmp_path):
    app=new_app(tmp_path)
    c=TestClient(app)
    body='Donald Trump signed the treaty in Paris yesterday.'
    write_article(app,'a','Diplomatic update',body)
    write_article(app,'b','Second outlet',body)
    write_article(app,'c','Third outlet','Donald Trump did not sign the treaty in Paris yesterday.')
    process(app)
    person=c.post('/api/focus',json=PRESETS[0]).json()['key']
    from focus_worker_support import drain_focus
    drain_focus(app.state.engine)
    with Session(app.state.engine) as s:
        link=s.get(WatchEvent,(person,'e_a'))
        assert link.reason.startswith('角色命中') and '签署' in link.branch
    api='/api/desk/articles/a/1/mentions'
    data=c.get(api).json()
    m=data['items'][0]
    url='/api/desk/mentions/'+m['id']
    assert [p['article'] for p in c.get(url+'/candidates').json()['items']]==['b']
    assert c.post(url+'/review',json={'decision':'accepted'}).status_code==200
    assert c.post(url+'/review',headers={'origin':'https://foreign.example'},json={'decision':'rejected'}).status_code==403
    assert c.post(url+'/review',json={'decision':'rejected'}).status_code==200
    process(app)
    assert c.get(url+'/candidates').json()['items']==[]
    assert '报道中的事件陈述' in c.get('/articles/a?revision=1').text
    write_article(app,'a','Correction','Donald Trump will visit Warsaw tomorrow.')
    process(app)
    assert c.get(api).json()['items'][0]['decision']=='rejected'
    assert '拟议或未来' in c.get('/events/e_a').text
    assert 'HAS_ARGUMENT' in c.get('/api/desk/graph-schema').json()['graph']['shared_relationships']
    with Session(app.state.engine) as s:
        s.exec(__import__('sqlalchemy').text('delete from versionreading'))
        s.commit()
    reopened=new_app(tmp_path)
    process(reopened)
    assert TestClient(reopened).get('/api/desk/articles/a/2/mentions').json()['items'][0]['kind']=='visit'


def test_model_guard_retry_and_cross_paragraph(tmp_path,monkeypatch):
    app=new_app(tmp_path)
    c=TestClient(app)
    write_article(app,'a','Update','Alice is the named delegate.\nShe will visit Paris tomorrow.')
    process(app)
    base='/api/desk/articles/a/1'
    endpoint=lambda:c.get(base+'/mentions').json()
    original=endpoint()['items']
    good=dict(block='P3',quote='She will visit Paris tomorrow.',trigger='visit',kind='visit',status='planned',
        time='tomorrow',arguments=[dict(role='actor',text='Alice',block='P2',evidence='Alice is the named delegate.'),
                                  dict(role='location',text='Paris')])
    for bad in [dict(good,arguments=[dict(role='actor',text='Invented')]),dict(good,status='reported')]:
        monkeypatch.setattr(reader,'model_candidates',lambda _,payload=bad:[payload])
        c.post(base+'/model')
        assert service.step(app.state.engine,model=True)
        assert endpoint()['state']=='error' and endpoint()['items']==original
    monkeypatch.setattr(reader,'model_candidates',lambda _:[good])
    c.post(base+'/model')
    service.step(app.state.engine,model=True)
    result=endpoint()
    assert result['method']=='model' and result['items'][0]['arguments'][0]['text']=='Alice'
    assert not service.step(app.state.engine,model=True)


def test_outline_scoping_and_targeted_research(tmp_path):
    app=new_app(tmp_path)
    c=TestClient(app)
    w=c.post('/api/focus',json=PRESETS[1]).json()['key']
    other=c.post('/api/focus',json=PRESETS[2]).json()['key']
    url='/api/focus/'+w+'/outline'
    first=c.post(url,json={'text':'援助条件是什么？'}).json()['key']
    point=c.post(url,json={'parent':first,'text':'协议何时生效？'}).json()['key']
    assert c.post(url,json={'parent':first,'text':'协议何时生效？'}).json()['key']==point
    assert c.post('/api/focus/'+other+'/outline',json={'parent':first,'text':'x'}).status_code==422
    ticket=c.post(url+'/'+point+'/review').json()['ticket']
    assert c.post(url+'/'+point+'/review').json()['ticket']==ticket
    app.state.desk.review_step()
    with Session(app.state.engine) as s:
        record=s.get(WatchReview,ticket)
        assert record.question=='协议何时生效？'
        res=c.get('/api/research/cases/'+record.case_key).json()
        assert '援助条件是什么' in res['goal'] and res['goal'].startswith(record.question)
    assert '协议何时生效' in c.get('/focus/'+w).text


def test_survey_hybrid_missing_evidence_citations_and_one_hop(tmp_path):
    app=new_app(tmp_path)
    c=TestClient(app)
    write_article(app,'a','Budget evidence','Donald Trump signed the cobalt treaty in Paris yesterday.')
    write_article(app,'b','Independent witness','Donald Trump signed the accord in Paris yesterday.')
    write_article(app,'c','Correction','Donald Trump did not sign the accord in Paris yesterday.')
    process(app)
    watch=c.post('/api/focus',json=PRESETS[0]).json()['key']
    from focus_worker_support import drain_focus
    drain_focus(app.state.engine)
    url='/api/focus/'+watch+'/outline'
    c.post(url,json={'text':'cobalt'})
    c.post(url,json={'text':'海底隧道通车'})
    doc=c.get('/focus/'+watch+'/survey').text
    assert '一跳候选' in doc and '未找到可定位的直接材料' in doc and '材料分组建议' in doc
    assert '未找到可定位的直接材料' in c.get('/focus/'+watch+'/survey?day=2020-01-01').text
    ticket=c.post('/api/focus/'+watch+'/reviews',json={}).json()['key']
    app.state.desk.review_step()
    from news2douyin.extraction.models import SurveyPacket
    from news2douyin.research.models import ResearchSource
    with Session(app.state.engine) as s:
        case=s.get(WatchReview,ticket).case_key
        plan=json.loads(s.get(SurveyPacket,case).data)
        section=next(x for x in plan['sections'] if x['title']=='cobalt')
        assert {x['article'] for x in section['evidence']}=={'a','b'}
        assert len(plan['selected'])<=plan['budget'] and 'c' not in {x['article'] for x in section['evidence']}
        for x in section['evidence']:
            src=s.get(ResearchSource,x['source_id'])
            paragraphs=json.loads(src.paragraphs_json)
            assert any(p['id']==x['paragraph_id'] and x['quote'] in p['text'] for p in paragraphs)
        assert '<summary>本轮取证快照</summary>' in c.get('/research/'+case).text


def test_http_model_protocol_recovery_and_expired_lease(tmp_path,monkeypatch):
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    from threading import Thread
    app=new_app(tmp_path)
    c=TestClient(app)
    write_article(app,'z','Update','Alice signed the treaty.')
    process(app)
    api='/api/desk/articles/z/1'
    calls=[]
    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            payload=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append((self.path,payload))
            item=dict(block='P2',quote='Alice signed the treaty.',trigger='signed',kind='signing',
                      status='reported',arguments=[dict(role='actor',text='Alice',entity_type='person')])
            blob=json.dumps({'choices':[{'message':{'content':json.dumps({'items':[item]})}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Length',str(len(blob)))
            self.end_headers()
            self.wfile.write(blob)
    with ThreadingHTTPServer(('127.0.0.1',0),Endpoint) as server:
        thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        monkeypatch.setenv('OPENAI_BASE_URL','http://127.0.0.1:'+str(server.server_port)+'/v1')
        monkeypatch.setenv('MODEL','qa-fixture')
        try:
            assert c.post(api+'/model').status_code==200
            assert service.pick(app.state.engine,True)
            assert service.pick(app.state.engine,True) is None
            with Session(app.state.engine) as s:
                job=s.exec(select(VersionReading)).one()
                job.until=0;s.add(job);s.commit()
            assert service.step(app.state.engine,True)
            data=c.get(api+'/mentions').json()
            assert data['method']=='model' and data['model']=='qa-fixture'
            assert data['items'][0]['arguments'][0]['entity_type']=='person'
            assert calls[0][0]=='/v1/chat/completions'
        finally:
            server.shutdown();thread.join()
