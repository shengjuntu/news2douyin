import json
from pathlib import Path
from importlib.metadata import version
from sqlmodel import Session, select
from news2douyin.storage.db import make_engine, init_db
from news2douyin.storage.models import Article, CollectJob
from news2douyin.collect import service, llm_filter
from news2douyin.dedup import service as dedup
from news2douyin.editorial.daily import choose, build_pick
from news2douyin.tasks.service import TaskService
from news2douyin.tasks.worker import TaskWorker
assert version('news2douyin')=='0.6.0'
import argparse
parser=argparse.ArgumentParser(description='Create a 0.6.0 fixture before upgrading the installed wheel')
parser.add_argument('directory')
root=Path(parser.parse_args().directory).resolve();root.mkdir(parents=True,exist_ok=False)
engine=make_engine('sqlite:///'+str(root/'old.db'));init_db(engine)
llm_filter.endpoint_alive=lambda:False
dedup.DEDUP_LLM_ENABLED=False
with Session(engine) as s:
    service.create_or_update_profile(s,dict(name='legacy_mock',provider='mock'))
    s.add(CollectJob(name='legacy_job',profile_name='legacy_mock',enabled=False,cron_expr='0 9 * * 1-5',timezone='Asia/Shanghai'));s.commit()
tasks=TaskService(engine);task=tasks.submit('legacy_mock');TaskWorker(engine,root/'runs').execute(tasks.claim())
assert tasks.get(task['task_id'])['status']=='succeeded'
with Session(engine) as s:
    article=s.exec(select(Article)).first()
    pick=choose(s,article.article_key)[0]
    package=build_pick(s,pick['selection_key'],storage_root=root/'runs')
    expected=dict(from_version=version('news2douyin'),task_id=task['task_id'],article_key=article.article_key,selection_key=pick['selection_key'],package_key=package['package_key'])
(root/'expected.json').write_text(json.dumps(expected))
engine.dispose()
print(json.dumps(expected))
