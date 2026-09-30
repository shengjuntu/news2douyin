"""Create real 0.10.0 selections, approved script, and queued video before upgrade."""
import argparse
import io
import json
import wave
from importlib.metadata import version
from pathlib import Path
from sqlmodel import Session
from news2douyin.editorial import daily
from news2douyin.server.app import create_app
from news2douyin.storage.articles import record_version
from news2douyin.storage.models import Article,ArticleEventLink,Event,VideoProduction,VideoWork
from news2douyin.video import workbench as wb,production as prod

assert version('news2douyin')=='0.10.0'
parser=argparse.ArgumentParser();parser.add_argument('directory');args=parser.parse_args()
root=Path(args.directory).resolve();root.mkdir(parents=True,exist_ok=False)
app=create_app(db_url=f'sqlite:///{root}/old.db',storage_root=str(root/'runs'))
with Session(app.state.engine) as s:
    s.add(Event(event_key='old',event_title='0.10 升级验证'))
    for i in range(2):
        row=Article(article_key=f'a{i}',title=f'0.10 虚构资料{i}',content='原版每日选题的新闻正文。')
        s.add(row);s.flush();record_version(s,row);s.add(ArticleEventLink(article_key=row.article_key,event_key='old'))
    s.commit();daily.choose(s,'a0',day='2026-09-30');picks=daily.choose(s,'a1',day='2026-09-30')
    package=daily.build_pick(s,picks[0]['selection_key'],storage_root=root/'runs');key=package['package_key']
    script=wb.script_detail(s,key);doc=script['document'];doc['script_text']='旧版审核稿。'
    script=wb.save_script(s,key,expected_version=script['version'],document=doc)
    script=wb.review_script(s,key,expected_version=script['version'],action='submit')
    script=wb.review_script(s,key,expected_version=script['version'],action='approve',checks={'sources_checked':True,'wording_checked':True})
audio=io.BytesIO()
with wave.open(audio,'wb') as w:w.setparams((1,2,24000,0,'NONE','not compressed'));w.writeframes(b'\0\0'*24000)
audio.seek(0);asset=prod.add_asset(app.state.engine,root/'runs',key,'audio','old.wav',audio)
video=prod.submit_video(app.state.engine,root/'runs',key,script['version'],dict(backend='uploaded',audio_id=asset['asset_id'],subtitles='1\n00:00:00,000 --> 00:00:01,000\n旧版审核稿。',preset='preview',template='brief'))
with Session(app.state.engine) as s:
    work=s.get(VideoWork,video['task_id']);work.title='旧版自定义作品名';work.starred=True;work.notes='升级应保留';s.add(work);s.commit()
    expected=dict(from_version=version('news2douyin'),script=wb.script_detail(s,key),picks=daily.list_picks(s,'2026-09-30'),
        video=video['task_id'],production=s.get(VideoProduction,video['task_id']).model_dump(),work=s.get(VideoWork,video['task_id']).model_dump())
(root/'expected.json').write_text(json.dumps(expected,ensure_ascii=False),encoding='utf-8');app.state.engine.dispose();print(json.dumps(dict(status='seeded',version=version('news2douyin'),video=video['task_id'])))
