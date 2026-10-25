from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from loguru import logger
from sqlmodel import select

from ..tasks.service import TaskService
from ..storage.models import CollectJob, utc_now_iso


from .cron import cron_matches


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
        self.tasks = TaskService(engine)

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
                jobs = list(session.exec(select(CollectJob).where(CollectJob.enabled == True, CollectJob.schedule_type != 'archived').order_by(CollectJob.id)))
            for job in jobs:
                if self._stop.is_set():
                    break
                try:
                    local_now = now.astimezone(ZoneInfo(job.timezone or 'UTC'))
                    if not cron_matches(job.cron_expr, local_now):
                        continue
                    fired_key = f'{job.id}:{minute_key}'
                    if fired_key in self._fired_keys:
                        continue
                    self.tasks.submit(job.profile_name, {'date_str': local_now.date().isoformat(), 'timezone': job.timezone}, scheduled={
                        'job_id': job.id, 'occurrence_key': fired_key, 'scheduled_at': minute_key})
                    self._fired_keys.add(fired_key)
                except Exception:
                    logger.exception(f'Scheduler job failed: {job.name}')
                    with session_scope(self.engine) as session:
                        current = session.get(CollectJob, job.id)
                        if current:
                            current.last_status, current.last_run_at = 'failed', utc_now_iso()
                            session.add(current)
                            session.commit()
