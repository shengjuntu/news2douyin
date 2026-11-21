"""Frozen causal RunDesk routing and actual selected-session tool admission."""
from copy import deepcopy
import json

import pytest
from sqlmodel import Session, select

from news2douyin.causal.models import CausalAgentLink, CausalInvestigation, CausalNode
from news2douyin.research.models import ResearchEnvironment, ResearchRun, ResearchSubmission
from news2douyin.research.rundesk import CAUSAL_TOOLS, RESEARCH_TOOLS, RemoteError, validate_runtime
from test_research import FakeRunDesk, research, setup_case


class CausalDesk(FakeRunDesk):
    def __init__(self):
        super().__init__()
        self.available=set(CAUSAL_TOOLS)
        self.session_available=None
        self.prepare_preset=None
        self.session_preset=None
        self.app_key=False
        self.sql_engine=None

    def overview(self, session=None):
        available=self.available if self.session_available is None or session is None else self.session_available
        result=dict(instance={'id':'instance-research','defaultModel':'test-model'}, errors={},
            mcp={'status':{'data':[{'name':'news2douyin-research','tools':{name:{} for name in available}}]}})
        if session is not None:
            result['session']=deepcopy(session)
            if session.get('environmentId'):
                result['environment']={'id':session['environmentId']}
        return result

    def _request(self, method, path, payload=None, params=None):
        if path=='/meta':
            self.calls.append((method,path,payload,params))
            return dict(apiVersions=['v1'],version='0.75.1',demo=False,capabilities=[
                'api-v1','idempotency','configuration-summary','application-connect',
                'application-runtime','application-runtime-presets'])
        if path=='/applications/news2douyin/runtime':
            self.calls.append((method,path,payload,params))
            if self.sql_engine is not None:
                assert self.sql_engine.pool.checkedout()==0, 'Preset lookup retained a SQL transaction'
            return dict(presetId=params['presetId'],instanceId='instance-causal',workspaceId='workspace-causal')
        if path.endswith('/runtime/prepare'):
            self.calls.append((method,path,payload,params))
            return dict(instanceId=payload['instanceId'],workspaceId=payload['workspaceId'],
                presetId=payload.get('presetId','') if self.prepare_preset is None else self.prepare_preset,
                executionMode='docker',environment={'id':'env-causal','state':'running'},
                probed=True,accountReady=True,errors={},servers={'news2douyin-research':sorted(self.available)})
        if path.startswith('/sessions/') and path.endswith('/configuration'):
            if self.app_key:raise RemoteError('denied',status=403,code='forbidden')
            self.calls.append((method,path,payload,params))
            return self.overview(self.sessions[path.split('/')[2]])
        if path.startswith('/instances/') and path.endswith('/configuration'):
            if self.app_key:raise RemoteError('denied',status=403,code='forbidden')
            self.calls.append((method,path,payload,params))
            return self.overview()
        result=super()._request(method,path,payload,params)
        if method=='POST' and path=='/sessions':
            if payload['instanceId']=='instance-causal':
                result.update(executionMode='docker',environmentId='env-causal')
            else:
                result['executionMode']='local'
            if self.session_preset is not None:
                result['source']['presetId']=self.session_preset
        return result


@pytest.fixture
def causal_case(research, monkeypatch):
    for suffix in ('PRESET','INSTANCE_ID','WORKSPACE_ID'):
        monkeypatch.delenv('NEWS2DOUYIN_CAUSAL_RUN_'+suffix,raising=False)
    app,client,_=research
    fake=CausalDesk();app.state.research.client_factory=lambda _config:fake
    case,_=setup_case((app,client,fake),start=False)
    key='ci_'+'a'*24;node='cn_'+'b'*24
    with Session(app.state.engine) as s:
        s.add(CausalInvestigation(key=key,text='合成政策变更',event_at='2022-02-24T12:00:00Z'))
        s.add(CausalNode(key=node,case_key=key,text='目标',occurred_at='2022-02-24T12:00:00Z',time_status='root'))
        s.add(CausalAgentLink(case_key=key,research_case_id=case['case_id'],date_from='1991-01-01',last_error='old error'))
        s.commit()
    fake.calls.clear()
    return app,client,fake,case,key


def saved(app,run_id):
    with Session(app.state.engine,expire_on_commit=False) as s:
        return s.get(ResearchRun,run_id),s.get(ResearchSubmission,run_id),s.get(ResearchEnvironment,run_id)


def test_shared_enqueue_is_atomic_local_and_freezes_causal_prompt(causal_case):
    app,_,fake,case,key=causal_case
    run=app.state.research.enqueue(case['case_id'])
    assert not fake.calls, 'Default shared route must not resolve or create a new preset'
    row,submission,plan=saved(app,run['run_id'])
    with Session(app.state.engine) as s:
        link=s.get(CausalAgentLink,key)
        assert link.current_run_id==row.run_id and link.last_error==''
    assert plan.mode=='local' and plan.search_provider=='rundesk' and plan.search_backend=='exa'
    session=json.loads(submission.session_payload);turn=json.loads(submission.turn_payload)
    assert session['instanceId']=='instance-research' and session['workspaceId']=='workspace-research'
    assert session['source']=={'kind':'application','appId':'news2douyin','taskId':row.run_id}
    assert not turn['skills']
    for text in ('causal_get_investigation','causal_search_events','causal_save_step','1991-01-01',
                 'possible_cause','contradicts','document_reference','stop_reason','research_save_report','partial'):
        assert text in turn['text']
    assert app.state.research.enqueue(case['case_id'])['run_id']==row.run_id


def test_local_dispatch_without_exa_admits_actual_tools_and_callback(causal_case):
    app,client,fake,case,key=causal_case
    run=app.state.research.enqueue(case['case_id']);received=[]
    def callback():
        with Session(app.state.engine) as s:
            assert s.get(CausalAgentLink,key).current_run_id==run['run_id']
        response=client.post('/mcp/research',headers={
            'Authorization':'Bearer '+app.state.research_settings.get()['mcp_token']},json={
            'jsonrpc':'2.0','id':1,'method':'tools/call','params':{
                'name':'causal_get_investigation','arguments':{'run_id':run['run_id']}}})
        result=response.json()['result'];assert not result['isError'],result
        received.append(json.loads(result['content'][0]['text']))
    fake.on_turn=callback
    app.state.research.dispatch(run['run_id'])
    row,_,_=saved(app,run['run_id']);assert row.status=='running',row.error
    assert received[0]['case_key']==key
    paths=[path for _,path,_,_ in fake.calls]
    assert paths.index('/sessions/session-1') < paths.index('/sessions/session-1/turns')
    assert not any(path.endswith('/mcp/exa') for _,path,_,_ in fake.calls)
    assert not any(path.startswith('/sessions/') and path.endswith('/configuration') for _,path,_,_ in fake.calls)


@pytest.mark.parametrize('missing',['causal_get_investigation','causal_search_events','causal_save_step'])
def test_each_causal_tool_is_required_before_session_creation(causal_case,missing):
    app,_,fake,case,_=causal_case
    fake.available.remove(missing)
    run=app.state.research.enqueue(case['case_id']);app.state.research.dispatch(run['run_id'])
    row,_,_=saved(app,run['run_id'])
    assert row.status=='failed' and '因果' in row.error and not fake.sessions


def test_actual_session_preset_mismatch_blocks_turn_even_if_runtime_probe_passed(causal_case):
    app,_,fake,case,_=causal_case
    fake.session_preset='unexpected-preset'
    run=app.state.research.enqueue(case['case_id']);app.state.research.dispatch(run['run_id'])
    row,submission,_=saved(app,run['run_id'])
    assert row.status=='failed' and '因果' in row.error
    assert len(fake.sessions)==1 and not submission.turn_attempted
    assert not any(path.endswith('/turns') for _,path,_,_ in fake.calls)


def test_optional_preset_resolves_outside_sql_and_dispatch_ignores_changed_defaults(causal_case,monkeypatch):
    app,_,fake,case,_=causal_case
    monkeypatch.setenv('NEWS2DOUYIN_CAUSAL_RUN_PRESET','causal-history')
    fake.sql_engine=app.state.engine
    run=app.state.research.enqueue(case['case_id'])
    row,submission,plan=saved(app,run['run_id'])
    assert row.instance_id=='instance-causal' and row.workspace_id=='workspace-causal' and plan.mode=='docker'
    assert json.loads(submission.session_payload)['source']['presetId']=='causal-history'
    config=app.state.research_settings.get()
    config.update(instance_id='new-default',workspace_id='new-workspace',runtime_mode='local')
    app.state.research_settings.write(config)
    monkeypatch.setenv('NEWS2DOUYIN_CAUSAL_RUN_PRESET','invalid/new/default')
    assert app.state.research.enqueue(case['case_id'])['run_id']==run['run_id']
    fake.calls.clear()
    fake.app_key=True
    app.state.research.dispatch(run['run_id'])
    row,_,plan=saved(app,run['run_id']);assert row.status=='running',row.error
    prepare=next(payload for _,path,payload,_ in fake.calls if path.endswith('/runtime/prepare'))
    assert prepare==dict(instanceId='instance-causal',workspaceId='workspace-causal',probe=True,presetId='causal-history')
    assert plan.environment_id=='env-causal'
    assert not any(method in ('PUT','PATCH') or path.endswith('/connect') for method,path,_,_ in fake.calls)
    assert not any(path.startswith(('/instances/','/workspaces/')) or path.endswith('/configuration')
        for _,path,_,_ in fake.calls)


def test_explicit_preset_pair_and_missing_pair_are_validated(causal_case,monkeypatch):
    app,_,fake,case,_=causal_case
    monkeypatch.setenv('NEWS2DOUYIN_CAUSAL_RUN_PRESET','causal-history')
    monkeypatch.setenv('NEWS2DOUYIN_CAUSAL_RUN_INSTANCE_ID','instance-causal')
    with pytest.raises(ValueError,match='成对'):app.state.research.enqueue(case['case_id'])
    with Session(app.state.engine) as s:
        assert not s.exec(select(ResearchRun).where(ResearchRun.case_id==case['case_id'])).first()
    monkeypatch.setenv('NEWS2DOUYIN_CAUSAL_RUN_WORKSPACE_ID','workspace-causal')
    run=app.state.research.enqueue(case['case_id']);assert run['instance_id']=='instance-causal'
    assert not fake.calls
    fake.prepare_preset='other-preset'
    app.state.research.dispatch(run['run_id'])
    row,_,_=saved(app,run['run_id']);assert row.status=='failed' and '预设' in row.error
    assert not fake.sessions


def test_uncertain_turn_resumes_saved_receipt_after_defaults_and_tools_change(causal_case,monkeypatch):
    app,_,fake,case,_=causal_case
    run=app.state.research.enqueue(case['case_id']);fake.timeout_turn=True
    app.state.research.dispatch(run['run_id'])
    row,_,_=saved(app,run['run_id']);assert row.status=='uncertain'
    config=app.state.research_settings.get();config.update(instance_id='new-default',workspace_id='new-workspace',runtime_mode='docker')
    app.state.research_settings.write(config)
    monkeypatch.setenv('NEWS2DOUYIN_CAUSAL_RUN_PRESET','invalid/new/default')
    fake.available.clear();fake.session_available=set();fake.timeout_turn=False
    before=len(fake.calls)
    assert app.state.research.sync(run['run_id'])['status']=='running'
    assert not any(path.endswith('/configuration') or path.endswith('/runtime/prepare')
        for _,path,_,_ in fake.calls[before:])
    assert len(fake.sessions)==1 and sum(path.endswith('/turns') for _,path,_,_ in fake.calls)==1
    assert app.state.research.cancel(case['case_id'])['status']=='paused'
    assert next(payload for _,path,payload,_ in fake.calls if path.endswith('/stop'))['expectedRunId']=='run-session-1'


def test_causal_docker_readiness_is_local_capable_but_normal_research_still_needs_exa():
    config=dict(instance_id='i',workspace_id='w',search_provider='rundesk',search_backend='exa')
    runtime=dict(instanceId='i',workspaceId='w',executionMode='docker',
        environment={'id':'env','state':'running'},probed=True,accountReady=True,errors={},
        servers={'news2douyin-research':sorted(CAUSAL_TOOLS)})
    assert validate_runtime(runtime,config,causal=True)['id']=='env'
    runtime['servers']['news2douyin-research']=sorted(RESEARCH_TOOLS)
    with pytest.raises(ValueError,match='Exa'):validate_runtime(runtime,config)
