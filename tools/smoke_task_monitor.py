"""Offline task-center browser fixture with an explicit test-only worker switch."""
import argparse
from contextlib import asynccontextmanager
from datetime import datetime,timezone,timedelta
import tempfile
import threading
import time
from pathlib import Path

from fastapi import HTTPException
from sqlmodel import Session
from news2douyin.collect import llm_filter,service as collect
from news2douyin.editorial import daily,daily_tasks
from news2douyin.storage.models import TaskRecord,Article,ArticleEventLink,Event,utc_now_iso
from news2douyin.storage.articles import record_version
from news2douyin.server.app import create_app
from news2douyin.server import webui


def main():
    import uvicorn
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=18199);args=parser.parse_args()
    llm_filter.endpoint_alive=lambda *a,**kw:False;webui._llm_alive=lambda *a,**kw:False
    gate=threading.Event();state={'waiting':False,'calls':0}
    def fixture(config):
        state['calls']+=1;state['waiting']=True
        if not gate.wait(40):raise ValueError('离线采集等待超时')
        state['waiting']=False;return []
    collect.PROVIDERS['mock']=fixture
    with tempfile.TemporaryDirectory(prefix='task-monitor-browser-') as directory:
        app=create_app(db_url=f'sqlite:///{directory}/app.db',storage_root=str(Path(directory)/'runs'))
        @asynccontextmanager
        async def controlled_lifespan(app):
            yield
            gate.set();app.state.worker.stop()
        app.router.lifespan_context=controlled_lifespan
        now=datetime.now(timezone.utc)
        with Session(app.state.engine) as s:
            collect.create_or_update_profile(s,{'name':'演示采集策略','provider':'mock'})
            a=Article(article_key='fixture',title='离线示例：每日脚本排队',content='示例资料仅验证任务排队，等待核对后制作。')
            s.add_all([a,Event(event_key='fixture',event_title='离线队列测试')]);s.flush();record_version(s,a)
            s.add(ArticleEventLink(article_key='fixture',event_key='fixture'));s.commit()
            pick=daily.choose(s,'fixture')[0]
            for i in range(112):
                moment=(now-timedelta(days=2,minutes=i)).isoformat()
                s.add(TaskRecord(task_id=f'history{i:03}',profile_name=f'历史记录 {i:03}',profile_json='{}',request_hash='x',
                  status='succeeded',stage='complete',created_at=moment,started_at=moment,finished_at=moment,
                  trigger_type='profile_test' if i%2 else 'manual'))
            s.add(TaskRecord(task_id='failure',profile_name='离线示例：失败待查看',profile_json='{}',request_hash='x',status='failed',stage='fetching',error_text='模拟上游不可用，请查看采集诊断。'))
            s.add(TaskRecord(task_id='date-boundary',profile_name='日期边界 %_ 字面匹配',profile_json='{}',request_hash='x',status='succeeded',created_at='2026-09-29T16:00:00Z',finished_at='2026-09-29T16:01:00Z'))
            s.commit()
        first=app.state.tasks.submit('演示采集策略')
        second=daily_tasks.submit_many(app.state.engine,dict(selection_keys=[pick['selection_key']]))['items'][0]['task']
        third=app.state.tasks.submit('演示采集策略',trial=True)
        @app.get('/fixture')
        def info():return dict(collect=first['task_id'],daily=second['task_id'],trial=third['task_id'],state=state)
        @app.post('/fixture/control/{mode}')
        def control(mode:str):
            if mode=='start':app.state.worker.start()
            elif mode=='release':gate.set()
            else:raise HTTPException(422)
            return state
        uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')


if __name__=='__main__':main()
