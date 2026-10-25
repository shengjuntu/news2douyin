from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlmodel import select

from ..collect.management import set_profile_enabled, delete_profile, save_job, delete_job
from ..collect.diagnostics import task_diagnostics
from ..scheduler.cron import next_occurrences
from ..storage.db import session_scope
from ..storage.models import TaskRecord
from ..tasks.service import task_dict
from .schemas.common import JobPayload
from .tasks import task_call


class TrialPayload(BaseModel):
    date_str: str = ''
    timezone: str = 'Asia/Shanghai'
    force_refresh: bool = False
    idempotency_key: str | None = Field(default=None, max_length=128)


class SchedulePreview(BaseModel):
    cron_expr: str = Field(max_length=120)
    timezone: str = Field(default='Asia/Shanghai', max_length=80)


def register_management_routes(app, engine):
    @app.post('/api/profiles/{name}/enable')
    def enable(name: str):
        with session_scope(engine) as session:
            return task_call(set_profile_enabled, session, name, True)

    @app.post('/api/profiles/{name}/disable')
    def disable(name: str):
        with session_scope(engine) as session:
            return task_call(set_profile_enabled, session, name, False)

    @app.delete('/api/profiles/{name}')
    def remove(name: str):
        with session_scope(engine) as session:
            return task_call(delete_profile, session, name)

    @app.post('/api/profiles/{name}/test', status_code=202)
    def test_profile(name: str, payload: TrialPayload):
        task = task_call(app.state.tasks.submit, name,
            {'date_str': payload.date_str, 'timezone': payload.timezone, 'cache_force_refresh': payload.force_refresh},
            trial=True, idempotency_key=payload.idempotency_key)
        return JSONResponse(task, status_code=202, headers={'Location': '/api/tasks/' + task['task_id']})

    @app.get('/api/profiles/{name}/tests')
    def profile_tests(name: str):
        with session_scope(engine) as session:
            return [task_dict(t) for t in session.exec(select(TaskRecord).where(TaskRecord.profile_name == name,
                     TaskRecord.trigger_type == 'profile_test').order_by(TaskRecord.queued_at.desc()).limit(10))]

    @app.get('/api/tasks/{task_id}/diagnostics')
    def diagnostics(task_id: str):
        with session_scope(engine) as session:
            return task_call(task_diagnostics, session, task_id)

    @app.put('/api/jobs/{job_id}')
    def edit_job(job_id: int, payload: JobPayload):
        with session_scope(engine) as session:
            return task_call(save_job, session, payload.model_dump(), job_id)

    @app.delete('/api/jobs/{job_id}')
    def remove_job(job_id: int):
        with session_scope(engine) as session:
            return task_call(delete_job, session, job_id)

    @app.post('/api/jobs/preview')
    def preview(payload: SchedulePreview):
        times = task_call(next_occurrences, payload.cron_expr, payload.timezone)
        return {'next_runs': times, 'timezone': payload.timezone,
                'warning': '' if times else '未来五年没有匹配日期，请调整计划'}
