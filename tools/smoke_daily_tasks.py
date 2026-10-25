"""Temporary offline server for daily task UI checks; never a production launcher."""
import argparse
import tempfile
import threading
from pathlib import Path

from fastapi import HTTPException
from sqlmodel import Session
from news2douyin.collect import llm_filter
from news2douyin.editorial import daily
from news2douyin.storage.articles import record_version
from news2douyin.storage.models import Article, ArticleEventLink, Event, utc_now_iso
from news2douyin.server import webui
from news2douyin.server.app import create_app


def seed(engine):
    with Session(engine) as s:
        s.add(Event(event_key='fixture',event_title='每日后台流程离线验证'))
        for i,title in enumerate(['计划公布','阶段实验','补充说明','后续安排']):
            row=Article(article_key='demo'+str(i),title='离线示例：'+title,
                content=f'虚构资料第{i}条：示例团队公布相关说明，具体结果仍待核对。',
                provider='mock',source_domain='example.invalid',url='https://example.invalid/'+str(i),
                published_at=utc_now_iso(),language='zh',country='cn')
            s.add(row);s.flush();record_version(s,row);s.add(ArticleEventLink(article_key=row.article_key,event_key='fixture'))
        s.commit()


def main():
    import uvicorn
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=18198);args=parser.parse_args()
    llm_filter.endpoint_alive=lambda *a,**kw:False;webui._llm_alive=lambda *a,**kw:False
    gate=threading.Event();state={'mode':'pass','calls':0,'waiting':False};original=daily.draft_document
    def model_fixture(editorial,mode='basic',**kwargs):
        if mode=='basic':return original(editorial,mode)
        state['calls']+=1
        if state['mode']=='hold':
            state['waiting']=True
            if not gate.wait(40):raise ValueError('离线测试等待超时')
            state['waiting']=False
        elif state['mode']=='fail_once':
            state['mode']='pass';raise ValueError('离线测试：模拟模型失败，可重试')
        result=original(editorial,'basic');result['notes']='AI 响应桩，仅用于后台流程验证。';return result
    daily.draft_document=model_fixture
    with tempfile.TemporaryDirectory(prefix='daily-tasks-browser-') as directory:
        app=create_app(db_url=f'sqlite:///{directory}/app.db',storage_root=str(Path(directory)/'runs'));seed(app.state.engine)
        @app.post('/fixture/control/{mode}')
        def control(mode:str):
            if mode not in {'pass','hold','fail_once','release'}:raise HTTPException(422)
            if mode=='release':gate.set()
            else:
                state['mode']=mode
                if mode=='hold':gate.clear()
            return state
        @app.get('/fixture/state')
        def fixture_state():return state
        uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')


if __name__=='__main__':main()
