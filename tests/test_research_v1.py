"""Submission recovery must survive lost acknowledgements and process restart."""
import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session

from test_research import research, setup_case
from news2douyin.research.models import ResearchRun, ResearchSubmission, ResearchCase
from news2douyin.research.runtime import ResearchRuntime
from news2douyin.research.rundesk import RemoteError, RunDeskClient, submit_once


def test_lost_session_response_recovers_same_session_after_restart(research):
    app,client,fake=research
    case,_=setup_case(research,start=False)
    run=app.state.research.enqueue(case['case_id'],'original instruction')
    original=fake.request; dropped=False
    def request(method,path,payload=None,params=None,**kw):
        nonlocal dropped
        value=original(method,path,payload,params,**kw)
        if path=='/sessions' and method=='POST' and not dropped:
            dropped=True;raise RemoteError('lost session acknowledgement',uncertain=True)
        return value
    fake.request=request
    app.state.research.dispatch(run['run_id'])
    with Session(app.state.engine) as s:
        current=s.get(ResearchRun,run['run_id']);assert current.status=='uncertain' and not current.session_id
        case_row=s.get(ResearchCase,case['case_id']);case_row.title='Renamed while recovering';s.add(case_row);s.commit()
    runtime=ResearchRuntime(app.state.engine,app.state.research_settings,lambda _:fake)
    assert runtime.sync(run['run_id'])['status']=='running'
    assert len(fake.sessions)==1
    assert sum(p=='/sessions' and m=='POST' for m,p,_,_ in fake.calls)==1
    assert sum(p.endswith('/turns') for _,p,_,_ in fake.calls)==1
    assert next(iter(fake.sessions.values()))['source']=={'kind':'application','appId':'news2douyin','taskId':run['run_id']}


def test_processing_and_unconfirmed_receipts_never_start_replacement(research):
    app,_,fake=research;case,_=setup_case(research,start=False)
    run=app.state.research.enqueue(case['case_id'])
    with Session(app.state.engine) as s: op=s.get(ResearchSubmission,run['run_id'])
    for state in ['processing','unconfirmed']:
        fake.receipts[op.session_key]={'method':'POST','path':'/sessions','state':state}
        app.state.research.dispatch(run['run_id'])
        app.state.research.sync(run['run_id'])
        assert not fake.sessions
        with Session(app.state.engine) as s:assert s.get(ResearchRun,run['run_id']).status=='uncertain'


def test_mismatched_run_is_never_stopped_or_steered(research):
    app,_,fake=research;case,run=setup_case(research)
    fake.sessions['session-1']['runId']='someone-elses-run'
    with pytest.raises(ValueError):app.state.research.steer(case['case_id'],'do not send','new-request-1234')
    assert app.state.research.cancel(case['case_id'])['status']=='paused'
    assert fake.sessions['session-1']['status']=='running'
    stop=next(body for _,path,body,_ in fake.calls if path.endswith('/stop'))
    assert stop['expectedRunId']=='run-session-1'


def test_cancel_unknown_turn_recovers_receipt_without_reposting(research):
    app,_,fake=research;fake.timeout_turn=True;case,run=setup_case(research)
    assert app.state.research.cancel(case['case_id'])['status']=='paused'
    assert sum(p.endswith('/turns') for _,p,_,_ in fake.calls)==1
    assert fake.sessions['session-1']['status']=='interrupted'


def test_cancel_before_turn_does_not_start_a_turn(research):
    app,_,fake=research;case,_=setup_case(research,start=False)
    run=app.state.research.enqueue(case['case_id']);original=fake.request
    def request(method,path,payload=None,params=None,**kw):
        value=original(method,path,payload,params,**kw)
        if method=='POST' and path=='/sessions':app.state.research.cancel(case['case_id'])
        return value
    fake.request=request;app.state.research.dispatch(run['run_id'])
    assert not any(p.endswith('/turns') for _,p,_,_ in fake.calls)
    with Session(app.state.engine) as s:assert s.get(ResearchRun,run['run_id']).status=='paused'


def test_complete_failure_receipt_is_not_success():
    class Client:
        def request(self,*args,**kwargs):return {'state':'completed','method':'POST','path':'/sessions','httpStatus':409,'response':{'code':'session_busy','error':'busy'}}
    with pytest.raises(RemoteError) as e:submit_once(Client(),'request-one','/sessions',{})
    assert e.value.status==409


def test_client_uses_v1_key_and_structured_error(monkeypatch):
    class Response:
        content=b'{}';status_code=409
        def json(self):return {'code':'request_unconfirmed','requestId':'request-1234','retryable':False,'error':'not confirmed'}
    calls=[]
    monkeypatch.setattr('requests.request',lambda *args,**kw:(calls.append((args,kw)) or Response()))
    with pytest.raises(RemoteError) as e:RunDeskClient({'rundesk_url':'http://localhost:3210/api/v1','rundesk_token':'private'}).request('POST','/sessions',{},key='request-one')
    assert calls[0][0][1]=='http://localhost:3210/api/v1/sessions'
    assert calls[0][1]['headers']['Idempotency-Key']=='request-one'
    assert e.value.uncertain and e.value.code=='request_unconfirmed' and e.value.request_id=='request-1234'
    assert 'private' not in str(e.value)


def test_legacy_run_adopts_only_matching_saved_input(research):
    app,_,fake=research;case,run=setup_case(research)
    with Session(app.state.engine) as s:s.delete(s.get(ResearchSubmission,run['run_id']));s.commit()
    original=fake.request
    def request(method,path,payload=None,params=None,**kw):
        if path.endswith('/events'):
            return [{'method':'run/input','data':{'runId':'run-session-1','input':{'text':'研究执行 run_id='+run['run_id']+'，课题'}}}]
        return original(method,path,payload,params,**kw)
    fake.request=request
    assert app.state.research.sync(run['run_id'])['status']=='running'
    with Session(app.state.engine) as s:assert s.get(ResearchSubmission,run['run_id']).legacy
    assert app.state.research.cancel(case['case_id'])['status']=='paused'


def test_setup_rejects_old_server_before_mutation(research):
    app,client,fake=research;original=fake.request
    fake.request=lambda m,p,*a,**kw: {'version':'0.5.4','capabilities':['instances','skills','mcp','sessions']} if p=='/meta' else original(m,p,*a,**kw)
    assert client.post('/api/research/setup').status_code==400
    assert not fake.instances


def test_setup_keys_saved_without_secrets_in_public_settings(research):
    app,client,fake=research;setup_case(research,start=False)
    saved=app.state.research_settings.get()['setup_requests'];assert all(v['key'] in fake.keys for v in saved.values())
    assert 'setup_requests' not in client.get('/api/research/settings').json()
    checked=client.post('/api/research/check').json();assert checked['default_model']=='test-model' and checked['mcp_ready']


def test_unreadable_receipt_does_not_mean_submission_failed():
    class Client:
        def request(self,*args,**kwargs):raise RemoteError('auth expired',status=401,code='unauthorized')
    with pytest.raises(RemoteError) as error:submit_once(Client(),'request-one','/sessions',{})
    assert error.value.uncertain and error.value.code=='unauthorized'
