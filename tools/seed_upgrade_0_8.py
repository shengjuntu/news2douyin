"""Run using the installed 0.8 wheel before installing 0.9."""
import argparse
import json
from importlib.metadata import version
from pathlib import Path
from sqlmodel import Session, select
from news2douyin.storage.db import make_engine, init_db
from news2douyin.storage.models import Article, ArticleEventLink, CollectJob
from news2douyin.collect import service, llm_filter
from news2douyin.dedup import service as dedup
from news2douyin.collect.management import set_profile_enabled
from news2douyin.editorial.daily import choose, build_pick
from news2douyin.tasks.service import TaskService
from news2douyin.tasks.worker import TaskWorker
from news2douyin.events import workbench as events
from news2douyin.events.schemas import MomentEdit
from news2douyin.video.workbench import script_detail

assert version('news2douyin')=='0.8.0'
parser=argparse.ArgumentParser();parser.add_argument('directory');args=parser.parse_args()
root=Path(args.directory).resolve();root.mkdir(parents=True,exist_ok=False)
engine=make_engine('sqlite:///'+str(root/'old.db'));init_db(engine)
llm_filter.endpoint_alive=lambda:False;dedup.DEDUP_LLM_ENABLED=False
with Session(engine) as s:
    service.create_or_update_profile(s,dict(name='legacy_mock',provider='mock'))
    s.add(CollectJob(name='legacy_job',profile_name='legacy_mock',enabled=False,cron_expr='0 9 * * 1-5',timezone='Asia/Shanghai'));s.commit()
tasks=TaskService(engine);task=tasks.submit('legacy_mock');TaskWorker(engine,root/'runs').execute(tasks.claim());assert tasks.get(task['task_id'])['status']=='succeeded'
trial=tasks.submit('legacy_mock',trial=True);TaskWorker(engine,root/'runs').execute(tasks.claim());assert tasks.get(trial['task_id'])['status']=='succeeded'
with Session(engine) as s:
    article=s.exec(select(Article)).first();pick=choose(s,article.article_key)[0];package=build_pick(s,pick['selection_key'],storage_root=root/'runs')
    event_key=s.exec(select(ArticleEventLink.event_key).where(ArticleEventLink.article_key==article.article_key)).first()
    view=events.workspace(s,event_key);source=next(a for a in view['sources'] if a['article_key']==article.article_key)
    view=events.save_moment(s,event_key,MomentEdit(expected_version=view['version'],title='0.8 已核对的原有进展',description='固定历史来源，升级后沿用。',reviewed=True,sources=[{k:source[k] for k in ('article_key','revision','content_hash','excerpt')}]).model_dump())
    set_profile_enabled(s,'legacy_mock',False)
    expected=dict(from_version=version('news2douyin'),task_id=task['task_id'],trial_id=trial['task_id'],article_key=article.article_key,selection_key=pick['selection_key'],package_key=package['package_key'],event_key=event_key,moments=view['moments'],event_version=view['version'],script=script_detail(s,package['package_key']))
(root/'expected.json').write_text(json.dumps(expected,ensure_ascii=False),encoding='utf-8');engine.dispose()
print(json.dumps({k:v for k,v in expected.items() if k not in {'moments','script'}},ensure_ascii=False))
