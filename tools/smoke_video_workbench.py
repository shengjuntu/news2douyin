"""0.10 real offline Chinese speech demo, or isolated browser fixture server."""
import argparse
import io
import json
import shutil
import tempfile
import wave
from pathlib import Path
from sqlmodel import Session
from news2douyin.collect import llm_filter
from news2douyin.editorial import generation
from news2douyin.events import workbench as events
from news2douyin.events.schemas import MomentEdit
from news2douyin.server import webui
from news2douyin.server.app import create_app
from news2douyin.storage.articles import record_version
from news2douyin.storage.models import Article,ArticleEventLink,Event,VideoProduction
from news2douyin.tasks.worker import TaskWorker
from news2douyin.video import production as prod,workbench as wb,render,media
from news2douyin.video.templates import TEMPLATES


def seed(engine,root):
    with Session(engine) as s:
        a=Article(article_key='demo',title='离线演示：研发进展资料',content='虚构团队于九月一日公布实验计划。后续结果尚未确定。本资料仅用于功能验证。',source_domain='example.invalid',url='https://example.invalid/demo',published_at='2026-09-02T00:00:00Z')
        s.add_all([a,Event(event_key='demo',event_title='离线视频演示')]);s.flush();record_version(s,a)
        s.add(ArticleEventLink(article_key='demo',event_key='demo'));s.commit()
        for title,known in [('计划公布',True),('后续结果待确认',False)]:
            view=events.workspace(s,'demo');source=view['sources'][0]
            view=events.save_moment(s,'demo',MomentEdit(expected_version=view['version'],title=title,reviewed=True,
                time_kind='occurred' if known else 'unknown',date_start='2026-09-01' if known else '',certainty='exact' if known else 'unknown',
                sources=[{k:source[k] for k in ('article_key','revision','content_hash','excerpt')}]).model_dump())
    queued=generation.submit(engine,'demo',dict(expected_version=view['version'],moment_keys=[m['moment_key'] for m in view['moments']],options={'mode':'recap'},idempotency_key='demo'))
    worker=TaskWorker(engine,root);worker.execute(worker.service.claim())
    key=generation.detail(engine,queued['task_id'])['package_key']
    with Session(engine) as s:
        script=wb.script_detail(s,key);doc=script['document'];doc['title']='研发进展｜离线视频演示'
        doc['segments'][0]['text']='离线示例：团队公布实验计划。';doc['segments'][0]['visual']='计划标题卡与发生日期'
        doc['segments'][1]['kind']='analysis';doc['segments'][1]['text']='现有资料不足以判断后续结果。';doc['segments'][1]['visual']='分析标签与未知时间提示'
        doc['script_text']=wb.segment_narration(doc['segments']);doc['notes']='虚构资料，仅用于软件验证；配音为离线机械音。'
        script=wb.save_script(s,key,expected_version=script['version'],document=doc)
        script=wb.review_script(s,key,expected_version=script['version'],action='submit')
        return wb.review_script(s,key,expected_version=script['version'],action='approve',checks={'sources_checked':True,'wording_checked':True})


def demo(engine,root,script):
    task=prod.submit_video(engine,root,script['package_key'],script['version'],{'backend':'espeak','preset':'preview','template':'timeline'})
    worker=TaskWorker(engine,root);worker.execute(worker.service.claim())
    result=prod.production_detail(engine,task['task_id']);assert result['task']['status']=='succeeded',result['task']
    return result


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output-dir');parser.add_argument('--serve',action='store_true');parser.add_argument('--port',type=int,default=18197);args=parser.parse_args()
    llm_filter.endpoint_alive=lambda *a,**k:False;webui._llm_alive=lambda *a,**k:False
    with tempfile.TemporaryDirectory(prefix='video-workbench-') as directory:
        root=Path(directory);app=create_app(db_url=f'sqlite:///{root}/app.db',storage_root=str(root/'runs'))
        script=seed(app.state.engine,root/'runs');result=demo(app.state.engine,root/'runs',script)
        if args.output_dir:
            output=Path(args.output_dir);output.mkdir(parents=True,exist_ok=True)
            for entry in result['files']:
                source,_=prod.output_file(app.state.engine,result['task']['task_id'],entry['name']);shutil.copyfile(source,output/entry['name'])
            for template in TEMPLATES:
                (output/(template+'-preview.png')).write_bytes(prod.preview_frame(app.state.engine,script['package_key'],script['version'],{'template':template},1))
            info=media.probe(output/'video.mp4')
            assert {s['codec_name'] for s in info['streams']}=={'h264','aac'}
            assert all(abs(float(s['duration'])-result['result']['duration'])<.15 for s in info['streams'])
            report=dict(status='passed',checks=['real_offline_chinese_speech','structured_scene_switching','three_template_previews','h264_aac_encode','audio_video_duration','scene_timeline_export','checksummed_video_bundle'],result=result['result'])
            (output/'demo-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));print(json.dumps(report,ensure_ascii=False))
        if args.serve:
            import uvicorn
            @app.get('/fixture')
            def fixture():return {'script':script,'video':result}
            # Browser validation exercises a real resume after one injected encode failure.
            original=render.encode_video;failed=False
            def interrupt_once(folder,duration,fps,context):
                nonlocal failed
                with Session(app.state.engine) as s:spec=json.loads(s.get(VideoProduction,context.task_id).spec_json)
                if not failed and spec['options'].get('template')=='explain':
                    failed=True;raise RuntimeError('离线演示：模拟编码中断，配音已保存，可重试')
                return original(folder,duration,fps,context)
            render.encode_video=interrupt_once
            uvicorn.run(app,host='127.0.0.1',port=args.port,log_level='warning')
        app.state.engine.dispose()


if __name__=='__main__':main()
