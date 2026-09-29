"""Task HTTP API. The old blocking endpoint remains as a compatibility adapter."""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from sqlmodel import Session

from ..storage.models import RunRecord
from ..tasks.control import TaskConflict
from ..tasks.service import TERMINAL


class CollectTaskPayload(BaseModel):
    profile_name: str
    override: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=128)
    max_attempts: int = Field(default=3, ge=1, le=10)


def task_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except TaskConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def register_task_routes(app, service, worker, run_to_dict):
    def submit(payload):
        return task_call(service.submit, payload.profile_name, payload.override,
                         idempotency_key=payload.idempotency_key, max_attempts=payload.max_attempts)

    def accepted(task):
        return JSONResponse(task, status_code=202, headers={'Location': '/api/tasks/' + task['task_id']})

    @app.post('/api/tasks/collect', status_code=202)
    def collect_task(payload: CollectTaskPayload):
        return accepted(submit(payload))

    @app.get('/api/tasks')
    def list_tasks(limit: int = Query(50, ge=1, le=500)):
        return service.list(limit)

    @app.get('/api/tasks/{task_id}')
    def get_task(task_id: str):
        return task_call(service.get, task_id)

    @app.post('/api/tasks/{task_id}/cancel')
    def cancel(task_id: str):
        return task_call(service.cancel, task_id)

    @app.post('/api/tasks/{task_id}/retry', status_code=202)
    def retry(task_id: str):
        return accepted(task_call(service.retry, task_id))

    @app.get('/api/tasks/{task_id}/events')
    def events(task_id: str, after: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500)):
        return task_call(service.events, task_id, after, limit)

    @app.get('/api/tasks/{task_id}/stream')
    async def stream(task_id: str, request: Request, after: int = Query(0, ge=0)):
        await run_in_threadpool(task_call, service.get, task_id)
        try:
            cursor = max(after, int(request.headers.get('last-event-id', '0')))
            if cursor < 0:
                raise ValueError()
        except ValueError:
            raise HTTPException(400, 'Last-Event-ID must be a nonnegative integer')

        async def generate():
            nonlocal cursor
            last_ping = time.monotonic()
            while not await request.is_disconnected():
                # Read state before events, so a terminal transition cannot be
                # missed between draining events and closing the stream.
                task = await run_in_threadpool(service.get, task_id)
                page = await run_in_threadpool(service.events, task_id, cursor)
                for event in page['items']:
                    cursor = event['id']
                    yield f"id: {cursor}\nevent: task\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                if task['status'] in TERMINAL and not page['has_more']:
                    # Named close event prevents EventSource reconnect loops.
                    yield 'event: end\ndata: {}\n\n'
                    return
                if page['has_more']:
                    continue
                if time.monotonic() - last_ping > 10:
                    yield ': heartbeat\n\n'
                    last_ping = time.monotonic()
                await asyncio.sleep(0.5)

        return StreamingResponse(generate(), media_type='text/event-stream',
                                 headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})

    @app.post('/api/collect/run-now')
    def run_now(payload: CollectTaskPayload, wait: bool = True,
                timeout: float = Query(290, gt=0, le=3600)):
        if wait and not worker.running:
            raise HTTPException(503, 'local worker is not running; use wait=false to enqueue')
        task = submit(payload)
        if not wait:
            return accepted(task)
        deadline = time.monotonic() + timeout
        while task['status'] not in TERMINAL:
            if time.monotonic() >= deadline:
                raise HTTPException(504, {'message': 'task continues in background', 'task_id': task['task_id']},
                                    headers={'Location': '/api/tasks/' + task['task_id']})
            time.sleep(0.1)
            task = service.get(task['task_id'])
        if task['status'] != 'succeeded':
            raise HTTPException(409 if task['status'] == 'cancelled' else 500,
                                {'task_id': task['task_id'], 'status': task['status'], 'error': task['error_text']})
        with Session(service.engine) as session:
            return run_to_dict(session.get(RunRecord, task['run_id']))
