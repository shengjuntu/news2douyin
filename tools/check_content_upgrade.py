"""Verify a v0.19 wheel's database upgrades without rewriting saved evidence.

Usage: python tools/check_content_upgrade.py /path/news2douyin-0.19.0-py3-none-any.whl
Uses a temporary database, never an existing installation's database.
"""
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


SEED = r'''
import hashlib,json,os,sqlite3
from pathlib import Path
from sqlmodel import Session
from news2douyin.storage.db import make_engine,init_db
from news2douyin.storage.models import Article,Event,ArticleEventLink,ScriptPackage
from news2douyin.storage.articles import record_version
from news2douyin.topics.models import TopicWorkspace,TopicHandoff
from news2douyin.topics.service import freeze
from news2douyin.research.models import ResearchCase,ShortScriptBrief
from news2douyin.video.workbench import initialize_script
root=Path(os.environ['N2D_UPGRADE_QA']);engine=make_engine('sqlite:///'+str(root/'news.db'));init_db(engine)
with Session(engine) as s:
    a=Article(article_key='old_article',title='Old report',content='Frozen historical text.',url='https://example.com/old')
    s.add(a);record_version(s,a)
    s.add(Event(event_key='old_event',event_title='Old event'))
    s.add(ArticleEventLink(article_key='old_article',event_key='old_event'))
    for cid in ('selection_case','research_case','orphan_case'):
        s.add(ResearchCase(case_id=cid,title=cid,goal='History',strategy_json='{}',cutoff='2026-10-01T00:00:00Z'))
    w=TopicWorkspace(workspace_id='old_topic',request_hash='seed',case_id='selection_case',title='旧选题',angle='旧角度',revision=7,scope_json=json.dumps({'event_key':'old_event'}))
    s.add(w);s.flush();source=freeze(s,w.workspace_id,a.article_key);w.selected_json=json.dumps([source.source_id]);s.add(w)
    s.add(TopicHandoff(handoff_id='old_handoff',workspace_id=w.workspace_id,request_hash='seed',case_id='research_case',run_id='old_run',payload_json=json.dumps({'action':'short'})))
    s.add(ShortScriptBrief(case_id='research_case',request_key='old_request',request_hash='seed',options_json='{}',package_key='old_package'))
    package=ScriptPackage(package_key='old_package',event_key='old_event',profile_name='old',output_dir=str(root/'runs'/'old_package'),script_text='Old saved script')
    initialize_script(s,package,{'event_title':'Old script','sources':[]})
    s.commit()
db=sqlite3.connect(root/'news.db')
tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
assert 'contenttask' not in tables,'Input must be a pre-0.20 wheel'
checks={name:db.execute('SELECT * FROM '+name+' ORDER BY 1').fetchall() for name in ['articleversion','topicsource','scriptrevision','topicworkspace']}
(root/'expected.json').write_text(json.dumps(checks))
engine.dispose()
print('V019_DATABASE_SEEDED')
'''


VERIFY = r'''
import json,os,sqlite3
from pathlib import Path
from fastapi.testclient import TestClient
from sqlmodel import Session,select
from news2douyin.server.app import create_app
from news2douyin.workbench.models import ContentTask,ContentBinding
root=Path(os.environ['N2D_UPGRADE_QA'])
for _ in range(2):
    app=create_app(db_url='sqlite:///'+str(root/'news.db'),storage_root=str(root/'runs'))
    c=TestClient(app)
    task=c.get('/api/workbench/old_topic').json()
    assert task['title']=='旧选题' and task['phase']=='creation' and task['topic_revision']==7
    assert task['event']=='old_event' and task['sources'][0]['revision']==1
    assert [x['id'] for x in task['cases']]==['research_case']
    assert [x['id'] for x in task['scripts']]==['old_package']
    assert c.get('/scripts/old_package',follow_redirects=False).headers['location']=='/workbench/old_topic/creation?script=old_package'
    assert c.get('/workbench/old_topic/creation').status_code==200
    assert 'orphan_case' in c.get('/workbench?view=history').text
    with Session(app.state.engine) as s:
        assert len(s.exec(select(ContentTask)).all())==1
        assert len(s.exec(select(ContentBinding)).all())==2
    app.state.engine.dispose()
db=sqlite3.connect(root/'news.db')
checks=json.loads((root/'expected.json').read_text())
for name,rows in checks.items():
    assert json.loads(json.dumps(db.execute('SELECT * FROM '+name+' ORDER BY 1').fetchall()))==rows,name+' changed'
print('UPGRADE_PASS: task, research, script, two starts, frozen source/revision integrity')
'''


def main():
    wheel=Path(sys.argv[1]).resolve()
    if not wheel.is_file():raise SystemExit('Old wheel is required')
    with tempfile.TemporaryDirectory(prefix='n2d-upgrade-') as folder:
        root=Path(folder);old=root/'old';old.mkdir()
        with zipfile.ZipFile(wheel) as archive:
            for member in archive.infolist():
                dest=(old/member.filename).resolve()
                if not dest.is_relative_to(old):raise ValueError('unsafe wheel member')
            archive.extractall(old)
        env=dict(os.environ,N2D_UPGRADE_QA=str(root),NEWS2DOUYIN_EVENT_READER='rules',NEWS2DOUYIN_FALKOR_URL='')
        subprocess.run([sys.executable,'-c',SEED],check=True,cwd=root,env=dict(env,PYTHONPATH=str(old)))
        source=Path(__file__).resolve().parents[1]/'src'
        subprocess.run([sys.executable,'-c',VERIFY],check=True,cwd=root,env=dict(env,PYTHONPATH=str(source)))


if __name__=='__main__':main()
