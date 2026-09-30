from __future__ import annotations

import io
import json
from pathlib import Path
import re
import shutil
import warnings
import wave
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..storage.models import VideoAsset, VideoProduction, TaskRecord, ScriptExport
from ..tasks.service import add_event, task_dict, TaskService
from ..tasks.control import TaskConflict
from . import workbench as wb
from .media import file_hash, font_path, capabilities
from .subtitles import parse_srt

PRESETS = {'preview': (360, 640, 12), 'portrait': (720, 1280, 24), 'fullhd': (1080, 1920, 24)}


class VideoOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    backend: Literal['espeak', 'edge', 'uploaded'] = 'espeak'
    voice: str = Field(default='', max_length=100, pattern=r'^[A-Za-z0-9_-]*$')
    rate: int = Field(default=0, ge=-50, le=100)
    preset: Literal['preview', 'portrait', 'fullhd'] = 'portrait'
    image_ids: list[str] = Field(default_factory=list, max_length=12)
    audio_id: str | None = None
    subtitles: str = Field(default='', max_length=80000)


def asset_dict(asset):
    return {name: getattr(asset, name) for name in ('asset_id', 'package_key', 'kind', 'original_name', 'sha256', 'size_bytes', 'created_at')} | {
        'metadata': json.loads(asset.metadata_json), 'url': f'/api/video/assets/{asset.asset_id}'}


def add_asset(engine, storage_root, package_key, kind, filename, source):
    if kind not in {'image', 'audio'}:
        raise ValueError('素材类型只能是 image 或 audio')
    with Session(engine) as session:
        wb.get_package(session, package_key)
    root = Path(storage_root).resolve() / 'video_assets'
    root.mkdir(parents=True, exist_ok=True)
    asset_id = 'asset_' + uuid4().hex
    temp = root / (asset_id + '.upload')
    output = root / (asset_id + ('.png' if kind == 'image' else '.wav'))
    size = 0
    committed = False
    try:
        with temp.open('xb') as dest:
            while block := source.read(1024 * 1024):
                size += len(block)
                if size > (20 if kind == 'image' else 50) * 1024 * 1024:
                    raise ValueError('图片不能超过 20 MiB，配音不能超过 50 MiB')
                dest.write(block)
        if kind == 'image':
            try:
                from PIL import Image, ImageOps
            except ImportError as exc:
                raise ValueError('请先安装 news2douyin[video]') from exc
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter('error', Image.DecompressionBombWarning)
                    with Image.open(temp) as image:
                        if image.width * image.height > 24000000 or min(image.size) < 16:
                            raise ValueError('图片尺寸需至少 16×16，且不超过 2400 万像素')
                        image.load()
                        image = ImageOps.exif_transpose(image).convert('RGB')
                        image.thumbnail((2160, 3840))
                        image.save(output, format='PNG')
                        metadata = {'width': image.width, 'height': image.height}
            except (OSError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
                raise ValueError('无效或过大的图片，请上传 PNG、JPEG 或 WebP') from exc
        else:
            try:
                with wave.open(str(temp)) as audio:
                    duration = audio.getnframes() / audio.getframerate()
                    if not 0.2 <= duration <= 600 or audio.getnchannels() not in (1, 2) or not 8000 <= audio.getframerate() <= 96000:
                        raise ValueError('配音需为 0.2–600 秒、单/双声道 PCM WAV')
                    declared = audio.getnframes() * audio.getnchannels() * audio.getsampwidth()
                    if declared > size or len(audio.readframes(audio.getnframes())) != declared:
                        raise ValueError('WAV 文件数据不完整')
                    metadata = {'duration': duration, 'sample_rate': audio.getframerate(), 'channels': audio.getnchannels()}
            except (wave.Error, EOFError) as exc:
                raise ValueError('请上传未压缩 PCM WAV 配音') from exc
            temp.replace(output)
        row = VideoAsset(asset_id=asset_id, package_key=package_key, kind=kind,
                         original_name=re.split(r'[/\\]', filename or 'asset')[-1][:180],
                         storage_path=str(output), sha256=file_hash(output), size_bytes=output.stat().st_size,
                         metadata_json=wb.canonical(metadata))
        with Session(engine) as session:
            session.add(row)
            session.commit()
            committed = True
            session.refresh(row)
            return asset_dict(row)
    finally:
        temp.unlink(missing_ok=True)
        if not committed:
            output.unlink(missing_ok=True)


def submit_video(engine, storage_root, package_key, expected_version, options, idempotency_key=None):
    options = VideoOptions.model_validate(options).model_dump()
    if idempotency_key is not None and not 1 <= len(idempotency_key) <= 128:
        raise ValueError('幂等键长度应为 1–128')
    key = 'video:' + idempotency_key if idempotency_key else None
    request_hash = wb.digest(wb.canonical([package_key, expected_version, options]).encode())
    with Session(engine) as session:
        if key:
            old = session.exec(select(TaskRecord).where(TaskRecord.idempotency_key == key)).first()
            if old:
                return TaskService._same_request(old, request_hash)
    diagnostics = capabilities()
    if not diagnostics['ready']:
        raise ValueError('; '.join(diagnostics['errors']))
    if options['backend'] != 'uploaded' and not diagnostics[options['backend']]:
        raise ValueError('所选配音后端未安装，请查看视频环境检查')
    # Produce the immutable approved handoff, then re-lock its state while
    # enqueuing: an intervening edit must not silently render unseen content.
    with Session(engine) as session:
        exported = wb.export_script(session, package_key, expected_version=expected_version)
    with Session(engine) as session:
        wb._lock(session, package_key, expected_version)
        export = session.get(ScriptExport, exported['export_key'])
        revision = wb.get_revision(session, package_key, export.revision)
        if wb.digest(revision.payload_json.encode()) != export.content_hash:
            raise ValueError('脚本快照校验失败')
        snapshot = json.loads(revision.payload_json)
        narration = snapshot['document']['script_text']
        if len(narration) > 6000:
            raise ValueError('最小视频流程支持最多 6000 字符，请先拆分脚本')
        if len(set(options['image_ids'])) != len(options['image_ids']):
            raise ValueError('图片素材不能重复选择')
        images, audio = [], None
        for asset_id, kind in [(k, 'image') for k in options['image_ids']] + ([(options['audio_id'], 'audio')] if options['audio_id'] else []):
            asset = session.get(VideoAsset, asset_id)
            if not asset or asset.package_key != package_key or asset.kind != kind:
                raise ValueError('素材不存在、类型错误或不属于当前脚本')
            data = asset_dict(asset) | {'path': asset.storage_path}
            if kind == 'image':
                images.append(data)
            else:
                audio = data
        if options['backend'] == 'uploaded':
            if audio is None:
                raise ValueError('请上传配音并提供匹配的 SRT')
            cues = parse_srt(options['subtitles'], narration)
            if cues[-1]['end'] > audio['metadata']['duration'] + 0.03:
                raise ValueError('字幕结束时间超过配音时长')
        elif audio or options['subtitles']:
            raise ValueError('自动配音时请清空上传配音和 SRT')
        else:
            cues = None
        options['voice'] = options['voice'] or ('cmn' if options['backend'] == 'espeak' else 'zh-CN-XiaoxiaoNeural')
        font = font_path()
        spec = {'schema_version': 1, 'snapshot': snapshot, 'script_hash': export.content_hash,
                'export_key': export.export_key, 'options': options, 'images': images,
                'audio': audio, 'cues': cues, 'font': {'path': str(font), 'sha256': file_hash(font)}}
        task_id = uuid4().hex
        task = TaskRecord(task_id=task_id, profile_name=package_key, trigger_type='video',
                          profile_json='{"kind":"video"}', request_hash=request_hash, idempotency_key=key)
        production = VideoProduction(task_id=task_id, package_key=package_key, revision=revision.revision,
                                     export_key=export.export_key, input_hash=wb.digest(wb.canonical(spec).encode()),
                                     spec_json=wb.canonical(spec))
        session.add(production)
        add_event(session, task, 'queued')
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            old = session.exec(select(TaskRecord).where(TaskRecord.idempotency_key == key)).first() if key else None
            if old:
                return TaskService._same_request(old, request_hash)
            raise
        session.refresh(task)
        return task_dict(task)


def production_detail(engine, task_id):
    with Session(engine) as session:
        row = session.get(VideoProduction, task_id)
        if row is None:
            raise KeyError('video task not found')
        task = session.get(TaskRecord, task_id)
        spec = json.loads(row.spec_json)
        result = json.loads(row.result_json)
        return {'task': task_dict(task), 'package_key': row.package_key, 'revision': row.revision,
                'title': spec['snapshot']['document']['title'], 'options': spec['options'],
                'input_hash': row.input_hash, 'result': {k: v for k, v in result.items() if k != 'files'},
                'files': [{'name': name, 'sha256': entry['sha256'], 'size_bytes': entry['size_bytes'],
                           'url': f'/api/video/tasks/{task_id}/files/{name}'} for name, entry in result.get('files', {}).items()]}


def output_file(engine, task_id, name):
    with Session(engine) as session:
        row = session.get(VideoProduction, task_id)
        if not row:
            raise KeyError('video task not found')
        task = session.get(TaskRecord, task_id)
        if task.status != 'succeeded':
            raise TaskConflict('视频尚未完成')
        entry = json.loads(row.result_json).get('files', {}).get(name)
        if not entry:
            raise KeyError('video file not found')
        path = Path(entry['path'])
        if not path.is_file():
            raise FileNotFoundError('视频产物丢失，请恢复运行目录备份')
        if file_hash(path) != entry['sha256']:
            raise TaskConflict('视频产物校验失败')
        return path, entry
