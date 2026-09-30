from typing import Literal

from pydantic import BaseModel
from fastapi import Query
from fastapi.responses import JSONResponse

from ..editorial.daily import choose, list_picks, build_pick
from ..storage.db import session_scope
from .scripts import script_call
from .tasks import task_call
from ..editorial import daily_tasks


class PickPayload(BaseModel):
    article_key: str
    day: str = ''
    timezone: str = ''
    active: bool = True


class BuildPickPayload(BaseModel):
    mode: Literal['basic', 'llm'] = 'basic'


def register_daily_routes(app, engine, storage_root):
    def accepted(result):
        pending = [r for r in result['items'] if not r['package_key']]
        return JSONResponse(result, status_code=202 if pending else 200)

    @app.post('/api/daily/script-tasks')
    def batch_tasks(payload: daily_tasks.DailyBatchRequest):
        return accepted(task_call(daily_tasks.submit_many, engine, payload.model_dump()))

    @app.get('/api/daily/script-tasks')
    def generation_list(day: str = '', timezone: str = '', limit: int = Query(30, ge=1, le=100)):
        return task_call(daily_tasks.list_generations, engine, day=day, timezone=timezone, limit=limit)

    @app.get('/api/daily/script-tasks/{task_id}')
    def generation_detail(task_id: str):
        return task_call(daily_tasks.detail, engine, task_id)

    @app.post('/api/daily/selections/{selection_key}/script-task')
    def single_task(selection_key: str, payload: daily_tasks.DailyTaskRequest):
        return accepted(task_call(daily_tasks.submit_many, engine, dict(selection_keys=[selection_key], mode=payload.mode)))

    @app.get('/api/daily/selections')
    def selections(day: str = '', timezone: str = ''):
        with session_scope(engine) as session:
            return script_call(list_picks, session, day, timezone)

    @app.post('/api/daily/selections')
    def select_article(payload: PickPayload):
        with session_scope(engine) as session:
            return script_call(choose, session, **payload.model_dump())

    @app.post('/api/daily/selections/{selection_key}/build')
    def build(selection_key: str, payload: BuildPickPayload):
        with session_scope(engine) as session:
            return script_call(build_pick, session, selection_key, storage_root=storage_root, mode=payload.mode)
