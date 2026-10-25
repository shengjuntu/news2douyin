"""Searchable video labels, additive migration, and read-only recovery diagnostics."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, update
from sqlmodel import Session, select

from ..search.dates import date_range, timezone_name
from ..storage.articles import lock_news
from ..storage.models import VideoWork, VideoProduction, TaskRecord, TaskCheckpoint, utc_now_iso
from ..storage.utils import loads
from ..tasks.control import TaskConflict
from ..tasks.service import task_dict
from .media import capabilities, file_hash
from .templates import TEMPLATES


class WorkEdit(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_version: int = Field(ge=0)
    title: str = Field(min_length=1, max_length=300)
    notes: str = Field(default='', max_length=4000)
    starred: bool = False
    archived: bool = False

    @field_validator('title')
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError('作品名称不能为空')
        return value.strip()


def backfill_works(engine):
    with Session(engine) as session:
        lock_news(session)
        rows = session.exec(select(VideoProduction).outerjoin(VideoWork, VideoWork.task_id == VideoProduction.task_id)
                            .where(VideoWork.task_id.is_(None))).all()
        for row in rows:
            spec = loads(row.spec_json, {})
            session.add(VideoWork(task_id=row.task_id,
                title=spec.get('snapshot', {}).get('document', {}).get('title') or row.package_key,
                template_id=spec.get('template', {}).get('id', 'legacy')))
        session.commit()


def metadata(session, row, spec=None):
    item = session.get(VideoWork, row.task_id)
    if item:
        return {k: getattr(item, k) for k in ('title','notes','starred','archived','version','updated_at','template_id')}
    spec = spec or loads(row.spec_json, {})
    return dict(title=spec.get('snapshot', {}).get('document', {}).get('title') or row.package_key,
                notes='', starred=False, archived=False, version=0, updated_at=row.created_at,
                template_id=spec.get('template', {}).get('id', 'legacy'))


def edit(engine, task_id, data):
    data = WorkEdit.model_validate(data)
    with Session(engine) as session:
        lock_news(session)
        row = session.get(VideoProduction, task_id)
        if not row: raise KeyError('作品不存在')
        current = metadata(session, row)
        if current['version'] != data.expected_version:
            raise TaskConflict('作品信息已在其他页面更新，请刷新后重试')
        work = session.get(VideoWork, task_id) or VideoWork(task_id=task_id, title=current['title'], template_id=current['template_id'])
        for key in ('title','notes','starred','archived'): setattr(work, key, getattr(data, key))
        work.version = data.expected_version + 1
        work.updated_at = utc_now_iso()
        session.add(work); session.commit()
        return metadata(session, row)


def presence(files):
    missing = []
    for name, entry in files.items():
        try:
            path = Path(entry['path'])
            if not path.is_file() or path.stat().st_size != entry.get('size_bytes'): missing.append(name)
        except (KeyError, OSError): missing.append(name)
    return missing


def work_page(engine, *, query='', status='succeeded', date_from='', date_to='', timezone='',
              template='', starred=False, archived=False, offset=0, limit=24):
    allowed = {'all','active','succeeded','failed','cancelled'}
    if status not in allowed: raise ValueError('无效作品状态')
    if template and template not in {*TEMPLATES, 'legacy'}: raise ValueError('无效视频模板')
    zone = timezone_name(timezone)
    start, end = date_range('custom', date_from, date_to, zone)
    conditions = [VideoWork.archived == archived]
    if query:
        escaped = query.strip().replace('\\','\\\\').replace('%','\\%').replace('_','\\_')
        conditions.append(or_(VideoWork.title.ilike('%'+escaped+'%', escape='\\'), VideoWork.notes.ilike('%'+escaped+'%', escape='\\')))
    if status == 'active': conditions.append(TaskRecord.status.in_(['queued','running','cancel_requested']))
    elif status != 'all': conditions.append(TaskRecord.status == status)
    if starred: conditions.append(VideoWork.starred == True)
    if template: conditions.append(VideoWork.template_id == template)
    if start: conditions.append(func.julianday(TaskRecord.created_at) >= func.julianday(start))
    if end: conditions.append(func.julianday(TaskRecord.created_at) < func.julianday(end))
    query_rows = select(VideoProduction,TaskRecord,VideoWork).join(TaskRecord,TaskRecord.task_id == VideoProduction.task_id).join(VideoWork,VideoWork.task_id == VideoProduction.task_id).where(*conditions)
    limit, offset = max(1,min(limit,100)), max(0,offset)
    with Session(engine) as session:
        total = session.exec(select(func.count()).select_from(query_rows.subquery())).one()
        rows = session.exec(query_rows.order_by(TaskRecord.created_at.desc(),VideoProduction.task_id.desc()).offset(offset).limit(limit)).all()
        items=[]
        for row, task, work in rows:
            spec, result=loads(row.spec_json,{}), loads(row.result_json,{})
            files=result.get('files',{}) if task.status=='succeeded' else {}
            missing=presence(files)
            url=lambda name: f'/api/video/tasks/{row.task_id}/files/{name}' if name in files and name not in missing else None
            items.append(dict(task=task_dict(task),package_key=row.package_key,revision=row.revision,
                work=metadata(session,row,spec),script_title=spec.get('snapshot',{}).get('document',{}).get('title',''),
                cover_url=url('cover.png'),video_url=url('video.mp4'),subtitles_url=url('subtitles.srt'),bundle_url=url('video_bundle.zip'),
                duration=result.get('duration'),width=result.get('width'),height=result.get('height'),missing_files=missing,
                template_name=spec.get('template',{}).get('name','旧版新闻卡')))
    return dict(items=items,total=total,offset=offset,limit=limit,timezone=zone)


def recovery(engine, task_id):
    """Explicit check: hashes can be expensive, never done by the gallery query."""
    with Session(engine) as session:
        row=session.get(VideoProduction,task_id); task=session.get(TaskRecord,task_id)
        if not row or not task: raise KeyError('视频任务不存在')
        spec=loads(row.spec_json,{})
        points=list(session.exec(select(TaskCheckpoint).where(TaskCheckpoint.task_id==task_id)))
        status, stage, input_hash = task.status, task.stage, row.input_hash
    from .render import valid
    def valid_safe(entry):
        try: return valid(entry)
        except (OSError,KeyError,TypeError): return False
    cached={p.checkpoint_key.split(':',1)[1]:loads(p.payload_json,{}) for p in points}
    completed=cached.get('video_result',{}).get('files',{})
    complete=bool(completed) and all(valid_safe(e) for e in completed.values())
    audio=valid_safe(cached.get('video_audio',{}).get('audio',{}))
    voices=sum(1 for key,value in cached.items() if key.startswith('video_voice_') and valid_safe(value.get('audio',{})))
    from .workbench import canonical, digest
    input_ok=digest(canonical(spec).encode())==input_hash
    checks=[dict(label='固定输入完整性',ok=input_ok,fix='输入校验失败，请恢复数据库备份或重新制作。')]
    for label,entry in [('中文字体',spec.get('font',{}))]+[('图片：'+a.get('original_name',''),a) for a in spec.get('images',[])]+([('上传配音',spec['audio'])] if spec.get('audio') and not audio else []):
        try: ok=Path(entry['path']).is_file() and file_hash(entry['path'])==entry['sha256']
        except (KeyError,OSError): ok=False
        checks.append(dict(label=label,ok=ok,fix='恢复提交时的原文件；如需换素材或字体，请重新制作当前审核稿。'))
    environment=capabilities()
    inputs_ok=all(c['ok'] for c in checks)
    backend=spec.get('options',{}).get('backend')
    voice_ok=audio or backend=='uploaded' or environment.get(backend,False)
    retryable=status in {'failed','cancelled'}
    can_retry=retryable and input_ok and (complete or (environment['ready'] and inputs_ok and voice_ok))
    advice=[]
    if status=='succeeded': advice.append('任务已完成；下载会校验文件。若文件丢失，请恢复运行目录备份，或从当前审核稿另做一版。')
    elif complete: advice.append('完整产物检查点有效，重试可直接恢复结果登记。')
    elif audio: advice.append('完整配音检查点有效，重试可复用配音并重做画面与编码。')
    elif voices: advice.append(f'已验证 {voices} 段配音检查点，重试复用这些片段。')
    else: advice.append('尚无可复用配音，重试将从未完成步骤开始。')
    if not voice_ok: advice.append('需要安装原任务使用的配音后端；换配音方式请重新制作。')
    if backend=='edge' and not audio and not complete: advice.append('Edge TTS 仍需可用网络和有效语音名称，本检查没有调用远端服务。')
    return dict(status=status,stage=stage,can_retry=can_retry,audio_cached=audio,voice_chunks=voices,
                result_cached=complete,checks=checks,environment=environment,advice=advice)
