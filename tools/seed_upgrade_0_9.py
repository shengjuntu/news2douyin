"""Create completed and queued 0.9 video tasks before installing 0.10."""
import argparse
import io
import json
import wave
from importlib.metadata import version
from pathlib import Path
from sqlmodel import Session
from news2douyin.server.app import create_app
from news2douyin.storage.models import Article,ArticleEventLink,Event,VideoProduction
from news2douyin.video.service import build_script_package
from news2douyin.video import workbench as wb,production as prod
from news2douyin.tasks.worker import TaskWorker

assert version('news2douyin')=='0.9.0'
parser=argparse.ArgumentParser();parser.add_argument('directory');args=parser.parse_args()
root=Path(args.directory).resolve();root.mkdir(parents=True,exist_ok=False)
app=create_app(db_url=f'sqlite:///{root}/old.db',storage_root=str(root/'runs'))
with Session(app.state.engine) as s:
    s.add_all([Article(article_key='old',title='0.9 虚构演示资料',content='仅为版本升级测试。'),Event(event_key='old',event_title='0.9 已有作品')]);s.add(ArticleEventLink(article_key='old',event_key='old'));s.commit()
    package=build_script_package(s,'old',output_root=root/'runs'/'packages');script=wb.script_detail(s,package.package_key)
    doc=script['document'];doc['script_text']='旧版审核稿。';doc['title']='0.9 已有作品'
    script=wb.save_script(s,package.package_key,expected_version=script['version'],document=doc)
    script=wb.review_script(s,package.package_key,expected_version=script['version'],action='submit')
    script=wb.review_script(s,package.package_key,expected_version=script['version'],action='approve',checks={'sources_checked':True,'wording_checked':True})
audio=io.BytesIO()
with wave.open(audio,'wb') as wav:wav.setparams((1,2,24000,0,'NONE','not compressed'));wav.writeframes(b'\0\0'*24000)
audio.seek(0);asset=prod.add_asset(app.state.engine,root/'runs',package.package_key,'audio','old.wav',audio)
options=dict(backend='uploaded',audio_id=asset['asset_id'],subtitles='1\n00:00:00,000 --> 00:00:01,000\n旧版审核稿。',preset='preview')
complete=prod.submit_video(app.state.engine,root/'runs',package.package_key,script['version'],options)
worker=TaskWorker(app.state.engine,root/'runs');worker.execute(worker.service.claim())
assert worker.service.get(complete['task_id'])['status']=='succeeded'
queued=prod.submit_video(app.state.engine,root/'runs',package.package_key,script['version'],options)
with Session(app.state.engine) as s:
    expected=dict(from_version=version('news2douyin'),script=wb.script_detail(s,package.package_key),completed=complete['task_id'],queued=queued['task_id'],productions={key:s.get(VideoProduction,key).model_dump() for key in [complete['task_id'],queued['task_id']]})
(root/'expected.json').write_text(json.dumps(expected,ensure_ascii=False),encoding='utf-8');app.state.engine.dispose();print(json.dumps(dict(status='seeded',version='0.9.0',completed=complete['task_id'],queued=queued['task_id'])))
