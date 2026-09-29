"""Real offline speech + H.264 encode. Requires video, voice-offline, ffmpeg/font.

Use python -I tools/smoke_video.py --output-dir /path/to/demo to retain a demo.
"""
import argparse
import json
from pathlib import Path
import shutil
import tempfile

from fastapi.testclient import TestClient
from sqlmodel import Session

from news2douyin.server.app import create_app
from news2douyin.storage.models import Article, ArticleEventLink, Event
from news2douyin.video import workbench as wb
from news2douyin.video.service import build_script_package
from news2douyin.tasks.worker import TaskWorker
from news2douyin.video.media import probe
from news2douyin.video.production import submit_video, production_detail, output_file


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir')
    parser.add_argument('--preset', default='preview', choices=['preview','portrait','fullhd'])
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='news2douyin-video-') as directory:
        root = Path(directory)
        app = create_app(db_url=f'sqlite:///{root / "app.db"}', storage_root=str(root / 'runs'))
        with Session(app.state.engine) as session:
            session.add(Event(event_key='demo', event_title='离线视频流程演示'))
            session.add(Article(article_key='demo-source', title='功能演示说明', content='这是软件功能验证，内容不属于真实新闻。', url='https://example.invalid/demo', published_at='2026-09-29T00:00:00Z'))
            session.add(ArticleEventLink(event_key='demo', article_key='demo-source'))
            session.commit()
            package = build_script_package(session, 'demo', output_root=root / 'packages')
            doc = wb.script_detail(session, package.package_key)
            doc['document']['script_text'] = '这是离线视频制作验证。系统已完成配音、字幕和画面合成。'
            current = wb.save_script(session, package.package_key, expected_version=doc['version'], document=doc['document'])
            current = wb.review_script(session, package.package_key, expected_version=current['version'], action='submit')
            current = wb.review_script(session, package.package_key, expected_version=current['version'], action='approve', checks={'sources_checked':True,'wording_checked':True}, note='仅用于功能演示，不是真实新闻')
        queued = submit_video(app.state.engine, root / 'runs', package.package_key, current['version'], {'backend':'espeak','preset':args.preset})
        worker = TaskWorker(app.state.engine, root / 'runs')
        worker.execute(app.state.tasks.claim())
        result = production_detail(app.state.engine, queued['task_id'])
        assert result['task']['status'] == 'succeeded', result['task']
        assert result['result']['duration'] > 1
        movie, _ = output_file(app.state.engine, queued['task_id'], 'video.mp4')
        for stream in probe(movie)['streams']:
            assert abs(float(stream['duration'])-result['result']['duration']) < .15
        client = TestClient(app)
        page = client.get('/videos/' + queued['task_id'])
        assert page.status_code == 200
        media = client.get('/api/video/tasks/' + queued['task_id'] + '/files/video.mp4', headers={'Range':'bytes=0-99'})
        assert media.status_code == 206 and len(media.content) == 100
        if args.output_dir:
            output = Path(args.output_dir)
            output.mkdir(parents=True, exist_ok=True)
            for name in ['video.mp4','cover.png','subtitles.srt','manifest.json']:
                path, _ = output_file(app.state.engine, queued['task_id'], name)
                shutil.copyfile(path, output / name)
        print(json.dumps({'status':'passed','checks':['offline_chinese_speech','sentence_timed_subtitles','real_h264_aac_encode','portrait_dimensions','artifact_checksums','video_page','http_range','full_video_audio_duration'], 'result':result['result']}, ensure_ascii=False))
        app.state.engine.dispose()


if __name__ == '__main__':
    main()
