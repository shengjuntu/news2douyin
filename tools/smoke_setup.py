"""Isolated first-run browser fixture. No actual news/model account is used."""
import argparse
from contextlib import asynccontextmanager
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from urllib.error import HTTPError

from sqlmodel import Session
from news2douyin.collect.service import create_or_update_profile
from news2douyin.server import readiness
from news2douyin.server.app import create_app
from news2douyin.storage.models import CollectJob


def main():
    import uvicorn
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=18200);args=parser.parse_args()
    for key in ('API_KEY','ALT_NEWS_KEY','OPENAI_BASE_URL','OPENAI_API_KEY','MODEL'):
        os.environ.pop(key,None)
    os.environ['NEWS2DOUYIN_TIMEZONE']='Asia/Shanghai'
    with tempfile.TemporaryDirectory(prefix='setup-browser-') as directory:
        os.environ['NEWS2DOUYIN_ENV_FILE']=str(Path(directory)/'absent.env')
        app=create_app(db_url=f'sqlite:///{directory}/app.db',storage_root=str(Path(directory)/'runs'))
        @asynccontextmanager
        async def stopped(app):
            yield
        app.router.lifespan_context=stopped
        state={'model_calls':0,'video_calls':0,'mode':'error'}
        class Response(io.BytesIO):status=200
        def model_request(*a,**kw):
            state['model_calls']+=1
            if state['mode']=='error':raise HTTPError('http://fixture',401,'private-secret',{},None)
            return Response(json.dumps({'data':[{'id':os.environ['MODEL']}]}).encode())
        readiness.build_opener=lambda *a:SimpleNamespace(open=model_request)
        def video():
            state['video_calls']+=1
            return dict(ok=True,ready=False,checked_at='2026-09-30T15:30:00Z',checks=[
                dict(key='ffmpeg',label='FFmpeg 可执行程序',ok=False,fix='安装 FFmpeg 并加入 PATH。'),
                dict(key='font',label='中文字体',ok=True,fix=''),
                dict(key='espeak',label='离线机械配音（可选）',ok=False,fix='可选上传配音。')])
        readiness.video_report=video
        @app.get('/fixture')
        def fixture():return state
        @app.post('/fixture/configure')
        def configure():
            os.environ.update(ALT_NEWS_KEY='private-news-key',OPENAI_API_KEY='private-model-key',
                OPENAI_BASE_URL='http://127.0.0.1:19993/v1',MODEL='<script>window.injected=true</script>')
            Path(app.state.storage_root).mkdir(exist_ok=True)
            with Session(app.state.engine) as s:
                create_or_update_profile(s,dict(name='演示采集',provider='mock'))
                create_or_update_profile(s,dict(name='真实新闻缺少密钥',provider='worldnewsapi'))
                create_or_update_profile(s,dict(name='<img src=x onerror=window.injected=true>',provider='worldnewsapi',extra={'api_key_env':'ALT_NEWS_KEY'}))
                s.add(CollectJob(name='工作日早间采集',profile_name='演示采集',cron_expr='0 9 * * 1-5',timezone='Asia/Shanghai'))
                s.add(CollectJob(name='失效计划',profile_name='已删除策略',cron_expr='0 9 * * *',timezone='UTC'))
                s.commit()
            return {'ok':True}
        @app.post('/fixture/model-ready')
        def model_ready():state['mode']='ready';return {'ok':True}
        uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')


if __name__=='__main__':main()
