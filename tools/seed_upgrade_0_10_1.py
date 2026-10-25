"""Seed real 0.10.1 queue, result and retry checkpoint before 0.10.2 installation."""
import argparse
import json
from importlib.metadata import version
from pathlib import Path
from unittest.mock import patch
from sqlmodel import Session,select
from news2douyin.server.app import create_app
from news2douyin.collect.service import create_or_update_profile
from news2douyin.editorial import daily,daily_tasks
from news2douyin.storage.models import Article,ArticleEventLink,Event,TaskRecord,TaskEvent,DailyScriptGeneration
from news2douyin.storage.articles import record_version
from news2douyin.tasks.worker import TaskWorker
from news2douyin.video import workbench as wb

assert version('news2douyin')=='0.10.1'
parser=argparse.ArgumentParser();parser.add_argument('directory');args=parser.parse_args()
root=Path(args.directory).resolve();root.mkdir(parents=True,exist_ok=False)
app=create_app(db_url=f'sqlite:///{root}/old.db',storage_root=str(root/'runs'))
with Session(app.state.engine) as s:
    create_or_update_profile(s,{'name':'old-profile','provider':'mock'})
    s.add(Event(event_key='old',event_title='旧版任务'))
    for i in range(2):
        a=Article(article_key=f'old{i}',title=f'旧版离线资料{i}',content='仅供升级测试的新闻摘录。')
        s.add(a);s.flush();record_version(s,a);s.add(ArticleEventLink(article_key=a.article_key,event_key='old'))
    s.commit();daily.choose(s,'old0',day='2026-09-30');picks=daily.choose(s,'old1',day='2026-09-30')
worker=TaskWorker(app.state.engine,root/'runs')
first=daily_tasks.submit_many(app.state.engine,dict(selection_keys=[picks[0]['selection_key']]))['items'][0]['task']['task_id'];worker.execute(worker.service.claim())
assert worker.service.get(first)['status']=='succeeded'
failed=daily_tasks.submit_many(app.state.engine,dict(selection_keys=[picks[1]['selection_key']]))['items'][0]['task']['task_id']
with patch.object(wb,'initialize_script',side_effect=OSError('offline upgrade checkpoint')):worker.execute(worker.service.claim())
assert worker.service.get(failed)['status']=='failed'
queued=worker.service.submit('old-profile')['task_id']
with Session(app.state.engine) as s:
    package=s.get(DailyScriptGeneration,first).package_key
    expected=dict(from_version=version('news2douyin'),first=first,failed=failed,queued=queued,script=wb.script_detail(s,package),
        tasks=[r.model_dump() for r in s.exec(select(TaskRecord).order_by(TaskRecord.task_id))],
        events=[r.model_dump() for r in s.exec(select(TaskEvent).order_by(TaskEvent.id))],
        generations=[r.model_dump() for r in s.exec(select(DailyScriptGeneration).order_by(DailyScriptGeneration.task_id))])
(root/'expected.json').write_text(json.dumps(expected,ensure_ascii=False),encoding='utf-8');app.state.engine.dispose();print(json.dumps(dict(status='seeded',version=version('news2douyin'))))
