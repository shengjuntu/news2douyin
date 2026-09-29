from __future__ import annotations

from fastapi import Form, HTTPException, Query, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from ..storage.models import VideoAsset, VideoProduction, TaskRecord
from ..tasks.control import TaskConflict
from ..tasks.service import task_dict
from ..video import production as prod
from ..video import workbench as wb
from ..video.media import capabilities, file_hash


class RenderPayload(BaseModel):
    package_key: str
    expected_version: int = Field(ge=1)
    options: prod.VideoOptions = Field(default_factory=prod.VideoOptions)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)


def video_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (wb.ScriptConflict, TaskConflict) as exc:
        raise HTTPException(409, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(410, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def list_productions(engine, package_key='', limit=50):
    with Session(engine) as session:
        query = select(VideoProduction, TaskRecord).join(TaskRecord, VideoProduction.task_id == TaskRecord.task_id)
        if package_key:
            query = query.where(VideoProduction.package_key == package_key)
        return [{'task': task_dict(task), 'package_key': row.package_key, 'revision': row.revision}
                for row, task in session.exec(query.order_by(TaskRecord.queued_at.desc()).limit(limit))]


def register_video_routes(app, engine, storage_root):
    @app.get('/api/video/capabilities')
    def diagnostics():
        return capabilities()

    @app.post('/api/video/assets', status_code=201)
    def upload(package_key: str = Form(...), kind: str = Form(...), file: UploadFile = File(...)):
        try:
            return video_call(prod.add_asset, engine, storage_root, package_key, kind, file.filename, file.file)
        finally:
            file.file.close()

    @app.get('/api/video/assets')
    def assets(package_key: str, limit: int = Query(100, ge=1, le=500)):
        with Session(engine) as session:
            video_call(wb.get_package, session, package_key)
            return [prod.asset_dict(asset) for asset in session.exec(select(VideoAsset).where(
                    VideoAsset.package_key == package_key).order_by(VideoAsset.created_at.desc(), VideoAsset.asset_id).limit(limit))]

    @app.get('/api/video/assets/{asset_id}')
    def get_asset(asset_id: str):
        with Session(engine) as session:
            asset = session.get(VideoAsset, asset_id)
            if not asset:
                raise HTTPException(404, '素材不存在')
            if video_call(file_hash, asset.storage_path) != asset.sha256:
                raise HTTPException(409, '素材校验失败')
            return FileResponse(asset.storage_path, media_type='image/png' if asset.kind == 'image' else 'audio/wav',
                                headers={'ETag': f'"{asset.sha256}"'})

    @app.post('/api/video/tasks', status_code=202)
    def submit(payload: RenderPayload):
        task = video_call(prod.submit_video, engine, storage_root, payload.package_key,
                          payload.expected_version, payload.options.model_dump(), payload.idempotency_key)
        return JSONResponse(task, status_code=202, headers={'Location': '/api/video/tasks/' + task['task_id']})

    @app.get('/api/video/tasks')
    def list_tasks(package_key: str = '', limit: int = Query(50, ge=1, le=200)):
        return list_productions(engine, package_key, limit)

    @app.get('/api/video/tasks/{task_id}')
    def detail(task_id: str):
        return video_call(prod.production_detail, engine, task_id)

    @app.get('/api/video/tasks/{task_id}/files/{name}')
    def download(task_id: str, name: str, download: bool = False):
        path, entry = video_call(prod.output_file, engine, task_id, name)
        mime = {'video.mp4': 'video/mp4', 'narration.wav': 'audio/wav', 'cover.png': 'image/png',
                'subtitles.srt': 'application/x-subrip', 'video_bundle.zip': 'application/zip'}.get(name, 'application/json')
        return FileResponse(path, media_type=mime, filename=name if download else None,
                            headers={'ETag': f'"{entry["sha256"]}"', 'Cache-Control': 'private, no-cache'})
