from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from ..collect.service import run_collection
from ..storage.models import CollectJob, ScheduleOccurrence, utc_now_iso


def _parse_field(field: str, value: int) -> bool:
    if field == '*':
        return True
    for part in field.split(','):
        part = part.strip()
        if part == '*':
            return True
        if part.startswith('*/'):
            step = int(part[2:])
            if step and value % step == 0:
                return True
            continue
        if '-' in part:
            a, b = part.split('-', 1)
            if int(a) <= value <= int(b):
                return True
            continue
        if str(value) == part:
            return True
    return False


def cron_matches(expr: str, dt: datetime) -> bool:
    parts = expr.split()
    if len(parts) != 5:
        return False
    minute, hour, day, month, dow = parts
    py_dow = (dt.weekday() + 1) % 7
    return (
        _parse_field(minute, dt.minute)
        and _parse_field(hour, dt.hour)
        and _parse_field(day, dt.day)
        and _parse_field(month, dt.month)
        and _parse_field(dow, py_dow)
    )


@dataclass
class SchedulerConfig:
    interval_sec: int = 15
    storage_root: str = 'runs_v7'


class SchedulerService:
    def __init__(self, engine, config: SchedulerConfig | None = None):
        self.engine = engine
        self.config = config or SchedulerConfig()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fired_keys: set[str] = set()
        self._run_lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name='news2douyin-scheduler')
        self._thread.start()
        logger.info('Scheduler started')

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info('Scheduler stopped')

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.exception('Scheduler tick failed')
            self._stop.wait(self.config.interval_sec)

    def tick(self) -> None:
        with self._run_lock:
            from ..storage.db import session_scope
            # Capture one instant so a slow first job does not change the
            # scheduled minute used to evaluate the remaining jobs.
            now = datetime.now(timezone.utc)
            minute_key = now.replace(second=0, microsecond=0).isoformat()
            self._fired_keys = {x for x in self._fired_keys if x.endswith(minute_key)}
            with session_scope(self.engine) as session:
                jobs = list(session.exec(select(CollectJob).where(CollectJob.enabled == True).order_by(CollectJob.id)))
            for job in jobs:
                if self._stop.is_set():
                    break
                try:
                    local_now = now.astimezone(ZoneInfo(job.timezone or 'UTC'))
                    if not cron_matches(job.cron_expr, local_now):
                        continue
                    fired_key = f'{job.id}:{minute_key}'
                    if fired_key in self._fired_keys or not self._claim(job.id, fired_key, minute_key):
                        continue
                    self._fired_keys.add(fired_key)
                    self._run_job(job.id, fired_key)
                except Exception:
                    logger.exception(f'Scheduler job failed: {job.name}')

    def _claim(self, job_id: int, key: str, scheduled_at: str) -> bool:
        from ..storage.db import session_scope
        with session_scope(self.engine) as session:
            session.add(ScheduleOccurrence(occurrence_key=key, job_id=job_id, scheduled_at=scheduled_at))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                if session.get(ScheduleOccurrence, key) is not None:
                    return False
                raise
        return True

    def _run_job(self, job_id: int, key: str) -> None:
        from ..storage.db import session_scope
        with session_scope(self.engine) as session:
            job = session.get(CollectJob, job_id)
            occurrence = session.get(ScheduleOccurrence, key)
            if job is None or not job.enabled:
                occurrence.status = 'skipped'
                occurrence.finished_at = utc_now_iso()
                session.add(occurrence)
                session.commit()
                return
            try:
                logger.info(f'Scheduler firing job={job.name} occurrence={key}')
                run = run_collection(session, job.profile_name, storage_root=self.config.storage_root,
                                     trigger_type='schedule', job_id=job.id)
                status, finished = run.status, run.finished_at or run.started_at
                run_id, error = getattr(run, 'id', None), None
            except Exception as exc:
                session.rollback()
                logger.exception(f'Scheduled collection failed job_id={job_id}')
                status, finished = 'failed', utc_now_iso()
                run_id, error = None, f'{type(exc).__name__}: {exc}'[:4000]
            job = session.get(CollectJob, job_id)
            if job:
                job.last_run_at, job.last_status = finished, status
                session.add(job)
            occurrence = session.get(ScheduleOccurrence, key)
            occurrence.status, occurrence.finished_at = status, finished
            occurrence.run_id, occurrence.error_text = run_id, error
            session.add(occurrence)
            session.commit()
