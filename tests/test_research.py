import json
import copy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.server.app import create_app
from news2douyin.storage.models import Article
from news2douyin.research.models import ResearchRun, ResearchReport, ResearchSource
from news2douyin.research import service as svc
from news2douyin.research.rundesk import RemoteError
from news2douyin.research.tools import ResearchTools


class FakeRunDesk:
    """Contract fixture for RunDesk API v1; does not simulate model quality."""
    def __init__(self):
        self.calls, self.instances, self.workspaces, self.sessions = [], [], [], {}
        self.skills, self.servers = {}, {'unrelated': {'command': 'keep-me'}}
        self.timeout_turn = False
        self.on_turn = None
        self.receipts = {}
        self.keys = []

    def request(self, method, path, payload=None, params=None, *, key=None):
        if path.startswith('/requests/'):
            value=self.receipts.get(path.rsplit('/',1)[-1])
            if value is None: raise RemoteError('not found',status=404,code='request_not_found')
            return copy.deepcopy(value)
        if key:
            self.keys.append(key)
            if key in self.receipts:
                assert self.receipts[key]['payload']==payload
                return copy.deepcopy(self.receipts[key]['response'])
        try:
            result=self._request(method,path,payload,params)
        except RemoteError as exc:
            if key and exc.uncertain and path.endswith('/turns'):
                self.receipts[key]={'method':'POST','path':path,'state':'completed','httpStatus':202,'response':copy.deepcopy(self.sessions[path.split('/')[2]]),'payload':copy.deepcopy(payload)}
            raise
        if key: self.receipts[key]={'method':'POST','path':path,'state':'completed','httpStatus':202 if path.endswith('/turns') else 200,'response':copy.deepcopy(result),'payload':copy.deepcopy(payload)}
        return copy.deepcopy(result)

    def _request(self, method, path, payload=None, params=None):
        self.calls.append((method, path, payload, params))
        if path == '/meta':
            return {'version':'0.6.1','demo':False,'apiVersions':['v1'],'capabilities':['instances','skills','mcp','sessions','api-v1','idempotency','configuration-summary']}
        if path == '/instances':
            if method == 'POST':
                row = {'id': 'instance-research', 'codexHome': '/remote/instance-research/codex', **payload}
                self.instances.append(row); return row
            return self.instances
        if path == '/workspaces':
            if method == 'POST':
                row = {'id': 'workspace-research', **payload}; self.workspaces.append(row); return row
            return self.workspaces
        if '/skills/' in path and method == 'PUT':
            assert params['instanceId'] == 'instance-research'
            assert params['scope'] == 'instance'
            assert 'research_save_report' in payload['content']
            self.skills['news-research'] = {'name':'news-research','path':'/remote/instance-research/codex/skills/news-research/SKILL.md','enabled':True}
            return {'ok':True}
        if path.endswith('/skills'):
            return {'data': [{'skills': list(self.skills.values())}]}
        if path.endswith('/mcp'):
            return {'version': 'config-v1', 'status': {'data': [{'name':'news2douyin-research','tools':{'research_get_case':{}}}]}}
        if '/mcp/' in path and method == 'PUT':
            assert params['instanceId'] == 'instance-research'
            assert payload['version'] == 'config-v1'
            self.servers[path.rsplit('/',1)[1]] = payload['config']; return {'saved':True}
        if path.endswith('/account'):
            return {'account': {'type':'apiKey'}, 'requiresOpenaiAuth':True}
        if path == '/sessions' and method == 'GET': return list(self.sessions.values())
        if path.endswith('/configuration'):
            return {'instance':{'defaultModel':'test-model'},'skills':list(self.skills.values()),'mcp':{'status':{'data':[{'name':'news2douyin-research','tools':{'research_get_case':{}}}]}},'errors':{}}
        if path == '/sessions' and method == 'POST':
            sid = 'session-' + str(len(self.sessions)+1)
            row = {'id':sid,'instanceId':payload['instanceId'],'workspaceId':payload['workspaceId'],'status':'idle','turnId':'','runId':'','source':payload.get('source',{})}
            self.sessions[sid]=row; return row
        if path.startswith('/sessions/'):
            sid = path.split('/')[2]; row=self.sessions[sid]
            if path.endswith('/turns'):
                row['status']='running'; row['turnId']='turn-'+sid; row['runId']='run-'+sid
                if self.on_turn: self.on_turn()
                if self.timeout_turn: raise RemoteError('提交超时',uncertain=True)
                return row
            if path.endswith('/stop'):
                if payload.get('expectedRunId')!=row['runId']:raise RemoteError('run conflict',status=409,code='run_conflict')
                row['status']='interrupted'; return {'ok':True}
            if path.endswith('/steer'):
                assert payload['expectedTurnId']==row['turnId']; return {'status':'accepted'}
            return row
        raise AssertionError((method,path,payload,params))


@pytest.fixture
def research(tmp_path):
    app=create_app(db_url=f'sqlite:///{tmp_path}/research.db',storage_root=str(tmp_path/'runs'))
    fake=FakeRunDesk();app.state.research.client_factory=lambda config:fake
    with Session(app.state.engine) as s:
        s.add(Article(article_key='seed',title='Example policy changed',content='The old policy was introduced in 2023.\nThe new policy was announced in 2026.',published_at='2026-01-01T00:00:00Z',url='https://example.com/news'))
        s.commit()
    client=TestClient(app)
    yield app,client,fake
    app.state.engine.dispose()


def setup_case(research, start=True):
    app,client,fake=research
    assert client.post('/api/research/setup').status_code==200
    response=client.post('/api/research/cases',json={'article_keys':['seed'],'goal':'Investigate the history','strategy_id':'background'})
    assert response.status_code==200,response.text
    case=response.json()
    if not start:return case,None
    response=client.post('/api/research/cases/'+case['case_id']+'/start',json={})
    assert response.status_code==200,response.text
    run=response.json();app.state.research.dispatch(run['run_id'])
    return case,run


def rpc(research,name,args):
    app,client,_=research
    response=client.post('/mcp/research',headers={'Authorization':'Bearer '+app.state.research_settings.get()['mcp_token']},
                         json={'jsonrpc':'2.0','id':7,'method':'tools/call','params':{'name':name,'arguments':args}})
    assert response.status_code==200,response.text
    return response.json()['result']


def value(result):
    assert not result['isError'],result
    return json.loads(result['content'][0]['text'])


def make_claim(research,case,run):
    source=case['sources'][0]['source_id']
    return value(rpc(research,'research_save_claim',{'run_id':run['run_id'],'text':'The previous policy dates to 2023.',
        'kind':'fact','occurred_at':'2023','evidence':[{'source_id':source,'paragraph_id':'P1','quote':'introduced in 2023'}]}))['claim_id']


def test_bootstrap_is_instance_scoped_and_repeatable(research):
    app,client,fake=research
    assert client.put('/api/research/settings',json={'rundesk_url':'http://localhost:3210','rundesk_token':'private-rundesk-token','callback_url':'http://localhost:18080','search_provider':'tavily','search_api_key':'private-search-key'}).status_code==200
    for _ in range(2): assert client.post('/api/research/setup').status_code==200
    assert len(fake.instances)==len(fake.workspaces)==1
    assert fake.servers['unrelated']=={'command':'keep-me'}
    assert fake.servers['news2douyin-research']['url']=='http://localhost:18080/mcp/research'
    text=client.get('/api/research/settings').text
    assert 'private-rundesk-token' not in text and 'private-search-key' not in text
    assert app.state.research_settings.get()['mcp_token'] not in text
    check=client.post('/api/research/check').json();assert check['account_ready'] and check['mcp_ready']
    assert not any('/turns' in c[1] for c in fake.calls)


def test_complete_mcp_research_resume_versions_and_export(research):
    app,client,fake=research;case,run=setup_case(research)
    data=value(rpc(research,'research_get_case',{'run_id':run['run_id']}));assert data['goal']=='Investigate the history'
    claim=make_claim(research,case,run)
    question={'run_id':run['run_id'],'key':'history','text':'When did the old policy start?','status':'answered','answer':'2023','claim_ids':[claim]}
    assert not rpc(research,'research_save_question',question)['isError']
    report={'run_id':run['run_id'],'title':'Background analysis','sections':[{'heading':'History','body':'The prior policy dates to 2023.','claim_ids':[claim]}],'gaps':['Implementation impact remains unknown.'],'completeness':'complete'}
    first=value(rpc(research,'research_save_report',report));second=value(rpc(research,'research_save_report',report));assert first==second
    sid=next(iter(fake.sessions));fake.sessions[sid]['status']='completed'
    assert app.state.research.sync(run['run_id'])['status']=='completed'
    text=client.get(f'/api/research/cases/{case["case_id"]}/reports/{first["report_id"]}/download').text
    assert 'introduced in 2023' in text and 'https://example.com/news' in text
    new=client.post('/api/research/cases/'+case['case_id']+'/start',json={'instruction':'Investigate the impact'}).json()
    app.state.research.dispatch(new['run_id']);assert len(fake.sessions)==2
    assert rpc(research,'research_save_report',report)['isError']
    report.update(run_id=new['run_id'],title='Updated analysis',completeness='partial')
    assert value(rpc(research,'research_save_report',report))['version']==2
    assert len(client.get('/api/research/cases/'+case['case_id']).json()['reports'])==2


def test_quote_validation_and_case_boundary(research):
    case,run=setup_case(research)
    args={'run_id':run['run_id'],'text':'unsupported','kind':'fact','evidence':[{'source_id':case['sources'][0]['source_id'],'paragraph_id':'P1','quote':'fabricated quotation'}]}
    assert rpc(research,'research_save_claim',args)['isError']
    app,client,_=research
    other=client.post('/api/research/cases',json={'article_keys':['seed'],'goal':'other research'}).json()
    args['evidence'][0].update(source_id=other['sources'][0]['source_id'],quote='introduced in 2023')
    assert rpc(research,'research_save_claim',args)['isError']
    with Session(app.state.engine) as s: assert not list(s.exec(select(ResearchReport)))


def test_stop_fences_late_writes_and_can_resume(research):
    app,client,fake=research;case,run=setup_case(research)
    response=client.post('/api/research/cases/'+case['case_id']+'/stop');assert response.json()['status']=='paused'
    assert rpc(research,'research_save_question',{'run_id':run['run_id'],'key':'late','text':'late write'})['isError']
    assert client.post('/api/research/cases/'+case['case_id']+'/start',json={}).status_code==200


def test_cancel_during_turn_submission_is_sent_after_acceptance(research):
    app,client,fake=research;case,_=setup_case(research,start=False)
    run=client.post('/api/research/cases/'+case['case_id']+'/start',json={}).json()
    fake.on_turn=lambda:app.state.research.cancel(case['case_id'])
    app.state.research.dispatch(run['run_id'])
    with Session(app.state.engine) as s: assert s.get(ResearchRun,run['run_id']).status=='paused'
    paths=[x[1] for x in fake.calls];assert paths.index('/sessions/session-1/stop')>paths.index('/sessions/session-1/turns')


def test_timeout_does_not_resubmit_turn_and_sync_recovers(research):
    app,client,fake=research;fake.timeout_turn=True
    case,run=setup_case(research)
    with Session(app.state.engine) as s: assert s.get(ResearchRun,run['run_id']).status=='uncertain'
    assert client.post('/api/research/cases/'+case['case_id']+'/start',json={}).status_code==400
    assert app.state.research.sync(run['run_id'])['status']=='running'
    assert sum(p.endswith('/turns') for _,p,_,_ in fake.calls)==1


def test_finished_without_report_is_partial_not_success(research):
    app,client,fake=research;case,run=setup_case(research)
    fake.sessions['session-1']['status']='completed'
    state=app.state.research.sync(run['run_id']);assert state['status']=='partial' and '未通过 MCP' in state['error']


def test_steering_is_persisted_and_deduplicated(research):
    app,client,fake=research;case,run=setup_case(research)
    path='/api/research/cases/'+case['case_id']+'/steer'
    payload={'text':'Prioritize historical context','request_id':'stable-request-1'}
    assert client.post(path,json=payload).status_code==200
    assert client.post(path,json=payload).status_code==200
    assert sum(p.endswith('/steer') for _,p,_,_ in fake.calls)==1
    assert any(a['message']==payload['text'] for a in client.get('/api/research/cases/'+case['case_id']).json()['activity'])


def test_mcp_auth_origin_protocol_and_pages(research):
    app,client,_=research
    initialize={'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-06-18'}}
    assert client.post('/mcp/research',json=initialize).status_code==401
    headers={'Authorization':'Bearer '+app.state.research_settings.get()['mcp_token']}
    assert client.post('/mcp/research',json=initialize,headers={**headers,'Origin':'https://evil.example'}).status_code==403
    assert client.post('/mcp/research',json=initialize,headers=headers).json()['result']['protocolVersion']=='2025-06-18'
    assert client.post('/mcp/research',json={'jsonrpc':'2.0','method':'notifications/initialized'},headers=headers).status_code==202
    assert client.get('/mcp/research',headers=headers).status_code==405
    for path in ['/research','/research?article_key=seed','/research/settings','/research-assets/research.js']:
        assert client.get(path).status_code==200


def test_strategy_snapshot_and_optimistic_revision(research):
    app,client,_=research;case,_=setup_case(research,start=False)
    strategy=next(s for s in client.get('/api/research/strategies').json() if s['strategy_id']=='background')
    strategy.pop('strategy_id');strategy['max_searches']=2
    assert client.put('/api/research/strategies/background',json=strategy).status_code==200
    assert client.put('/api/research/strategies/background',json=strategy).status_code==400
    assert client.get('/api/research/cases/'+case['case_id']).json()['strategy']['max_searches']==10


def test_search_budget_and_idempotent_network_call(research,monkeypatch):
    app,client,_=research;case,run=setup_case(research)
    calls=[]
    monkeypatch.setattr('news2douyin.research.sources.web_search',lambda *args:(calls.append(1) or []))
    args={'run_id':run['run_id'],'query':'historical background','scope':'web'}
    assert value(rpc(research,'research_search',args))==value(rpc(research,'research_search',args));assert len(calls)==1
    with Session(app.state.engine) as s:
        r=s.get(ResearchRun,run['run_id']);r.search_count=10;s.add(r);s.commit()
    args['query']='another query';assert rpc(research,'research_search',args)['isError'];assert len(calls)==1


def test_future_source_rejected_and_original_snapshot_preserved(research,monkeypatch):
    app,client,_=research;case,run=setup_case(research)
    monkeypatch.setattr('news2douyin.research.sources.read_document',lambda url:dict(title='Future',url=url,text='future material',kind='webpage',published_at='2099-01-01T00:00:00Z'))
    assert rpc(research,'research_read_source',{'run_id':run['run_id'],'url':'https://example.com/future'})['isError']
    with Session(app.state.engine) as s:
        a=s.exec(select(Article).where(Article.article_key=='seed')).one();a.content='changed';s.add(a);s.commit()
    doc=value(rpc(research,'research_read_source',{'run_id':run['run_id'],'source_id':case['sources'][0]['source_id']}))
    assert 'introduced in 2023' in doc['paragraphs'][0]['text']


def test_source_fetch_blocks_private_network(monkeypatch):
    from news2douyin.research.sources import safe_fetch,TextReader
    monkeypatch.setattr('socket.getaddrinfo',lambda *a,**k:[(2,1,6,'',('127.0.0.1',80))])
    with pytest.raises(ValueError,match='内网'):safe_fetch('http://example.com/metadata')
    parser=TextReader();parser.feed('<html><title>Title</title><script>steal secret</script><article><p>Actual news.</p></article></html>')
    assert 'Actual news.' in ''.join(parser.parts) and 'steal secret' not in ''.join(parser.parts)


def test_settings_cannot_change_while_running(research):
    app,client,_=research;setup_case(research)
    assert client.put('/api/research/settings',json={'rundesk_url':'http://other:3210'}).status_code==400
    assert client.post('/api/research/setup').status_code==400


def test_continuation_cutoff_does_not_rewrite_old_report(research):
    app,client,fake=research;case,run=setup_case(research)
    report={'run_id':run['run_id'],'title':'Original cutoff','sections':[{'heading':'Notes','body':'Pending investigation','claim_ids':[]}],'gaps':['Missing sources'],'completeness':'partial'}
    first=value(rpc(research,'research_save_report',report))
    fake.sessions['session-1']['status']='completed';app.state.research.sync(run['run_id'])
    with Session(app.state.engine) as s:
        c=svc.require_case(s,case['case_id']);c.cutoff='2026-01-02T00:00:00Z';s.add(c);s.commit()
    new=client.post('/api/research/cases/'+case['case_id']+'/start',json={'refresh_cutoff':True}).json()
    assert new['cutoff']>'2026-01-02T00:00:00Z'
    with Session(app.state.engine) as s:assert s.get(ResearchReport,first['report_id']).cutoff==case['cutoff']


def test_recover_interrupted_dispatch_checks_remote_without_resubmitting(research):
    app,client,fake=research;case,run=setup_case(research)
    with Session(app.state.engine) as s:
        row=s.get(ResearchRun,run['run_id']);row.dispatch_done=False;row.status='starting'
        row.started_at=(datetime.now(timezone.utc)-timedelta(seconds=100)).isoformat();s.add(row);s.commit()
    app.state.research.tick(run['run_id']);app.state.research.tick(run['run_id'])
    assert sum(p.endswith('/turns') for _,p,_,_ in fake.calls)==1
    with Session(app.state.engine) as s:assert s.get(ResearchRun,run['run_id']).status=='running'


def test_pdf_source_extraction_and_empty_scan_message(monkeypatch):
    pypdf=pytest.importorskip('pypdf')
    from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
    from io import BytesIO
    from news2douyin.research.sources import read_document
    writer=pypdf.PdfWriter();page=writer.add_blank_page(width=400,height=400)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    stream=DecodedStreamObject();stream.set_data(b'BT /F1 12 Tf 20 300 Td (The old policy was introduced in 2023.) Tj ET')
    page[NameObject('/Contents')]=writer._add_object(stream);buffer=BytesIO();writer.write(buffer)
    monkeypatch.setattr('news2douyin.research.sources.safe_fetch',lambda url:(url,'application/pdf',buffer.getvalue()))
    doc=read_document('https://example.com/policy.pdf');assert doc['kind']=='pdf' and 'introduced in 2023' in doc['text']
