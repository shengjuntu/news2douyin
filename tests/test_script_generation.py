import copy
import json
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.editorial import generation as gen
from news2douyin.events import workbench as events
from news2douyin.events.schemas import MomentEdit
from news2douyin.storage.articles import record_version, latest_version, lock_news
from news2douyin.storage.models import (Article, ArticleEventLink, Event, ScriptGeneration,
    ScriptPackage, TaskCheckpoint, TaskRecord)
from news2douyin.tasks.service import TaskService
from news2douyin.tasks.worker import TaskWorker
from news2douyin.tasks.control import TaskConflict
from news2douyin.server.app import create_app
from news2douyin.video import workbench as scripts


@pytest.fixture
def curated(engine):
    with Session(engine) as s:
        article = Article(article_key='source', title='离线测试：研发计划公布',
            content='测试团队公布研发计划，计划安排三组实验。后续实验日期尚不明确。',
            source_domain='fixture.test', url='https://fixture.test/plan', published_at='2026-09-02T01:00:00Z')
        s.add_all([article, Event(event_key='topic', event_title='研发进展')]); s.flush()
        record_version(s, article)
        s.add(ArticleEventLink(article_key='source', event_key='topic')); s.commit()
        view = events.workspace(s, 'topic'); source = view['sources'][0]
        data = MomentEdit(expected_version=view['version'], title='计划公布', description='计划安排三组实验。',
            time_kind='occurred', date_start='2026-09-01', certainty='exact', reviewed=True,
            sources=[{k: source[k] for k in ('article_key', 'revision', 'content_hash', 'excerpt')}]).model_dump()
        view = events.save_moment(s, 'topic', data)
        data.update(expected_version=view['version'], title='后续日期待确认', description='后续实验日期尚不明确。',
                    time_kind='unknown', date_start='', certainty='unknown', reviewed=False)
        view = events.save_moment(s, 'topic', data)
    return engine


def request(engine, **overrides):
    with Session(engine) as s:
        view = events.workspace(s, 'topic')
    data = dict(expected_version=view['version'], moment_keys=[m['moment_key'] for m in view['moments'] if m['reviewed']],
                options=dict(backend='outline', mode='recap', style='neutral', duration_sec=90), idempotency_key='one')
    data.update(overrides)
    return data


def execute(engine, tmp_path):
    worker = TaskWorker(engine, tmp_path)
    task = worker.service.claim()
    assert task
    worker.execute(task)
    return worker.service.get(task.task_id)


def frozen(engine, task_id):
    with Session(engine) as s:
        return json.loads(s.get(ScriptGeneration, task_id).spec_json)


def test_requires_reviewed_selected_current_nodes(curated):
    data = request(curated)
    with Session(curated) as s:
        unknown = next(m for m in events.workspace(s, 'topic')['moments'] if not m['reviewed'])
    for change in [dict(moment_keys=[]), dict(moment_keys=['missing']), dict(moment_keys=[unknown['moment_key']]),
                   dict(moment_keys=data['moment_keys'] * 2)]:
        with pytest.raises(ValueError): gen.submit(curated, 'topic', data | change)
    with pytest.raises(TaskConflict): gen.submit(curated, 'topic', data | {'expected_version': 0})
    with Session(curated) as s:
        assert not list(s.exec(select(ScriptGeneration)))


def test_idempotency_concurrent_and_changed_request(curated):
    data = request(curated)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: gen.submit(curated, 'topic', data), range(2)))
    assert first['task_id'] == second['task_id'] and first['kind'] == 'script'
    with pytest.raises(TaskConflict):
        gen.submit(curated, 'topic', data | {'options': {'mode': 'brief'}})
    with Session(curated) as s:
        assert len(list(s.exec(select(TaskRecord)))) == 1


@pytest.mark.parametrize('mode', ['brief', 'explain', 'recap'])
def test_worker_creates_independent_versioned_evidence_drafts(curated, tmp_path, mode):
    data = request(curated, options=dict(mode=mode, backend='outline', duration_sec=60, style='plain'))
    task = gen.submit(curated, 'topic', data)
    assert execute(curated, tmp_path)['status'] == 'succeeded'
    result = gen.detail(curated, task['task_id'])
    with Session(curated) as s:
        script = scripts.script_detail(s, result['package_key'])
        assert script['status'] == 'draft' and script['revision'] == 1
        assert script['document']['segments'][0]['evidence_ids'] == ['E1']
        assert script['document']['target_duration_sec'] == 60
        assert '发生日期：2026-09-01' in script['document']['script_text']
        assert '报道日期：2026-09-02' not in script['document']['script_text']
        assert script['editorial']['evidence']['source_versions'][0]['document']['content']
        assert Path(script['output_dir'], 'evidence_snapshot.json').exists()
    assert gen.submit(curated, 'topic', data)['task_id'] == task['task_id']
    next_task = gen.submit(curated, 'topic', data | {'idempotency_key': 'two'})
    assert execute(curated, tmp_path)['status'] == 'succeeded'
    assert gen.detail(curated, next_task['task_id'])['package_key'] != result['package_key']
    with Session(curated) as s:
        assert scripts.script_detail(s, result['package_key']) == script


def test_two_versions_one_article_and_event_changes_do_not_change_snapshot(curated, tmp_path):
    with Session(curated) as s:
        lock_news(s)
        article = s.exec(select(Article)).one(); article.content = '更正：计划包含两组实验，开始日期有争议。'
        s.add(article); record_version(s, article); s.commit()
        view = events.workspace(s, 'topic'); source = view['sources'][0]
        data = MomentEdit(expected_version=view['version'], title='计划更正', description=article.content,
            time_kind='reported', date_start='2026-09-03', certainty='disputed', time_note='日期来自不同版本，尚待核实', reviewed=True,
            sources=[{k: source[k] for k in ('article_key','revision','content_hash','excerpt')}]).model_dump()
        events.save_moment(s, 'topic', data)
    task = gen.submit(curated, 'topic', request(curated))
    spec = frozen(curated, task['task_id'])
    assert [s['article_revision'] for s in spec['evidence']['sources']] == [1, 2]
    assert spec['evidence']['moments'][1]['certainty'] == 'disputed'
    with Session(curated) as s:
        view = events.workspace(s, 'topic')
        events.delete_moment(s, 'topic', view['moments'][0]['moment_key'], view['version'])
    assert execute(curated, tmp_path)['status'] == 'succeeded'
    with Session(curated) as s:
        result = scripts.script_detail(s, gen.detail(curated, task['task_id'])['package_key'])
    assert result['document']['source_keys'] == ['source']
    assert '有争议' in result['document']['script_text']
    assert result['editorial']['evidence'] == spec['evidence']


def test_ai_receives_only_frozen_evidence_and_analysis_label(curated, tmp_path, monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'not-in-snapshot-secret')
    task = gen.submit(curated, 'topic', request(curated, options=dict(backend='llm',mode='explain')))
    spec = frozen(curated, task['task_id']); value = gen.outline(spec)
    value['segments'].append(dict(value['segments'][0], segment_key='interpretation', kind='analysis', text='仅凭目前材料，还不能判断后续实验何时完成。'))
    post = Mock(return_value=Mock(json=lambda: {'choices':[{'message':{'content':json.dumps(value)}}]}))
    monkeypatch.setattr(gen.requests, 'post', post)
    assert execute(curated, tmp_path)['status'] == 'succeeded'
    sent = json.loads(post.call_args.kwargs['json']['messages'][1]['content'])
    assert len(sent['moments']) == 1 and len(sent['sources']) == 1
    assert 'not-in-snapshot-secret' not in json.dumps(spec)
    with Session(curated) as s:
        script = scripts.script_detail(s, gen.detail(curated, task['task_id'])['package_key'])
    assert '\n\n分析：仅凭目前材料' in script['document']['script_text']
    assert script['document']['segments'][-1]['kind'] == 'analysis'
    assert script['editorial']['generation']['backend'] == 'llm'


@pytest.mark.parametrize('fault', ['unknown_source', 'unknown_node', 'no_source', 'duplicate_part', 'extra_field'])
def test_invalid_model_response_never_creates_draft(curated, tmp_path, monkeypatch, fault):
    task = gen.submit(curated, 'topic', request(curated, options={'backend':'llm'}))
    value = gen.outline(frozen(curated, task['task_id']))
    if fault == 'unknown_source': value['segments'][0]['evidence_ids'] = ['invented']
    if fault == 'unknown_node': value['segments'][0]['moment_keys'] = ['invented']
    if fault == 'no_source': value['segments'][0]['evidence_ids'] = []
    if fault == 'duplicate_part': value['segments'].append(value['segments'][0])
    if fault == 'extra_field': value['untrusted'] = 'do not echo this content'
    monkeypatch.setattr(gen, 'model_draft', lambda spec: value)
    result = execute(curated, tmp_path)
    assert result['status'] == 'failed' and '引用校验' in result['error_text']
    assert 'do not echo' not in result['error_text']
    assert gen.detail(curated, task['task_id'])['package_key'] is None
    with Session(curated) as s:
        assert not list(s.exec(select(ScriptPackage)))


def test_model_network_failure_is_sanitized_and_no_silent_fallback(curated, tmp_path, monkeypatch):
    task = gen.submit(curated, 'topic', request(curated, options={'backend':'llm'}))
    monkeypatch.setattr(gen.requests, 'post', Mock(side_effect=RuntimeError('secret-token and private endpoint')))
    result = execute(curated, tmp_path)
    assert result['status'] == 'failed' and '未改用提纲' in result['error_text']
    assert 'secret-token' not in result['error_text']
    assert gen.detail(curated, task['task_id'])['package_key'] is None


def test_retry_reuses_validated_result_and_commits_one_draft(curated, tmp_path, monkeypatch):
    task = gen.submit(curated, 'topic', request(curated, options={'backend':'llm'}))
    model = Mock(side_effect=lambda spec: gen.outline(spec)); monkeypatch.setattr(gen, 'model_draft', model)
    original = scripts.initialize_script
    calls = []
    def fail_after_files(*args, **kwargs):
        folder = original(*args, **kwargs); calls.append(folder)
        raise OSError('simulated disk error before commit')
    monkeypatch.setattr(scripts, 'initialize_script', fail_after_files)
    assert execute(curated, tmp_path)['status'] == 'failed'
    assert not calls[0].exists()
    with Session(curated) as s:
        assert not list(s.exec(select(ScriptPackage)))
        assert s.get(TaskCheckpoint, task['task_id']+':script_document')
    monkeypatch.setattr(scripts, 'initialize_script', original)
    TaskService(curated).retry(task['task_id'])
    assert execute(curated, tmp_path)['status'] == 'succeeded'
    assert model.call_count == 1
    with Session(curated) as s:
        assert len(list(s.exec(select(ScriptPackage)))) == 1
        assert s.get(ScriptGeneration, task['task_id']).package_key
        assert s.get(TaskRecord, task['task_id']).status == 'succeeded'


def test_cancel_during_model_call_discards_late_result(curated, tmp_path, monkeypatch):
    task = gen.submit(curated, 'topic', request(curated, options={'backend':'llm'}))
    def cancelled(spec):
        TaskService(curated).cancel(task['task_id'])
        return gen.outline(spec)
    monkeypatch.setattr(gen, 'model_draft', cancelled)
    assert execute(curated, tmp_path)['status'] == 'cancelled'
    assert gen.detail(curated, task['task_id'])['package_key'] is None
    with Session(curated) as s:
        assert not s.get(TaskCheckpoint, task['task_id']+':script_document')


def test_expired_owner_cannot_save_and_recovery_uses_same_snapshot(curated, tmp_path, monkeypatch):
    task = gen.submit(curated, 'topic', request(curated, options={'backend':'llm'}))
    original_spec = frozen(curated, task['task_id'])
    def expire(spec):
        with Session(curated) as s:
            row=s.get(TaskRecord,task['task_id']);row.lease_until=time.time()-1;s.add(row);s.commit()
        TaskService(curated).recover()
        return gen.outline(spec)
    monkeypatch.setattr(gen, 'model_draft', expire)
    assert execute(curated, tmp_path)['status'] == 'queued'
    assert gen.detail(curated, task['task_id'])['package_key'] is None
    monkeypatch.setattr(gen, 'model_draft', gen.outline)
    assert execute(curated, tmp_path)['status'] == 'succeeded'
    assert frozen(curated, task['task_id']) == original_spec


def test_segment_edit_validation_versions_restore_and_export(curated, tmp_path):
    task = gen.submit(curated, 'topic', request(curated)); execute(curated, tmp_path)
    key = gen.detail(curated, task['task_id'])['package_key']
    with Session(curated) as s:
        first = scripts.script_detail(s, key); original = copy.deepcopy(first['document'])
        for change in [dict(script_text='与分段不一致'), dict(segments=[]), dict(source_keys=[])]:
            with pytest.raises(ValueError): scripts.save_script(s, key, expected_version=first['version'], document=original | change)
        doc=copy.deepcopy(original);doc['segments'][0]['visual']='对应来源截图与日期卡';doc['segments'][0]['assets']='需要研发计划原文截图'
        doc['segments'][0]['kind']='analysis';doc['script_text']=scripts.segment_narration(doc['segments'])
        second=scripts.save_script(s,key,expected_version=first['version'],document=doc)
        assert second['revision']==2 and second['status']=='draft'
        with pytest.raises(scripts.ScriptConflict): scripts.save_script(s,key,expected_version=first['version'],document=original)
        second=scripts.review_script(s,key,expected_version=second['version'],action='submit')
        second=scripts.review_script(s,key,expected_version=second['version'],action='approve',checks={'sources_checked':True,'wording_checked':True})
        result=scripts.export_script(s,key,expected_version=second['version'])
        _,content=scripts.read_export(s,key,result['export_key'])
        with zipfile.ZipFile(BytesIO(content)) as z:
            assert json.loads(z.read('storyboard.json'))==doc['segments']
            assert json.loads(z.read('evidence_snapshot.json'))==first['editorial']['evidence']
            assert json.loads(z.read('assets_manifest.json'))['storyboard'][0]['assets']=='需要研发计划原文截图'
            assert z.read('script.txt').decode()==doc['script_text']
        restored=scripts.save_script(s,key,expected_version=second['version'],document=None,restore_revision=1)
        assert restored['revision']==3 and restored['document']==original and restored['status']=='draft'
        assert scripts.get_revision(s,key,2)


def test_generation_http_routes_and_templates(curated, tmp_path):
    app=create_app(db_url=str(curated.url),storage_root=str(tmp_path/'runs'))
    client=TestClient(app)
    data=request(curated)
    response=client.post('/api/events/topic/script-tasks',json=data)
    assert response.status_code==202,response.text
    tid=response.json()['task_id'];assert client.get(response.headers['Location']).status_code==200
    assert client.get('/events/topic/generate').status_code==200
    assert client.get('/tasks/'+tid).status_code==200
    assert client.get('/scripts').status_code==200
    assert client.post('/api/events/topic/script-tasks',json=data|{'moment_keys':[]}).status_code==422
    execute(curated,tmp_path)
    item=client.get('/api/script-tasks/'+tid).json();assert client.get(item['script_url']).status_code==200
    assert client.get('/api/script-tasks').json()[0]['package_key']==item['package_key']
    assert client.get('/api/script-tasks/missing').status_code==404
    app.state.engine.dispose()


def test_changed_model_target_is_blocked_before_sending_credentials(curated, tmp_path, monkeypatch):
    task=gen.submit(curated,'topic',request(curated,options={'backend':'llm'}))
    monkeypatch.setenv('OPENAI_BASE_URL','https://different.example.test/v1')
    post=Mock();monkeypatch.setattr(gen.requests,'post',post)
    result=execute(curated,tmp_path)
    assert result['status']=='failed' and '地址或型号已改变' in result['error_text']
    assert not post.called and gen.detail(curated,task['task_id'])['package_key'] is None


def test_citation_must_belong_to_its_selected_node(curated):
    task=gen.submit(curated,'topic',request(curated));spec=frozen(curated,task['task_id'])
    extra=copy.deepcopy(spec['evidence']['sources'][0]);extra['evidence_id']='E2';extra['article_key']='other'
    spec['evidence']['sources'].append(extra)
    value=gen.outline(spec);value['segments'][0]['evidence_ids']=['E2']
    with pytest.raises(ValueError,match='对应所选节点'):gen.validate_draft(spec,value)
