"""0.9 installed resources and optional real 0.8-to-0.9 upgrade. Run with python -I."""
import argparse
import json
from importlib.metadata import version
from io import BytesIO
from pathlib import Path
import tempfile
import zipfile
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.editorial import generation
from news2douyin.events import workbench as events
from news2douyin.events.schemas import MomentEdit
from news2douyin.server import webui
from news2douyin.server.app import create_app
from news2douyin.storage.articles import record_version
from news2douyin.storage.models import Article, ArticleEventLink, Event, DailySelection, TaskRecord
from news2douyin.tasks.service import TaskService
from news2douyin.tasks.worker import TaskWorker
from news2douyin.video import workbench as scripts


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--upgrade-fixture',default='');args=parser.parse_args()
    assert tuple(map(int,version('news2douyin').split('.')[:3])) >= (0,9,0)
    checks=[]
    with tempfile.TemporaryDirectory(prefix='scripts-wheel-') as directory,patch.object(webui,'_llm_alive',return_value=False):
        app=create_app(db_url=f'sqlite:///{directory}/app.db',storage_root=str(Path(directory)/'runs'));client=TestClient(app)
        engine=app.state.engine
        with Session(engine) as s:
            row=Article(article_key='fixture',title='虚构资料：实验计划',content='虚构团队于九月一日公布实验计划。此资料仅用于测试。',url='https://example.com/fixture',source_domain='example.com',published_at='2026-09-02T01:00:00Z')
            s.add_all([row,Event(event_key='topic',event_title='安装验证专题')]);s.flush();record_version(s,row)
            s.add(ArticleEventLink(article_key='fixture',event_key='topic'));s.commit()
            view=events.workspace(s,'topic');source=view['sources'][0]
            view=events.save_moment(s,'topic',MomentEdit(expected_version=view['version'],title='实验计划公布',reviewed=True,time_kind='occurred',date_start='2026-09-01',certainty='exact',sources=[{k:source[k] for k in ('article_key','revision','content_hash','excerpt')}]).model_dump())
        assert client.get('/events/topic/generate').status_code==200
        request=dict(expected_version=view['version'],moment_keys=[view['moments'][0]['moment_key']],options=dict(mode='recap',style='plain',duration_sec=90,backend='outline'),idempotency_key='installed')
        response=client.post('/api/events/topic/script-tasks',json=request);assert response.status_code==202,response.text
        task=response.json();assert task['kind']=='script'
        checks.append('installed_generation_template_and_enqueue')
        assert client.post('/api/events/topic/script-tasks',json=request).json()['task_id']==task['task_id']
        worker=TaskWorker(engine,Path(directory)/'runs');worker.execute(worker.service.claim())
        result=client.get('/api/script-tasks/'+task['task_id']).json();assert result['task']['status']=='succeeded'
        assert client.get(result['script_url']).status_code==200
        assert client.get('/scripts').status_code==200 and client.get('/tasks/'+task['task_id']).status_code==200
        checks.append('installed_worker_idempotency_and_result_pages')
        key=result['package_key'];base='/api/scripts/'+key;script=client.get(base).json()
        doc=script['document'];doc['segments'][0]['kind']='analysis';doc['segments'][0]['text']='仅凭目前资料，尚不能判断实验何时完成。'
        doc['segments'][0]['assets']='需要计划原文摘录';doc['script_text']=scripts.segment_narration(doc['segments'])
        response=client.put(base,json={'expected_version':script['version'],'document':doc});assert response.status_code==200,response.text
        script=response.json();assert script['revision']==2
        script=client.post(base+'/review',json={'expected_version':script['version'],'action':'submit'}).json()
        script=client.post(base+'/review',json={'expected_version':script['version'],'action':'approve','sources_checked':True,'wording_checked':True}).json()
        exported=client.post(base+'/exports',json={'expected_version':script['version']}).json()
        with zipfile.ZipFile(BytesIO(client.get(exported['download_url']).content)) as z:
            assert json.loads(z.read('storyboard.json'))[0]['assets']=='需要计划原文摘录'
            assert json.loads(z.read('evidence_snapshot.json'))['source_versions'][0]['document']['content']==source['excerpt']
            assert '来源：E1' in z.read('script.md').decode()
            assert z.read('script.txt').decode().startswith('分析：')
        checks.append('installed_segment_edit_review_and_cited_exports')
        assert client.get('/scripts/'+key+'/video').status_code==200
        bad=client.put(base,json={'expected_version':script['version'],'document':dict(doc,script_text='独立正文与分段不一致')})
        assert bad.status_code==422
        checks.append('installed_video_handoff_and_narration_consistency_guard')
        # Persist a failed task, close/reopen the app DB, then retry with the same pinned input.
        request['idempotency_key']='recovery';request['options']['backend']='llm'
        failed=client.post('/api/events/topic/script-tasks',json=request).json()
        with patch.object(generation,'model_draft',side_effect=ValueError('offline simulated failure')):
            worker.execute(worker.service.claim())
        assert client.get('/api/script-tasks/'+failed['task_id']).json()['task']['status']=='failed'
        engine.dispose()
        app=create_app(db_url=f'sqlite:///{directory}/app.db',storage_root=str(Path(directory)/'runs'));client=TestClient(app)
        assert client.post('/api/tasks/'+failed['task_id']+'/retry').status_code==202
        worker=TaskWorker(app.state.engine,Path(directory)/'runs')
        with patch.object(generation,'model_draft',side_effect=generation.outline):worker.execute(worker.service.claim())
        recovered=client.get('/api/script-tasks/'+failed['task_id']).json();assert recovered['task']['status']=='succeeded' and recovered['package_key']!=key
        checks.append('installed_restart_retry_preserves_task_and_creates_separate_draft')
        app.state.engine.dispose()
    if args.upgrade_fixture:
        root=Path(args.upgrade_fixture);expected=json.loads((root/'expected.json').read_text());assert expected['from_version']=='0.8.0'
        app=create_app(db_url='sqlite:///'+str(root/'old.db'),storage_root=str(root/'runs'));client=TestClient(app)
        assert not client.get('/api/profiles/legacy_mock').json()['enabled']
        assert not client.get('/api/jobs').json()[0]['enabled']
        assert client.get('/api/tasks/'+expected['trial_id']+'/diagnostics').json()['stats']['dry_run']
        with Session(app.state.engine) as s:
            assert s.get(DailySelection,expected['selection_key']).package_key==expected['package_key']
            assert s.get(TaskRecord,expected['task_id']).status=='succeeded'
            assert scripts.script_detail(s,expected['package_key'])==expected['script']
        checks.append('upgrade_preserves_profiles_schedules_news_picks_tasks_and_old_draft')
        view=client.get('/api/events/'+expected['event_key']+'/workspace').json()
        assert view['moments']==expected['moments'] and view['version']==expected['event_version']
        response=client.post('/api/events/'+expected['event_key']+'/script-tasks',json=dict(expected_version=view['version'],moment_keys=[m['moment_key'] for m in view['moments']],options={'mode':'recap'},idempotency_key='upgrade'))
        assert response.status_code==202,response.text
        worker=TaskWorker(app.state.engine,root/'runs');worker.execute(worker.service.claim())
        result=client.get('/api/script-tasks/'+response.json()['task_id']).json();assert result['task']['status']=='succeeded'
        assert result['package_key']!=expected['package_key']
        with Session(app.state.engine) as s:
            new=scripts.script_detail(s,result['package_key'])
            assert new['sources'][0]['article_content_hash']==expected['moments'][0]['sources'][0]['content_hash']
            assert scripts.script_detail(s,expected['package_key'])==expected['script']
        checks.append('upgrade_uses_existing_reviewed_nodes_and_keeps_old_sources_and_drafts')
        app.state.engine.dispose()
    print(json.dumps({'status':'passed','version':version('news2douyin'),'passed':len(checks),'checks':checks}))


if __name__=='__main__':main()
