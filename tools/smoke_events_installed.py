"""0.8 installed wheel and optional 0.7 upgrade check. Run with python -I."""
import argparse
import json
from importlib.metadata import version
from pathlib import Path
import tempfile
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlmodel import Session,select
from news2douyin.events import research
from news2douyin.server import webui
from news2douyin.server.app import create_app
from news2douyin.storage.models import Article,ArticleVersion,DailySelection,TaskRecord
from news2douyin.storage.articles import record_version
from news2douyin.video.workbench import script_detail


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--upgrade-fixture',default='');args=parser.parse_args()
    assert tuple(map(int,version('news2douyin').split('.')[:3])) >= (0,8,0)
    checks=[]
    with tempfile.TemporaryDirectory(prefix='events-wheel-') as directory,patch.object(webui,'_llm_alive',return_value=False):
        app=create_app(db_url=f'sqlite:///{directory}/app.db',storage_root=str(Path(directory)/'runs'))
        client=TestClient(app)
        with Session(app.state.engine) as s:
            row=Article(article_key='fixture',title='离线新闻资料',content='这是一段虚构的原始报道，用于验证来源摘录。',url='https://example.com/fixture',source_domain='example.com',published_at='2026-09-02T01:00:00Z')
            s.add(row);s.flush();record_version(s,row);s.commit()
        view=client.post('/api/events',json={'title':'证据专题'}).json();key=view['event_key'];base='/api/events/'+key
        view=client.post(base+'/sources',json={'expected_version':view['version'],'article_keys':['fixture']}).json()
        source=view['sources'][0]
        node=dict(expected_version=view['version'],title='资料中的进展',time_kind='unknown',sources=[{k:source[k] for k in ('article_key','revision','content_hash','excerpt')}])
        response=client.post(base+'/moments',json=node);assert response.status_code==200,response.text
        view=response.json();assert len(view['moments'])==1
        assert client.get('/events/'+key).status_code==200
        assert client.get('/events').status_code==200
        checks.append('installed_event_editor_and_pinned_evidence')
        assert client.request('DELETE',base+'/sources/fixture',json={'expected_version':view['version']}).status_code==409
        bundle=client.get(base+'/evidence-export').json()
        assert bundle['source_versions'][0]['document']['content']==source['excerpt']
        checks.append('installed_removal_guard_and_full_export')
        item=dict(title='外部离线候选',content='虚构外部资料，用来验证检索后收录。',url='https://external.example.com/a',source={'domain':'external.example.com'},country='cn',language='zh',published_at='2026-09-03T00:00:00Z')
        with patch.object(research,'search_news',return_value={'items':[item],'available':1}):
            report=client.post(base+'/research',json={'query':'离线示例新闻'}).json()
        assert report['status']=='succeeded'
        view=client.post(base+'/research/'+report['research_key']+'/import',json={'expected_version':view['version'],'indexes':[0]}).json()
        assert len(view['sources'])==2
        checks.append('installed_research_preview_and_selected_import')
        extra=client.post('/api/events',json={'title':'合并目标'}).json()
        response=client.post(base+'/merge',json={'expected_version':view['version'],'target_event_key':extra['event_key'],'target_expected_version':extra['version'],'note':'测试合并'})
        assert response.status_code==200,response.text
        assert len(response.json()['workspace']['moments'])==1
        assert client.get(base+'/workspace').json()['merged_into']==extra['event_key']
        checks.append('installed_merge_preserves_node_sources')
        app.state.engine.dispose()
    if args.upgrade_fixture:
        root=Path(args.upgrade_fixture);expected=json.loads((root/'expected.json').read_text());assert expected['from_version']=='0.7.0'
        app=create_app(db_url='sqlite:///'+str(root/'old.db'),storage_root=str(root/'runs'));client=TestClient(app)
        assert not client.get('/api/profiles/legacy_mock').json()['enabled']
        assert not client.get('/api/jobs').json()[0]['enabled']
        assert client.get('/api/tasks/'+expected['trial_id']+'/diagnostics').json()['stats']['dry_run']
        checks.append('upgrade_preserves_strategy_state_schedule_and_trial')
        with Session(app.state.engine) as s:
            pick=s.get(DailySelection,expected['selection_key']);assert pick.package_key==expected['package_key']
            old_script=script_detail(s,expected['package_key'])
            assert old_script['sources'][0]['article_key']==expected['article_key']
            assert s.get(TaskRecord,expected['task_id']).status=='succeeded'
        checks.append('upgrade_preserves_news_selection_script_and_task')
        base='/api/events/'+expected['event_key'];view=client.get(base+'/workspace').json();assert view['version']==0
        source=next(a for a in view['sources'] if a['article_key']==expected['article_key'])
        response=client.post(base+'/moments',json={'expected_version':view['version'],'title':'升级后整理的进展','sources':[{k:source[k] for k in ('article_key','revision','content_hash','excerpt')}]})
        assert response.status_code==200,response.text
        assert client.get('/events/'+expected['event_key']).status_code==200
        assert len(client.get(base+'/evidence-export').json()['source_versions'])>=1
        with Session(app.state.engine) as s:
            assert script_detail(s,expected['package_key'])==old_script
        checks.append('upgrade_old_event_can_add_evidence_without_rewriting_script')
        app.state.engine.dispose()
    print(json.dumps({'status':'passed','version':version('news2douyin'),'passed':len(checks),'checks':checks}))


if __name__=='__main__':main()
