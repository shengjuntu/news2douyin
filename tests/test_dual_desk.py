import io
from zipfile import ZipFile
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from news2douyin.candidates.promotion import promote
from news2douyin.candidates.service import candidate_token, list_reports
from news2douyin.collect import service as collect
from news2douyin.collect.providers import gkg
from news2douyin.desk import service as desk
from news2douyin.desk.models import Watch, WatchReview
from news2douyin.desk.rules import PRESETS
from news2douyin.server.app import create_app
from news2douyin.storage.models import ArticleVersion, Event
from focus_worker_support import drain_focus


STAMP='20250203194500'
PROFILE={'name':'gkg-fixture','provider':'gkg','search_query':'Ukraine','max_items':30,
         'date_str':'2025-02-03','timezone':'UTC','gkg_time':'19:45','filter_mode':'rules',
         'country':'','language':''}
def archive():
    output=io.BytesIO()
    lines=[]
    for n,domain in enumerate(('one.example','two.example')):
        cols=['']*27
        cols[:5]=[STAMP+'-'+str(n),STAMP,'1',domain,'https://'+domain+'/story']
        cols[7]='ECON_TRADE_DISPUTE;';cols[9]='1#Ukraine#UP#UP##';cols[11]='donald trump;'
        cols[26]='<PAGE_TITLE>Trump seeks Ukraine rare earth deal for military aid</PAGE_TITLE><PAGE_AUTHORS>Shared wire</PAGE_AUTHORS>'
        lines.append('\t'.join(cols))
    with ZipFile(output,'w') as z:z.writestr(STAMP+'.gkg.csv','\n'.join(lines)+'\n')
    return output.getvalue()


def app_client(tmp_path):
    app=create_app(db_url='sqlite:///'+str(tmp_path/'news.db'),storage_root=str(tmp_path/'runs'))
    return app,TestClient(app)

def ingest(app,tmp_path,monkeypatch):
    parsed=gkg.parse_archive(archive(),STAMP,PROFILE)
    assert len(parsed)==2 and parsed[0]['gkg']['mention_iso']==['ua']
    # The research and place assertions use publisher text as evidence.
    for item in parsed:
        item.update(content='Ukraine and Donald Trump discussed a rare earth agreement for military aid.',
                    content_kind='text')
    monkeypatch.setitem(collect.PROVIDERS,'gkg',lambda cfg:parsed)
    storage_root=tmp_path/'runs'
    with Session(app.state.engine) as s:
        before=[event.event_key for event in s.exec(select(Event)).all()]
        run=collect.run_collection(s,'gkg-fixture',profile_snapshot=PROFILE,storage_root=storage_root)
        assert run.status=='succeeded'
        assert [event.event_key for event in s.exec(select(Event)).all()]==before
        if before:
            return before[0]
        reports=list_reports(s,provider='gkg')
        assert len(reports)==2
        target={}
        for index, report in enumerate(reports):
            result=promote(s,storage_root,report.key,expected_token=candidate_token(report),
                           request_id=f'dual-desk-fixture-{index}',**target)
            target=dict(event_key=result['event_key'],expected_version=result['version'])
    desk.drain(app.state.engine)
    return result['event_key']

def test_backfill_many_to_many_exclusion_and_legacy_links(tmp_path,monkeypatch):
    app,c=app_client(tmp_path)
    trump=c.post('/api/focus',json=PRESETS[0]).json()
    poland=c.post('/api/focus',json=PRESETS[2]).json()
    assert '暂无匹配' in c.get('/focus/'+poland['key']).text
    key=ingest(app,tmp_path,monkeypatch)
    war=c.post('/api/focus',json=PRESETS[1]).json()
    drain_focus(app.state.engine)
    with Session(app.state.engine) as s:
        assert {w['key'] for w in desk.memberships(s,key)}=={trump['key'],war['key']}
    assert c.get('/events?q=Trump',follow_redirects=False).headers['location']=='/radar?q=Trump'
    view=c.get('/events/'+key,params={'back':'/focus/'+war['key']}).text
    assert '返回原焦点' in view and '特朗普活动' in view and '俄乌战争' in view
    assert 'href="//evil.example' not in c.get('/events/'+key,params={'back':'//evil.example'}).text
    topic='/api/focus/'+trump['key']
    assert c.post(topic+'/links',json={'event':key,'action':'exclude'}).status_code==200
    ingest(app,tmp_path,monkeypatch)
    assert c.put(topic,json={**PRESETS[0],'scope':'Donald Trump','version':1}).status_code==200
    assert c.put(topic,json={**PRESETS[0],'version':1}).status_code==409
    with Session(app.state.engine) as s:assert len(desk.memberships(s,key))==1
    reopened,cc=app_client(tmp_path)
    assert '暂无匹配' in cc.get('/focus/'+trump['key']).text
    assert 'Trump seeks' in cc.get('/focus/'+war['key']).text
    assert cc.get('/',follow_redirects=False).headers['location']=='/focus'
    assert cc.post(topic+'/links',json={'event':key,'action':'include'}).status_code==200
    assert '手动关联' in cc.get('/focus/'+trump['key']).text
    assert '事件雷达' in cc.get('/radar').text
    assert cc.get('/',follow_redirects=False).headers['location']=='/radar'
    assert cc.post('/api/focus',json={'name':'x','scope':','}).status_code==422
    assert cc.post('/api/focus',json=PRESETS[0],headers={'Origin':'https://outsider.example'}).status_code==403


def test_geo_evidence_and_research_queue(tmp_path,monkeypatch):
    app,c=app_client(tmp_path)
    key=ingest(app,tmp_path,monkeypatch)
    watch=c.post('/api/focus',json=PRESETS[1]).json()['key']
    drain_focus(app.state.engine)
    with Session(app.state.engine) as s:art=s.exec(select(ArticleVersion)).first()
    data=dict(role='impact',iso='ua',article=art.article_key,revision=art.revision,quote='Ukraine')
    url=f'/api/desk/events/{key}/places'
    assert c.post(url,json={**data,'quote':'invented'}).status_code==422
    place=c.post(url,json=data).json()
    assert '已标注' in c.get('/events/'+key).text
    assert c.delete(url+'/'+place['key']).status_code==200
    request=c.post('/api/focus/'+watch+'/reviews',json={'event':key}).json()
    duplicate=c.post('/api/focus/'+watch+'/reviews',json={'event':key}).json()
    assert request['key']==duplicate['key']
    app.state.desk.review_step()
    with Session(app.state.engine) as s:
        row=s.get(WatchReview,request['key'])
        assert row.status=='blocked' and 'RunDesk' in row.message
    assert c.post('/api/focus/'+watch+'/reviews/'+request['key']+'/retry',json={}).status_code==200
    assert len(c.get('/api/desk/map-shapes').json())>150

def test_calendar_review_without_news(tmp_path):
    app,c=app_client(tmp_path)
    watch=c.post('/api/focus',json=dict(**PRESETS[2],auto_research=True)).json()['key']
    with Session(app.state.engine) as s:
        w=s.get(Watch,watch);w.next_review='2020-01-01T00:00:00Z';s.add(w);s.commit()
    app.state.desk.review_step()
    app.state.desk.review_step()
    with Session(app.state.engine) as s:
        ticket=s.exec(select(WatchReview)).one()
        assert ticket.automatic and ticket.event=='' and ticket.status=='blocked'
        from news2douyin.research.models import ResearchCase
        assert s.get(ResearchCase,ticket.case_key) is not None


def test_watch_research_accumulates_and_honors_cutoff(tmp_path, monkeypatch):
    from test_research import FakeRunDesk, rpc, value
    from news2douyin.research.models import ResearchCase, ResearchRun
    app, c = app_client(tmp_path)
    config = app.state.research_settings.get()
    config.update(runtime_mode='local', search_provider='rundesk')
    app.state.research_settings.write(config)
    fake = FakeRunDesk()
    app.state.research.client_factory = lambda _: fake
    env = app, c, fake
    assert c.post('/api/research/setup').status_code == 200
    event = ingest(app, tmp_path, monkeypatch)
    watch = c.post('/api/focus', json=PRESETS[0]).json()['key']
    drain_focus(app.state.engine)
    url = '/api/focus/' + watch + '/reviews'
    historical = c.post(url,json={'cutoff':'2024-01-01'}).json()['key']
    app.state.desk.review_step()
    with Session(app.state.engine) as s:
        case = s.get(ResearchCase,s.get(WatchReview,historical).case_key)
        assert case.article_keys_json=='[]' and case.cutoff.startswith('2024-01-01')
    ticket = c.post(url,json={'event':event}).json()['key']
    app.state.desk.review_step()
    with Session(app.state.engine) as s:
        row=s.get(WatchReview,ticket)
        assert row.status=='submitted'
        case_id = row.case_key
        run=s.exec(select(ResearchRun).where(ResearchRun.case_id==case_id)).one().run_id
    app.state.research.dispatch(run)
    data=c.get('/api/research/cases/'+case_id).json()
    source=data['sources'][0]['source_id']
    claim=value(rpc(env,'research_save_claim',{'run_id':run,'text':'This report concerns military aid.',
        'kind':'fact','evidence':[{'source_id':source,'paragraph_id':'P1','quote':'military aid'}]}))['claim_id']
    value(rpc(env,'research_save_question',{'run_id':run,'key':'agreement','text':'Was an agreement signed?','status':'gap'}))
    value(rpc(env,'research_save_report',{'run_id':run,'title':'Aid evidence','sections':[{'heading':'Terms','body':'The headline links the deal to aid.','claim_ids':[claim]}],'gaps':['Agreement unclear'],'completeness':'partial'}))
    assert 'Aid evidence' in c.get('/focus/'+watch).text
    summary = c.post(url,json={}).json()['key']
    app.state.desk.review_step()
    with Session(app.state.engine) as s:
        summary_case=s.get(WatchReview,summary).case_key
    data=c.get('/api/research/cases/'+summary_case).json()
    assert 'Was an agreement signed?' in data['goal']
    assert data['sources'] and watch in c.get('/research/'+case_id).text


def test_gkg_schedule_crosses_midnight_and_replays_occurrence():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    config = {**PROFILE, 'timezone':'Asia/Shanghai', 'gkg_time':''}
    occurrence = datetime(2025,2,4,0,15,tzinfo=ZoneInfo('Asia/Shanghai'))
    assert gkg.scheduled_batch(config, occurrence) == '20250203154500'
    next_slot = occurrence.replace(minute=30)
    assert gkg.scheduled_batch(config, next_slot) == '20250203160000'
