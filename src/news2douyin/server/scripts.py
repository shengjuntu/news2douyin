from __future__ import annotations

from typing import Literal

from fastapi import HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlmodel import select

from ..storage.db import session_scope
from ..storage.models import ScriptRevision, ScriptReviewEvent, ScriptExport
from ..video.service import build_script_package
from ..video import workbench as wb
from ..editorial import generation
from .schemas.common import ScriptBuildPayload
from pathlib import Path


class SaveScriptPayload(BaseModel):
    expected_version: int = Field(ge=1)
    document: wb.ScriptDocument
    change_note: str = Field(default='', max_length=2000)


class VersionPayload(BaseModel):
    expected_version: int = Field(ge=1)


class RestorePayload(VersionPayload):
    change_note: str = Field(default='', max_length=2000)


class ReviewPayload(VersionPayload):
    action: Literal['submit', 'approve', 'request_changes']
    note: str = Field(default='', max_length=2000)
    reviewer: str = Field(default='', max_length=100)
    sources_checked: bool = False
    wording_checked: bool = False


def script_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except wb.ScriptConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(410, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def register_script_routes(app, engine, storage_root):
    from .tasks import task_call
    from fastapi.responses import JSONResponse

    @app.post('/api/events/{event_key}/script-tasks', status_code=202)
    def generate(event_key: str, payload: generation.GenerationRequest):
        task = task_call(generation.submit, engine, event_key, payload.model_dump())
        return JSONResponse(task, status_code=202, headers={'Location': '/api/script-tasks/' + task['task_id']})

    @app.get('/api/script-tasks')
    def list_generation_tasks(event_key: str = '', limit: int = Query(50, ge=1, le=100)):
        return generation.list_generations(engine, event_key, limit)

    @app.get('/api/script-tasks/{task_id}')
    def generation_task(task_id: str):
        return task_call(generation.detail, engine, task_id)

    @app.post('/api/scripts/build')
    def build(payload: ScriptBuildPayload):
        with session_scope(engine) as session:
            package = script_call(build_script_package, session, payload.event_key,
                     profile_name=payload.profile_name, output_root=Path(storage_root) / 'packages')
            return wb.script_detail(session, package.package_key)

    @app.get('/api/scripts')
    def list_scripts(event_key: str = '', limit: int = Query(50, ge=1, le=200)):
        with session_scope(engine) as session:
            return wb.list_scripts(session, event_key=event_key, limit=limit)

    @app.get('/api/scripts/{package_key}')
    def get_script(package_key: str):
        with session_scope(engine) as session:
            return script_call(wb.script_detail, session, package_key)

    @app.post('/api/scripts/{package_key}/adopt')
    def adopt(package_key: str):
        with session_scope(engine) as session:
            return script_call(wb.adopt_legacy, session, package_key)

    @app.put('/api/scripts/{package_key}')
    def save(package_key: str, payload: SaveScriptPayload):
        with session_scope(engine) as session:
            return script_call(wb.save_script, session, package_key, expected_version=payload.expected_version,
                               document=payload.document.model_dump(), change_note=payload.change_note)

    @app.get('/api/scripts/{package_key}/revisions')
    def revisions(package_key: str, before: int | None = Query(None, ge=1), limit: int = Query(50, ge=1, le=200)):
        with session_scope(engine) as session:
            script_call(wb.get_package, session, package_key)
            query = select(ScriptRevision).where(ScriptRevision.package_key == package_key)
            if before is not None:
                query = query.where(ScriptRevision.revision < before)
            return [wb.revision_dict(r) for r in session.exec(query.order_by(ScriptRevision.revision.desc()).limit(limit))]

    @app.get('/api/scripts/{package_key}/revisions/{revision}')
    def revision(package_key: str, revision: int):
        with session_scope(engine) as session:
            return wb.revision_dict(script_call(wb.get_revision, session, package_key, revision), full=True)

    @app.post('/api/scripts/{package_key}/revisions/{revision}/restore')
    def restore(package_key: str, revision: int, payload: RestorePayload):
        with session_scope(engine) as session:
            return script_call(wb.save_script, session, package_key, expected_version=payload.expected_version,
                               document=None, change_note=payload.change_note, restore_revision=revision)

    @app.post('/api/scripts/{package_key}/review')
    def review(package_key: str, payload: ReviewPayload):
        with session_scope(engine) as session:
            return script_call(wb.review_script, session, package_key, expected_version=payload.expected_version,
                               action=payload.action, note=payload.note, reviewer=payload.reviewer,
                               checks={'sources_checked': payload.sources_checked, 'wording_checked': payload.wording_checked})

    @app.get('/api/scripts/{package_key}/reviews')
    def reviews(package_key: str, before: int | None = Query(None, ge=1), limit: int = Query(100, ge=1, le=200)):
        with session_scope(engine) as session:
            script_call(wb.get_package, session, package_key)
            query = select(ScriptReviewEvent).where(ScriptReviewEvent.package_key == package_key)
            if before is not None:
                query = query.where(ScriptReviewEvent.id < before)
            return [wb.review_dict(r) for r in session.exec(query.order_by(ScriptReviewEvent.id.desc()).limit(limit))]

    @app.post('/api/scripts/{package_key}/exports')
    def export(package_key: str, payload: VersionPayload):
        with session_scope(engine) as session:
            return script_call(wb.export_script, session, package_key, expected_version=payload.expected_version)

    @app.get('/api/scripts/{package_key}/exports')
    def exports(package_key: str, limit: int = Query(100, ge=1, le=200)):
        with session_scope(engine) as session:
            script_call(wb.get_package, session, package_key)
            return [wb.export_dict(row) for row in session.exec(select(ScriptExport).where(
                    ScriptExport.package_key == package_key).order_by(ScriptExport.state_version.desc()).limit(limit))]

    @app.get('/api/scripts/{package_key}/exports/{export_key}/download')
    def download(package_key: str, export_key: str):
        with session_scope(engine) as session:
            row, content = script_call(wb.read_export, session, package_key, export_key)
            # Filename is generated server-side and excludes user-controlled titles.
            return Response(content, media_type='application/zip', headers={
                'Content-Disposition': f'attachment; filename="{row.export_key}.zip"',
                'ETag': f'"{row.archive_sha256}"', 'Cache-Control': 'private, no-cache'})
