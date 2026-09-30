import copy
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.editorial import daily, daily_tasks as gen
from news2douyin.server.app import create_app
from news2douyin.storage.articles import record_version
from news2douyin.storage.models import (Article, ArticleEventLink, DailySelection, DailyScriptGeneration,
    Event, ScriptPackage, TaskCheckpoint, TaskRecord)
from news2douyin.tasks.control import TaskConflict
from news2douyin.tasks.service import TaskContext, TaskService
from news2douyin.tasks.worker import TaskWorker
from news2douyin.video import workbench as wb


@pytest.fixture
def chosen(engine):
    with Session(engine) as s:
        s.add(Event(event_key='topic', event_title='同一专题中的多篇报道'))
        for i in range(3):
            a=Article(article_key=f'a{i}', title=f'所选报道{i}', content=f'第{i}条原始报道的内容。内容仅用于离线测试。',
                      source_domain='fixture.test', url=f'https://fixture.test/{i}')
            s.add(a);s.flush();record_version(s,a)
            s.add(ArticleEventLink(article_key=a.article_key,event_key='topic'))
        s.commit()
        daily.choose(s,'a0',day='2026-09-30');picks=daily.choose(s,'a1',day='2026-09-30')
    return [p['selection_key'] for p in picks]


def submit(engine, keys, mode='basic'):
    return gen.submit_many(engine,dict(selection_keys=keys,mode=mode))['items']


def run(engine, root):
    worker=TaskWorker(engine,root);task=worker.service.claim();assert task
    worker.execute(task);return worker.service.get(task.task_id)


def test_batch_commits_before_worker_and_fixes_source_version(engine, chosen, tmp_path):
    items=submit(engine,chosen);assert len(items)==2
    with Session(engine) as s:
        assert not s.exec(select(ScriptPackage)).all()
        a=s.exec(select(Article).where(Article.article_key=='a0')).one();a.content='之后的更正，不能替换已提交资料。';s.add(a);record_version(s,a);s.commit()
    for item in items:
        assert run(engine,tmp_path)['status']=='succeeded'
        d=gen.detail(engine,item['task']['task_id'])
        assert d['source']['revision']==1 and d['task']['kind']=='daily_script'
        with Session(engine) as s:
            script=wb.script_detail(s,d['package_key']);assert script['status']=='draft'
            assert script['document']['source_keys']==[d['source']['article_key']]
            assert '之后的更正' not in script['script_text']
            assert '原始报道' in script['script_text']
            assert s.get(DailySelection,item['selection_key']).package_key==d['package_key']
    again=submit(engine,chosen,'llm');assert all(i['reused'] and i['package_key'] for i in again)
    assert [i['package_key'] for i in again]==[gen.detail(engine,i['task']['task_id'])['package_key'] for i in items]


def test_batch_invalid_pick_rolls_back_all_tasks(engine,chosen):
    with pytest.raises(KeyError):submit(engine,[chosen[0],'missing'])
    with Session(engine) as s:
        assert not s.exec(select(TaskRecord)).all() and not s.exec(select(DailyScriptGeneration)).all()
    for keys in [[],chosen*2,chosen*11]:
        with pytest.raises(ValueError):submit(engine,keys)


def test_concurrent_submit_has_one_task_per_pick_and_mode_conflicts(engine,chosen):
    with ThreadPoolExecutor(2) as pool:
        one,two=list(pool.map(lambda _:submit(engine,chosen),range(2)))
    assert [r['task']['task_id'] for r in one]==[r['task']['task_id'] for r in two]
    with pytest.raises(TaskConflict):submit(engine,[chosen[0]],'llm')
    with Session(engine) as s:assert len(s.exec(select(TaskRecord)).all())==2


def test_model_failure_redacts_error_and_retry_uses_same_request(engine,chosen,tmp_path,monkeypatch):
    import requests
    monkeypatch.setenv('OPENAI_API_KEY','secret-should-not-appear')
    tid=submit(engine,[chosen[0]],'llm')[0]['task']['task_id']
    post=Mock(side_effect=requests.ConnectionError('secret-should-not-appear'))
    monkeypatch.setattr(requests,'post',post)
    failed=run(engine,tmp_path);assert failed['status']=='failed' and 'secret-' not in failed['error_text']
    with Session(engine) as s:
        assert 'secret-should-not-appear' not in s.get(DailyScriptGeneration,tid).spec_json
        assert not s.exec(select(ScriptPackage)).all()
    reply=dict(title='中文初稿',script_text='基于所选资料的中文口播。',visual_notes='来源卡',notes='待核对。')
    post.side_effect=None;post.return_value=Mock(json=lambda:{'choices':[{'message':{'content':json.dumps(reply)}}]})
    TaskService(engine).retry(tid);assert run(engine,tmp_path)['status']=='succeeded'
    assert post.call_count==2 and '所选报道1' not in post.call_args.kwargs['json']['messages'][1]['content']
    assert gen.detail(engine,tid)['package_key'] and not gen.detail(engine,tid)['can_retry']


def test_changed_model_target_never_receives_key(engine,chosen,tmp_path,monkeypatch):
    import requests
    monkeypatch.setenv('OPENAI_BASE_URL','http://localhost:19001/v1')
    tid=submit(engine,[chosen[0]],'llm')[0]['task']['task_id']
    monkeypatch.setenv('OPENAI_BASE_URL','http://localhost:19002/v1')
    post=Mock();monkeypatch.setattr(requests,'post',post)
    result=run(engine,tmp_path);assert result['status']=='failed' and '模型地址' in result['error_text'];post.assert_not_called()
    assert gen.detail(engine,tid)['can_retry']


def test_remove_reselect_while_model_pending_cancels_and_blocks_late_write(engine,chosen,tmp_path,monkeypatch):
    tid=submit(engine,[chosen[0]])[0]['task']['task_id']
    original=daily.draft_document
    def generate(*a,**kw):
        with Session(engine) as s:
            daily.choose(s,'a0',day='2026-09-30',active=False)
            daily.choose(s,'a0',day='2026-09-30',active=True)
        return original(*a,**kw)
    monkeypatch.setattr(daily,'draft_document',generate)
    assert run(engine,tmp_path)['status']=='cancelled'
    assert not gen.detail(engine,tid)['current']
    with pytest.raises(TaskConflict):TaskService(engine).retry(tid)
    with Session(engine) as s:assert not s.exec(select(ScriptPackage)).all()
    monkeypatch.setattr(daily,'draft_document',original)
    fresh=submit(engine,[chosen[0]])[0]['task']['task_id'];assert fresh!=tid
    assert run(engine,tmp_path)['status']=='succeeded'


def test_manual_cancel_can_retry_or_switch_mode_and_old_task_is_fenced(engine,chosen,tmp_path):
    tid=submit(engine,[chosen[0]],'llm')[0]['task']['task_id'];svc=TaskService(engine)
    svc.cancel(tid);assert gen.detail(engine,tid)['can_retry']
    svc.retry(tid);assert svc.get(tid)['status']=='queued';svc.cancel(tid)
    new=submit(engine,[chosen[0]],'basic')[0]['task']['task_id'];assert new!=tid
    with pytest.raises(TaskConflict):svc.retry(tid)
    assert run(engine,tmp_path)['status']=='succeeded'


def test_partial_files_roll_back_then_checkpoint_skips_regeneration(engine,chosen,tmp_path,monkeypatch):
    tid=submit(engine,[chosen[0]])[0]['task']['task_id']
    draft=Mock(wraps=daily.draft_document);monkeypatch.setattr(daily,'draft_document',draft)
    original=wb.initialize_script
    def fail(*a,**kw):
        original(*a,**kw)
        raise OSError('simulated write failure')
    monkeypatch.setattr(wb,'initialize_script',fail)
    assert run(engine,tmp_path)['status']=='failed'
    with Session(engine) as s:
        assert not s.exec(select(ScriptPackage)).all()
        assert s.get(DailySelection,chosen[0]).package_key is None
        assert s.get(TaskCheckpoint,tid+':daily_document')
    assert not list(tmp_path.rglob('script.txt'))
    monkeypatch.setattr(wb,'initialize_script',original)
    TaskService(engine).retry(tid);assert run(engine,tmp_path)['status']=='succeeded';assert draft.call_count==1


@pytest.mark.parametrize('target',['input','checkpoint'])
def test_corrupt_input_or_checkpoint_cannot_create_draft(engine,chosen,tmp_path,monkeypatch,target):
    tid=submit(engine,[chosen[0]])[0]['task']['task_id']
    if target=='checkpoint':
        original=wb.initialize_script
        monkeypatch.setattr(wb,'initialize_script',Mock(side_effect=OSError('stop after checkpoint')))
        assert run(engine,tmp_path)['status']=='failed'
        monkeypatch.setattr(wb,'initialize_script',original)
        with Session(engine) as s:
            row=s.get(TaskCheckpoint,tid+':daily_document');data=json.loads(row.payload_json);data['document']['script_text']='tampered';row.payload_json=json.dumps(data);s.add(row);s.commit()
        TaskService(engine).retry(tid)
    else:
        with Session(engine) as s:
            row=s.get(DailyScriptGeneration,tid);data=json.loads(row.spec_json);data['mode']='llm';row.spec_json=json.dumps(data);s.add(row);s.commit()
    assert run(engine,tmp_path)['status']=='failed'
    with Session(engine) as s:assert not s.exec(select(ScriptPackage)).all()


def test_lost_lease_worker_cannot_save_late_result(engine,chosen,tmp_path,monkeypatch):
    tid=submit(engine,[chosen[0]])[0]['task']['task_id'];original=daily.draft_document
    def expired(*a,**kw):
        with Session(engine) as s:
            row=s.get(TaskRecord,tid);row.lease_until=time.time()-1;s.add(row);s.commit()
        TaskService(engine).recover()
        return original(*a,**kw)
    monkeypatch.setattr(daily,'draft_document',expired)
    assert run(engine,tmp_path)['status']=='queued'
    with Session(engine) as s:assert not s.exec(select(ScriptPackage)).all()
    monkeypatch.setattr(daily,'draft_document',original)
    assert run(engine,tmp_path)['status']=='succeeded'


def test_legacy_build_does_not_compete_with_background(engine,chosen,tmp_path):
    submit(engine,[chosen[0]])
    with Session(engine) as s:
        with pytest.raises(wb.ScriptConflict):daily.build_pick(s,chosen[0],storage_root=tmp_path)
    assert run(engine,tmp_path)['status']=='succeeded'
    with Session(engine) as s:assert daily.build_pick(s,chosen[0],storage_root=tmp_path)['reused']


def test_new_service_instance_recovers_pending_daily_task(engine,chosen,tmp_path):
    tid=submit(engine,[chosen[0]])[0]['task']['task_id']
    from news2douyin.storage.db import make_engine,init_db
    new=make_engine(str(engine.url));init_db(new)
    assert run(new,tmp_path)['status']=='succeeded'
    with Session(new) as s:
        assert daily.list_picks(s,'2026-09-30')[0]['package_key']==gen.detail(new,tid)['package_key']
    new.dispose()


def test_http_batch_progress_selection_history_and_validation(tmp_path):
    app=create_app(db_url=f'sqlite:///{tmp_path}/http.db',storage_root=str(tmp_path/'runs'));engine=app.state.engine
    with Session(engine) as s:
        s.add_all([Article(article_key='a',title='HTTP 初稿',content='HTTP测试新闻正文。'),Event(event_key='e',event_title='HTTP 事件'),ArticleEventLink(article_key='a',event_key='e')]);s.commit()
    client=TestClient(app)
    key=client.post('/api/daily/selections',json=dict(article_key='a',day='2026-09-30')).json()[0]['selection_key']
    res=client.post('/api/daily/script-tasks',json=dict(selection_keys=[key],mode='basic'));assert res.status_code==202
    tid=res.json()['items'][0]['task']['task_id']
    assert client.post('/api/daily/selections/'+key+'/script-task',json=dict(mode='basic')).json()['items'][0]['task']['task_id']==tid
    assert client.post('/api/daily/selections/'+key+'/build',json=dict(mode='basic')).status_code==409
    assert client.post('/api/daily/script-tasks',json=dict(selection_keys=[key],mode='llm')).status_code==409
    assert client.get('/api/daily/script-tasks?day=2026-09-30').json()[0]['task']['task_id']==tid
    assert client.get('/api/daily/script-tasks?day=2026-10-01').json()==[]
    for path in ['/daily?day=2026-09-30','/scripts','/tasks','/tasks/'+tid]:assert client.get(path).status_code==200
    assert '/diagnostics' not in client.get('/tasks/'+tid).text
    assert run(engine,tmp_path/'runs')['status']=='succeeded'
    assert client.get('/api/daily/selections?day=2026-09-30').json()[0]['package_key']
    assert client.post('/api/daily/script-tasks',json=dict(selection_keys=[key])).status_code==200
    assert client.get('/api/daily/script-tasks?day=bad-date').status_code==422
    assert client.get('/api/daily/script-tasks/absent').status_code==404
    assert client.post('/api/daily/script-tasks',json=dict(selection_keys=[key,key])).status_code==422
    assert client.post('/api/daily/script-tasks',json=dict(selection_keys=[key],api_key='secret')).status_code==422
    client.post('/api/daily/selections',json=dict(article_key='a',day='2026-09-30',active=False))
    result=client.get('/api/daily/script-tasks/'+tid).json();assert result['script_url'] and not result['current']
    engine.dispose()


def test_background_submission_fences_already_running_legacy_request(engine,chosen,tmp_path,monkeypatch):
    original=daily.draft_document
    def during_legacy(*a,**kw):
        submit(engine,[chosen[0]])
        return original(*a,**kw)
    monkeypatch.setattr(daily,'draft_document',during_legacy)
    with Session(engine) as s:
        with pytest.raises(wb.ScriptConflict):daily.build_pick(s,chosen[0],storage_root=tmp_path)
    with Session(engine) as s:assert not s.exec(select(ScriptPackage)).all()
    monkeypatch.setattr(daily,'draft_document',original)
    assert run(engine,tmp_path)['status']=='succeeded'
