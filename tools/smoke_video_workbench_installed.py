"""Installed 0.10 contracts and real 0.9 video/database migration. Run with python -I."""
import argparse
import io
import json
import runpy
import tempfile
import wave
import zipfile
from importlib.metadata import version
from pathlib import Path
from fastapi.testclient import TestClient
from sqlmodel import Session
from news2douyin.server.app import create_app
from news2douyin.storage.models import VideoProduction,VideoWork
from news2douyin.tasks.worker import TaskWorker
from news2douyin.video import production as prod,workbench as wb


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--upgrade-fixture',default='');args=parser.parse_args();assert tuple(map(int,version('news2douyin').split('.')[:3])) >= (0,10,0)
    seed=runpy.run_path(str(Path(__file__).with_name('smoke_video_workbench.py')))['seed'];checks=[]
    with tempfile.TemporaryDirectory(prefix='video-wheel-') as directory:
        root=Path(directory);app=create_app(db_url=f'sqlite:///{root}/app.db',storage_root=str(root/'runs'));client=TestClient(app)
        script=seed(app.state.engine,root/'runs');key=script['package_key']
        assert client.get('/scripts/'+key+'/video').status_code==200
        templates=client.get('/api/video/templates').json();assert len(templates)==3
        input=dict(package_key=key,expected_version=script['version'],options={'template':'explain','backend':'uploaded','preset':'preview'})
        preview=client.post('/api/video/preview',json=input);assert preview.status_code==200 and preview.content.startswith(b'\x89PNG')
        checks.append('installed_templates_setup_and_real_preview')
        voice=io.BytesIO()
        with wave.open(voice,'wb') as wav:wav.setparams((1,2,24000,0,'NONE','not compressed'));wav.writeframes(b'\0\0'*48000)
        upload=client.post('/api/video/assets',data={'package_key':key,'kind':'audio'},files={'file':('fixture.wav',voice.getvalue(),'audio/wav')});assert upload.status_code==201
        parts=[('分析：' if p['kind']=='analysis' else '')+p['text'] for p in script['document']['segments']]
        input['options'].update(audio_id=upload.json()['asset_id'],subtitles='1\n00:00:00,000 --> 00:00:01,000\n'+parts[0]+'\n\n2\n00:00:01,000 --> 00:00:02,000\n'+parts[1])
        assert client.post('/api/video/preflight',json=input).json()['ready']
        task=client.post('/api/video/tasks',json=input).json();tid=task['task_id'];worker=TaskWorker(app.state.engine,root/'runs');worker.execute(worker.service.claim())
        result=client.get('/api/video/tasks/'+tid).json();assert result['task']['status']=='succeeded',result['task']
        with zipfile.ZipFile(io.BytesIO(client.get('/api/video/tasks/'+tid+'/files/video_bundle.zip').content)) as z:
            timing=json.loads(z.read('scene_timeline.json'));assert timing['scenes'][1]['kind']=='analysis' and timing['frames'][-1]['end']==2
        checks.append('installed_preflight_actual_encode_and_scene_export')
        page=client.get('/api/video/works').json();assert page['total']==1 and page['items'][0]['cover_url']
        assert client.get('/api/video/tasks/'+tid+'/files/video.mp4',headers={'Range':'bytes=0-63'}).status_code==206
        assert client.get('/videos/'+tid).status_code==200 and client.get('/videos').status_code==200
        checks.append('installed_gallery_cover_playback_range_and_download')
        updated=client.put('/api/video/works/'+tid,json={'expected_version':1,'title':'安装验证作品','notes':'可检索备注','starred':True,'archived':True}).json();assert updated['version']==2
        assert client.get('/api/video/works').json()['total']==0
        assert client.get('/api/video/works?archived=true&starred=true&query=可检索备注').json()['total']==1
        checks.append('installed_rename_search_favorite_archive_and_version')
        recovery=client.get('/api/video/tasks/'+tid+'/recovery').json();assert recovery['result_cached'] and recovery['audio_cached']
        assert client.get('/scripts/'+key+'/video?from_task='+tid).status_code==200
        checks.append('installed_recovery_diagnostics_and_reuse_configuration')
        app.state.engine.dispose()
    if args.upgrade_fixture:
        root=Path(args.upgrade_fixture);expected=json.loads((root/'expected.json').read_text());assert expected['from_version']=='0.9.0'
        app=create_app(db_url=f'sqlite:///{root}/old.db',storage_root=str(root/'runs'));client=TestClient(app)
        with Session(app.state.engine) as s:
            assert wb.script_detail(s,expected['script']['package_key'])==expected['script']
            for tid,before in expected['productions'].items():
                assert s.get(VideoProduction,tid).model_dump()==before
                assert s.get(VideoWork,tid).template_id=='legacy'
        assert client.get('/api/video/works').json()['total']==1
        complete=expected['completed'];assert client.get('/api/video/tasks/'+complete+'/files/video.mp4',headers={'Range':'bytes=0-63'}).status_code==206
        checks.append('upgrade_backfills_library_preserving_existing_video_script_and_bytes')
        worker=TaskWorker(app.state.engine,root/'runs');claimed=worker.service.claim();assert claimed.task_id==expected['queued'];worker.execute(claimed)
        assert worker.service.get(expected['queued'])['status']=='succeeded'
        assert client.get('/api/video/works?template=legacy').json()['total']==2
        with Session(app.state.engine) as s:
            assert s.get(VideoProduction,expected['queued']).spec_json==expected['productions'][expected['queued']]['spec_json']
        checks.append('upgrade_resumes_queued_legacy_video_with_original_frozen_input')
        app.state.engine.dispose()
    print(json.dumps(dict(status='passed',version=version('news2douyin'),passed=len(checks),checks=checks)))


if __name__=='__main__':main()
