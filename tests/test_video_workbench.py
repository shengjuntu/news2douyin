import copy
import io
import json
import time
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.editorial import generation
from news2douyin.server.app import create_app
from news2douyin.storage.db import init_db
from news2douyin.storage.models import VideoProduction, VideoWork, VideoAsset, TaskRecord, ScriptExport
from news2douyin.tasks.control import TaskConflict
from news2douyin.tasks.service import TaskService
from news2douyin.tasks.worker import TaskWorker
from news2douyin.video import production as prod, render, library, media, workbench as wb
from news2douyin.video.templates import TEMPLATES, scene_plan, bind_cues
from test_script_generation import curated, request as generation_request
from test_video_pipeline import wav_bytes, require_media, make_script


@pytest.fixture
def structured(curated,tmp_path):
    task=generation.submit(curated,'topic',generation_request(curated))
    worker=TaskWorker(curated,tmp_path);worker.execute(worker.service.claim())
    key=generation.detail(curated,task['task_id'])['package_key']
    with Session(curated) as s:
        script=wb.script_detail(s,key);doc=script['document'];doc['segments'][0]['text']='计划公布。'
        doc['segments'].append(dict(doc['segments'][0],segment_key='analysis',kind='analysis',text='仍待确认。',visual='分析标签与来源卡'))
        doc['script_text']=wb.segment_narration(doc['segments']);doc['title']='离线模板与作品验证'
        script=wb.save_script(s,key,expected_version=script['version'],document=doc)
        script=wb.review_script(s,key,expected_version=script['version'],action='submit')
        return wb.review_script(s,key,expected_version=script['version'],action='approve',checks={'sources_checked':True,'wording_checked':True})


def options(engine,root,script):
    audio=prod.add_asset(engine,root,script['package_key'],'audio','audio.wav',io.BytesIO(wav_bytes(2)))
    return dict(backend='uploaded',audio_id=audio['asset_id'],preset='preview',template='timeline',
        subtitles='1\n00:00:00,100 --> 00:00:00,600\n计划公布。\n\n2\n00:00:00,800 --> 00:00:01,600\n分析：仍待确认。')


def image_asset(engine,root,script,color='red'):
    from PIL import Image
    data=io.BytesIO();Image.new('RGB',(64,64),color).save(data,format='PNG');data.seek(0)
    return prod.add_asset(engine,root,script['package_key'],'image',color+'.png',data)


def execute(engine,root):
    worker=TaskWorker(engine,root);worker.execute(worker.service.claim())


def test_preflight_and_preview_have_no_task_or_export_side_effect(curated,tmp_path,structured,monkeypatch):
    require_media(monkeypatch);settings=options(curated,tmp_path,structured)
    with Session(curated) as s:
        old_tasks=len(s.exec(select(TaskRecord)).all());old_exports=len(s.exec(select(ScriptExport)).all())
    result=prod.preflight(curated,structured['package_key'],structured['version'],settings)
    assert result['ready'] and result['scene_count']==2 and result['actual_duration']==2
    preview=prod.preview_frame(curated,structured['package_key'],structured['version'],settings,1)
    assert preview.startswith(b'\x89PNG')
    with Session(curated) as s:
        assert len(s.exec(select(TaskRecord)).all())==old_tasks
        assert len(s.exec(select(ScriptExport)).all())==old_exports
        assert not s.exec(select(VideoProduction)).all()
    # Preview needs no uploaded voice, even when that is the selected future backend.
    prod.preview_frame(curated,structured['package_key'],structured['version'],{'backend':'uploaded'})


def test_srt_must_respect_semantic_segment_boundaries(curated,tmp_path,structured,monkeypatch):
    require_media(monkeypatch);settings=options(curated,tmp_path,structured)
    settings['subtitles']='1\n00:00:00,000 --> 00:00:02,000\n计划公布。分析：仍待确认。'
    with pytest.raises(ValueError,match='跨越脚本段落'):
        prod.submit_video(curated,tmp_path,structured['package_key'],structured['version'],settings)
    with Session(curated) as s: assert not s.exec(select(VideoProduction)).all()


def test_scene_assignments_pinned_and_reject_foreign_or_obsolete_assets(curated,tmp_path,structured,monkeypatch):
    require_media(monkeypatch)
    selected=image_asset(curated,tmp_path,structured)
    opts=options(curated,tmp_path,structured)|{'scene_images':{'analysis':selected['asset_id']}}
    task=prod.submit_video(curated,tmp_path,structured['package_key'],structured['version'],opts)
    with Session(curated) as s:
        row=s.get(VideoProduction,task['task_id']);spec=json.loads(row.spec_json)
    assert spec['scenes'][0]['image_id'] is None and spec['scenes'][1]['image_id']==selected['asset_id']
    from news2douyin.video.templates import scene_image
    assert scene_image(spec,spec['scenes'][0]) is None
    assert spec['template']==TEMPLATES['timeline']
    assert spec['scenes'][0]['dates'][0]['label']=='发生 2026-09-01'
    with pytest.raises(ValueError,match='段落已不存在'):
        prod.preflight(curated,structured['package_key'],structured['version'],opts|{'scene_images':{'old-part':selected['asset_id']}})
    other=make_script(curated,tmp_path,'unrelated');foreign=image_asset(curated,tmp_path,other)
    with pytest.raises(ValueError,match='不属于'):
        prod.preflight(curated,structured['package_key'],structured['version'],opts|{'scene_images':{'analysis':foreign['asset_id']}})
    with Session(curated) as s:
        assert s.get(VideoProduction,task['task_id']).spec_json==row.spec_json


@pytest.mark.parametrize('template',['brief','explain','timeline'])
def test_real_template_render_includes_continuous_scene_timing(curated,tmp_path,structured,monkeypatch,template):
    require_media(monkeypatch)
    settings=options(curated,tmp_path,structured)|{'template':template}
    if template=='timeline':settings['scene_images']={'analysis':image_asset(curated,tmp_path,structured,'blue')['asset_id']}
    task=prod.submit_video(curated,tmp_path,structured['package_key'],structured['version'],settings)
    execute(curated,tmp_path);result=prod.production_detail(curated,task['task_id'])
    assert result['task']['status']=='succeeded',result['task']
    timing=json.loads(prod.output_file(curated,task['task_id'],'scene_timeline.json')[0].read_text())
    frames=timing['frames'];assert frames[0]['start']==0 and frames[-1]['end']==2
    assert all(a['end']==b['start'] for a,b in zip(frames,frames[1:]))
    assert next(f for f in frames if f['text'].startswith('分析：'))['kind']=='analysis'
    assert timing['template']['id']==template
    if template=='timeline':
        assert not frames[0]['image_id']
        assert next(f for f in frames if f['text'].startswith('分析：'))['image_id']
    info=media.probe(prod.output_file(curated,task['task_id'],'video.mp4')[0])
    assert {s['codec_name'] for s in info['streams']}=={'h264','aac'}
    assert abs(float(info['format']['duration'])-2)<.15
    with zipfile.ZipFile(prod.output_file(curated,task['task_id'],'video_bundle.zip')[0]) as z:
        assert json.loads(z.read('scene_timeline.json'))==timing
        manifest=json.loads(z.read('manifest.json'))
        assert manifest['scene_count']==2 and manifest['template']['id']==template
    assert library.work_page(curated)['items'][0]['cover_url']


def test_template_preview_pixels_differ_and_use_same_layout(curated,tmp_path,structured,monkeypatch):
    require_media(monkeypatch)
    data=[prod.preview_frame(curated,structured['package_key'],structured['version'],{'template':key}) for key in TEMPLATES]
    assert len(set(data))==3
    from PIL import Image
    for key,png in zip(TEMPLATES,data):
        image=Image.open(io.BytesIO(png));assert image.size==(360,640)
        from PIL import ImageColor
        assert image.getpixel((0,0))==ImageColor.getrgb(TEMPLATES[key]['background'])


def test_gallery_filters_pagination_timezone_metadata_and_archival(curated,tmp_path,structured,monkeypatch):
    require_media(monkeypatch);settings=options(curated,tmp_path,structured)
    first=prod.submit_video(curated,tmp_path,structured['package_key'],structured['version'],settings)
    second=prod.submit_video(curated,tmp_path,structured['package_key'],structured['version'],settings|{'template':'brief'})
    third=prod.submit_video(curated,tmp_path,structured['package_key'],structured['version'],settings|{'template':'explain'})
    with Session(curated) as s:
        for tid,date in [(first['task_id'],'2026-09-29T15:59:59Z'),(second['task_id'],'2026-09-29T16:00:00Z'),(third['task_id'],'2026-09-30T16:00:00Z')]:
            task=s.get(TaskRecord,tid);task.created_at=date;s.add(task)
        s.commit()
    page=library.work_page(curated,status='all',date_from='2026-09-30',date_to='2026-09-30',timezone='Asia/Shanghai')
    assert [v['task']['task_id'] for v in page['items']]==[second['task_id']]
    assert library.work_page(curated,status='all',limit=1)['total']==3
    assert len(library.work_page(curated,status='all',limit=1,offset=1)['items'])==1
    assert library.work_page(curated,status='all',template='explain')['total']==1
    edited=library.edit(curated,first['task_id'],dict(expected_version=1,title='原始 100%_ 标题',notes='重点材料',starred=True))
    assert edited['version']==2
    assert library.work_page(curated,status='all',query='100%_')['total']==1
    assert library.work_page(curated,status='active',starred=True)['total']==1
    assert library.work_page(curated,status='all',query='重点')['total']==1
    with pytest.raises(TaskConflict):library.edit(curated,first['task_id'],dict(expected_version=1,title='过期覆盖'))
    library.edit(curated,first['task_id'],dict(expected_version=2,title=edited['title'],archived=True,starred=True))
    assert library.work_page(curated,status='all')['total']==2
    assert library.work_page(curated,status='all',archived=True)['total']==1
    library.edit(curated,first['task_id'],dict(expected_version=3,title='已恢复'))
    assert library.work_page(curated,status='all')['total']==3
    assert prod.production_detail(curated,first['task_id'])['title']==structured['document']['title']
    with pytest.raises(ValueError):library.work_page(curated,status='all',date_from='2026-10-10',date_to='2026-09-01')


def test_migration_backfills_legacy_video_without_mutating_input_or_result(curated,tmp_path,structured,monkeypatch):
    require_media(monkeypatch)
    task=prod.submit_video(curated,tmp_path,structured['package_key'],structured['version'],options(curated,tmp_path,structured))
    with Session(curated) as s:
        item=s.get(VideoWork,task['task_id']);s.delete(item)
        row=s.get(VideoProduction,task['task_id']);spec=json.loads(row.spec_json)
        spec.pop('template');spec.pop('scenes');spec['schema_version']=1
        row.spec_json=wb.canonical(spec);row.input_hash=wb.digest(row.spec_json.encode());s.add(row);s.commit()
        before=(row.spec_json,row.result_json,row.input_hash)
    init_db(curated)
    with Session(curated) as s:
        work=s.get(VideoWork,task['task_id']);assert work.title==structured['document']['title'] and work.template_id=='legacy'
        row=s.get(VideoProduction,task['task_id']);assert (row.spec_json,row.result_json,row.input_hash)==before
    execute(curated,tmp_path)
    assert prod.production_detail(curated,task['task_id'])['task']['status']=='succeeded'


def test_recovery_reuses_audio_and_reports_missing_assets(curated,tmp_path,structured,monkeypatch):
    require_media(monkeypatch);settings=options(curated,tmp_path,structured)
    task=prod.submit_video(curated,tmp_path,structured['package_key'],structured['version'],settings)
    real_encode=render.encode_video;monkeypatch.setattr(render,'encode_video',Mock(side_effect=RuntimeError('simulated encoder interruption')))
    execute(curated,tmp_path)
    report=library.recovery(curated,task['task_id'])
    assert report['can_retry'] and report['audio_cached']
    with Session(curated) as s:
        Path(s.get(VideoAsset,settings['audio_id']).storage_path).unlink()
    assert library.recovery(curated,task['task_id'])['can_retry']
    monkeypatch.setattr(render,'encode_video',real_encode);TaskService(curated).retry(task['task_id']);execute(curated,tmp_path)
    assert prod.production_detail(curated,task['task_id'])['task']['status']=='succeeded'
    movie,_=prod.output_file(curated,task['task_id'],'video.mp4');movie.unlink()
    entry=library.work_page(curated)['items'][0]
    assert 'video.mp4' in entry['missing_files'] and not entry['video_url']
    assert 'video.mp4' in prod.production_detail(curated,task['task_id'])['missing_files']


def test_recovery_blocks_corrupt_input_even_with_valid_result(curated,tmp_path,structured,monkeypatch):
    require_media(monkeypatch)
    task=prod.submit_video(curated,tmp_path,structured['package_key'],structured['version'],options(curated,tmp_path,structured))
    with Session(curated) as s:
        row=s.get(VideoProduction,task['task_id']);row.input_hash='wrong';s.add(row)
        taskrow=s.get(TaskRecord,task['task_id']);taskrow.status='failed';s.add(taskrow);s.commit()
    report=library.recovery(curated,task['task_id'])
    assert not report['can_retry'] and not report['checks'][0]['ok']


def test_capabilities_requires_real_encoders(monkeypatch,tmp_path):
    monkeypatch.setattr(media,'binary',lambda name:name)
    monkeypatch.setattr(media.subprocess,'run',lambda *a,**k:Mock(returncode=0,stdout=b' V..... some_codec\n A..... aac\n'))
    result=media.capabilities()
    assert not result['ready']
    assert not result['encoders']['libx264']
    assert next(c for c in result['checks'] if c['key']=='encoders')['fix']


def test_work_api_preview_preflight_and_reuse_page(curated,tmp_path,structured,monkeypatch):
    require_media(monkeypatch)
    app=create_app(db_url=str(curated.url),storage_root=str(tmp_path));client=TestClient(app)
    settings=options(curated,tmp_path,structured);payload=dict(package_key=structured['package_key'],expected_version=structured['version'],options=settings)
    assert len(client.get('/api/video/templates').json())==3
    assert client.post('/api/video/preflight',json=payload).json()['ready']
    response=client.post('/api/video/preview',json=payload);assert response.status_code==200 and response.headers['content-type']=='image/png'
    assert client.post('/api/video/preview',json=payload|{'scene_index':99}).status_code==422
    task=client.post('/api/video/tasks',json=payload).json();key=task['task_id']
    assert client.get('/api/video/works?status=active').json()['total']==1
    assert client.get('/scripts/'+structured['package_key']+'/video?from_task='+key).status_code==200
    assert client.get('/videos/'+key).status_code==200 and client.get('/videos').status_code==200
    assert client.put('/api/video/works/'+key,json={'expected_version':1,'title':'新的作品名称'}).status_code==200
    assert client.put('/api/video/works/'+key,json={'expected_version':1,'title':'过期'}).status_code==409
    assert client.get('/api/video/works?status=invalid').status_code==422
    assert client.get('/api/video/tasks/'+key+'/recovery').status_code==200
    app.state.engine.dispose()


def test_preview_missing_drawing_dependency_is_actionable(curated,tmp_path,structured,monkeypatch):
    require_media(monkeypatch)
    monkeypatch.setattr(render,'draw_card',Mock(side_effect=ModuleNotFoundError('PIL')))
    with pytest.raises(ValueError,match='图片依赖'):
        prod.preview_frame(curated,structured['package_key'],structured['version'],{})
