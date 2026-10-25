import io
import json
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update
from sqlmodel import Session, select

from news2douyin.server.app import create_app
from news2douyin.storage.models import (Article, ArticleEventLink, Event, TaskRecord,
                                      VideoProduction, VideoAsset, TaskCheckpoint)
from news2douyin.tasks.service import TaskService, TaskContext
from news2douyin.tasks.worker import TaskWorker
from news2douyin.tasks.control import TaskCancelled, TaskConflict, TaskLeaseLost
from news2douyin.video import production as prod, render, media, workbench as wb
from news2douyin.video.service import build_script_package
from news2douyin.video.subtitles import split_narration, parse_srt, timecode


def wav_bytes(duration=1, rate=24000):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as f:
        f.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        f.writeframes(b'\0\0' * round(rate * duration))
    return buffer.getvalue()


def make_script(engine, tmp_path, key='demo'):
    with Session(engine) as s:
        s.add(Event(event_key=key, event_title='视频流程验证'))
        s.add(Article(article_key=key, title='功能测试资料', content='仅用于测试。'))
        s.add(ArticleEventLink(event_key=key, article_key=key))
        s.commit()
        package = build_script_package(s, key, output_root=tmp_path / 'packages')
        current = wb.script_detail(s, package.package_key)
        current = wb.save_script(s, package.package_key, expected_version=current['version'],
                                document=dict(current['document'], script_text='测试。'))
        current = wb.review_script(s, package.package_key, expected_version=current['version'], action='submit')
        return wb.review_script(s, package.package_key, expected_version=current['version'], action='approve',
                                checks={'sources_checked': True, 'wording_checked': True})


@pytest.fixture
def setup(engine, tmp_path, monkeypatch):
    script = make_script(engine, tmp_path)
    # Queue contract tests do not depend on installed system media tools.
    font = tmp_path / 'fixture.font'
    font.write_bytes(b'queue-fixture')
    monkeypatch.setattr(prod, 'font_path', lambda: font)
    monkeypatch.setattr(prod, 'capabilities', lambda: {'ready':True,'espeak':True,'edge':True})
    return script


def upload_audio(engine, tmp_path, script):
    return prod.add_asset(engine, tmp_path, script['package_key'], 'audio', 'voice.wav', io.BytesIO(wav_bytes()))


def uploaded_options(asset_id):
    return {'backend':'uploaded','audio_id':asset_id,'subtitles':'1\n00:00:00,000 --> 00:00:01,000\n测试。','preset':'preview'}


def require_media(monkeypatch):
    if not media.capabilities()['ready']:
        pytest.skip('real render needs ffmpeg, ffprobe, Pillow and CJK font')
    monkeypatch.setattr(prod, 'font_path', media.font_path)


def test_split_preserves_all_spoken_characters():
    text = '测试金额为12.5元。\n' + 'This is a long English sentence. ' * 5 + '系统继续处理！'
    chunks = split_narration(text)
    assert len(chunks) > 2
    compact = lambda t: re.sub(r'\s+', '', t)
    assert compact(''.join(chunks)) == compact(text)
    assert '12.5' in chunks[0]
    assert timecode(59.9996) == '00:01:00,000'


def test_invalid_configured_font_blocks_readiness(tmp_path, monkeypatch):
    pytest.importorskip('PIL.ImageFont')
    font = tmp_path / 'broken.ttf'
    font.write_bytes(b'not a font')
    monkeypatch.setenv('NEWS2DOUYIN_VIDEO_FONT', str(font))
    monkeypatch.setattr(media, 'binary', lambda name: name)
    result = media.capabilities()
    assert result['font'] is False
    assert result['ready'] is False
    assert result['errors']


@pytest.mark.parametrize('text', [
    '1\n00:00:01,000 --> 00:00:00,000\n测试。',
    '1\n00:00:00,000 --> 00:00:01,000\n错误内容。',
    '1\n00:70:00,000 --> 00:70:01,000\n测试。',
    '1\n00:00:00,000 --> 00:00:01,000\n测\n\n2\n00:00:00,500 --> 00:00:02,000\n试。',
])
def test_invalid_srt_is_rejected(text):
    with pytest.raises(ValueError):
        parse_srt(text, '测试。')


def test_assets_normalize_images_and_reject_invalid_audio(engine, tmp_path, setup):
    Image = pytest.importorskip('PIL.Image')
    image = io.BytesIO()
    Image.new('RGB',(64,32),'red').save(image,format='JPEG')
    image.seek(0)
    asset = prod.add_asset(engine,tmp_path,setup['package_key'],'image','../../photo.jpg',image)
    assert asset['original_name'] == 'photo.jpg'
    with Session(engine) as s:
        row = s.get(VideoAsset,asset['asset_id'])
        assert Path(row.storage_path).suffix == '.png'
        assert Image.open(row.storage_path).format == 'PNG'
    with pytest.raises(ValueError):
        prod.add_asset(engine,tmp_path,setup['package_key'],'audio','bad.wav',io.BytesIO(b'not audio'))
    with pytest.raises(ValueError):
        prod.add_asset(engine,tmp_path,setup['package_key'],'image','bad.png',io.BytesIO(b'not image'))
    assert not list((tmp_path/'video_assets').glob('*.upload'))


def test_submission_requires_current_approval(engine, tmp_path, setup):
    with Session(engine) as s:
        current = wb.save_script(s,setup['package_key'],expected_version=setup['version'],document=dict(setup['document'],script_text='新稿'))
    with pytest.raises(wb.ScriptConflict):
        prod.submit_video(engine,tmp_path,setup['package_key'],setup['version'],{})
    with pytest.raises(wb.ScriptConflict):
        prod.submit_video(engine,tmp_path,setup['package_key'],current['version'],{})
    with Session(engine) as s:
        assert not list(s.exec(select(VideoProduction)))


def test_snapshot_is_pinned_and_idempotency_checked(engine, tmp_path, setup):
    task = prod.submit_video(engine,tmp_path,setup['package_key'],setup['version'],{},'once')
    assert task['kind'] == 'video'
    with Session(engine) as s:
        row = s.get(VideoProduction,task['task_id'])
        before = row.spec_json
        wb.save_script(s,setup['package_key'],expected_version=setup['version'],document=dict(setup['document'],script_text='后续稿'))
        assert s.get(VideoProduction,task['task_id']).spec_json == before
    assert prod.submit_video(engine,tmp_path,setup['package_key'],setup['version'],{},'once')['task_id'] == task['task_id']
    with pytest.raises(TaskConflict):
        prod.submit_video(engine,tmp_path,setup['package_key'],setup['version'],{'rate':10},'once')


def test_concurrent_video_submission_queues_once(engine, tmp_path, setup):
    barrier = threading.Barrier(2)
    def submit(_):
        barrier.wait(timeout=3)
        return prod.submit_video(engine,tmp_path,setup['package_key'],setup['version'],{},'same')['task_id']
    with ThreadPoolExecutor(2) as pool:
        ids = list(pool.map(submit,range(2)))
    assert ids[0] == ids[1]
    with Session(engine) as s:
        assert len(list(s.exec(select(VideoProduction)))) == 1


def test_uploaded_audio_requires_matching_srt_and_owned_asset(engine, tmp_path, setup):
    audio = upload_audio(engine,tmp_path,setup)
    options = uploaded_options(audio['asset_id'])
    options['subtitles'] = '1\n00:00:00,000 --> 00:00:02,000\n测试。'
    with pytest.raises(ValueError,match='超过'):
        prod.submit_video(engine,tmp_path,setup['package_key'],setup['version'],options)
    other = make_script(engine,tmp_path,'other')
    with pytest.raises(ValueError,match='素材'):
        prod.submit_video(engine,tmp_path,other['package_key'],other['version'],uploaded_options(audio['asset_id']))
    with pytest.raises(ValueError,match='清空'):
        prod.submit_video(engine,tmp_path,setup['package_key'],setup['version'],{'backend':'espeak','audio_id':audio['asset_id']})


def test_queued_video_cancel_has_no_artifacts(engine,tmp_path,setup):
    task = prod.submit_video(engine,tmp_path,setup['package_key'],setup['version'],{})
    tasks = TaskService(engine)
    assert tasks.cancel(task['task_id'])['status'] == 'cancelled'
    assert tasks.claim() is None
    assert prod.production_detail(engine,task['task_id'])['files'] == []


def test_media_cancellation_kills_running_subprocess(tmp_path,monkeypatch):
    processes=[]
    original=subprocess.Popen
    def capture(*args,**kwargs):
        proc=original(*args,**kwargs)
        processes.append(proc)
        return proc
    monkeypatch.setattr(media.subprocess,'Popen',capture)
    count=0
    def cancel():
        nonlocal count
        count+=1
        if count>2:
            raise TaskCancelled('cancelled')
    with pytest.raises(TaskCancelled):
        media.run_process([sys.executable,'-c','import time; time.sleep(20)'],cwd=tmp_path,check=cancel)
    assert processes and processes[0].poll() is not None


def test_real_uploaded_render_files_and_checksum(engine,tmp_path,setup,monkeypatch):
    require_media(monkeypatch)
    audio=upload_audio(engine,tmp_path,setup)
    from PIL import Image
    data=io.BytesIO();Image.new('RGB',(80,60),'#315b91').save(data,format='PNG');data.seek(0)
    image=prod.add_asset(engine,tmp_path,setup['package_key'],'image','scene.png',data)
    options=uploaded_options(audio['asset_id']) | {'image_ids':[image['asset_id']]}
    queued=prod.submit_video(engine,tmp_path,setup['package_key'],setup['version'],options)
    tasks=TaskService(engine)
    TaskWorker(engine,tmp_path).execute(tasks.claim())
    result=prod.production_detail(engine,queued['task_id'])
    assert result['task']['status']=='succeeded',result['task']
    assert result['result']['width']==360 and result['result']['height']==640
    assert result['result']['alignment']=='user_supplied_srt'
    path,entry=prod.output_file(engine,queued['task_id'],'video.mp4')
    assert media.file_hash(path)==entry['sha256']
    streams = media.probe(path)['streams']
    assert {s['codec_name'] for s in streams}=={'h264','aac'}
    video = next(s for s in streams if s['codec_type']=='video')
    assert abs(float(video['duration'])-1.0)<.15
    assert int(video['nb_frames'])>=11
    bundle,_=prod.output_file(engine,queued['task_id'],'video_bundle.zip')
    import zipfile,hashlib
    with zipfile.ZipFile(bundle) as zipped:
        manifest=json.loads(zipped.read('manifest.json'))
        for name,checksum in manifest['files'].items():
            assert hashlib.sha256(zipped.read(name)).hexdigest()==checksum
    with pytest.raises(KeyError):
        prod.output_file(engine,queued['task_id'],'../../outside')
    path.write_bytes(b'corrupt')
    with pytest.raises(TaskConflict):
        prod.output_file(engine,queued['task_id'],'video.mp4')


def test_crash_resume_reuses_completed_voice_chunks(engine,tmp_path,setup,monkeypatch):
    require_media(monkeypatch)
    with Session(engine) as s:
        doc=dict(setup['document'],script_text='第一段。第二段。')
        current=wb.save_script(s,setup['package_key'],expected_version=setup['version'],document=doc)
        current=wb.review_script(s,setup['package_key'],expected_version=current['version'],action='submit')
        current=wb.review_script(s,setup['package_key'],expected_version=current['version'],action='approve',checks={'sources_checked':True,'wording_checked':True})
    calls=[]
    original=render.run_process
    def synthetic_voice(args,**kwargs):
        # Explicit short WAV fixture: this test verifies checkpoints, not voice quality.
        if 'news2douyin.video.voice' in args:
            calls.append(args)
            Path(args[args.index('--output')+1]).write_bytes(wav_bytes(.3))
        else:
            return original(args,**kwargs)
    monkeypatch.setattr(render,'run_process',synthetic_voice)
    queued=prod.submit_video(engine,tmp_path,setup['package_key'],current['version'],{'preset':'preview'})
    tasks=TaskService(engine);claim=tasks.claim();context=TaskContext(tasks,claim,threading.Event())
    progress=context.progress
    def crash(stage,current=0,total=0):
        if stage=='voice' and current==1:
            with Session(engine) as s:
                s.exec(update(TaskRecord).where(TaskRecord.task_id==claim.task_id).values(lease_until=time.time()-1));s.commit()
            raise TaskLeaseLost('simulated worker crash')
        progress(stage,current,total)
    context.progress=crash
    with pytest.raises(TaskLeaseLost):
        render.run_video(engine,claim,tmp_path,context)
    assert len(calls)==1
    tasks.recover()
    TaskWorker(engine,tmp_path).execute(tasks.claim())
    assert tasks.get(claim.task_id)['status']=='succeeded'
    assert len(calls)==2  # First chunk was not synthesized a second time.
    assert tasks.get(claim.task_id)['attempts']==2


def test_corrupt_audio_checkpoint_is_rebuilt(engine,tmp_path,setup,monkeypatch):
    require_media(monkeypatch)
    audio=upload_audio(engine,tmp_path,setup)
    queued=prod.submit_video(engine,tmp_path,setup['package_key'],setup['version'],uploaded_options(audio['asset_id']))
    tasks=TaskService(engine);claim=tasks.claim();context=TaskContext(tasks,claim,threading.Event())
    with Session(engine) as s:
        spec=json.loads(s.get(VideoProduction,claim.task_id).spec_json)
    folder=tmp_path/'attempt';folder.mkdir()
    first=render.make_audio(spec,folder,context)
    Path(first['audio']['path']).write_bytes(b'broken')
    other=tmp_path/'retry';other.mkdir()
    second=render.make_audio(spec,other,context)
    assert render.valid(second['audio'])
    assert second['audio']['path']!=first['audio']['path']


def test_video_api_pages_range_and_worker_integration(tmp_path,monkeypatch):
    require_media(monkeypatch)
    app=create_app(db_url=f'sqlite:///{tmp_path}/api.db',storage_root=str(tmp_path/'runs'))
    script=make_script(app.state.engine,tmp_path)
    with TestClient(app) as client:
        uploaded=client.post('/api/video/assets',data={'package_key':script['package_key'],'kind':'audio'},files={'file':('test.wav',wav_bytes(),'audio/wav')})
        assert uploaded.status_code==201
        response=client.post('/api/video/tasks',json={'package_key':script['package_key'],'expected_version':script['version'],'options':uploaded_options(uploaded.json()['asset_id'])})
        assert response.status_code==202
        task_id=response.json()['task_id']
        deadline=time.monotonic()+12
        while True:
            task=client.get('/api/tasks/'+task_id).json()
            if task['status'] in {'succeeded','failed'}:break
            assert time.monotonic()<deadline
            time.sleep(.05)
        assert task['status']=='succeeded',task
        assert client.get('/videos').status_code==200
        assert client.get('/videos/'+task_id).status_code==200
        assert client.get('/scripts/'+script['package_key']+'/video').status_code==200
        assert client.get('/tasks/'+task_id).status_code==200
        media_url='/api/video/tasks/'+task_id+'/files/video.mp4'
        partial=client.get(media_url,headers={'Range':'bytes=0-63'})
        assert partial.status_code==206 and len(partial.content)==64
        assert 'attachment;' in client.get(media_url+'?download=true').headers['content-disposition']
        assert client.get('/api/video/tasks/missing').status_code==404
