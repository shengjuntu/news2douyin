from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from loguru import logger
from sqlmodel import select

from ..collect.service import run_collection
from ..storage.models import CollectJob


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
            with session_scope(self.engine) as session:
                jobs = list(session.exec(select(CollectJob).where(CollectJob.enabled == True)))
                for job in jobs:
                    # Evaluate cron in the job's own timezone (falls back to UTC on bad values).
                    try:
                        tz = ZoneInfo(job.timezone or 'UTC')
                    except Exception:
                        tz = ZoneInfo('UTC')
                    now = datetime.now(tz)
                    if not cron_matches(job.cron_expr, now):
                        continue
                    minute_key = now.strftime('%Y-%m-%d %H:%M')
                    fired_key = f'{job.id}:{minute_key}'
                    if fired_key in self._fired_keys:
                        continue
                    self._fired_keys.add(fired_key)
                    logger.info(f'Scheduler firing job={job.name} cron={job.cron_expr} tz={job.timezone}')
                    run = run_collection(session, job.profile_name, storage_root=self.config.storage_root, trigger_type='schedule', job_id=job.id)
                    job.last_run_at = run.finished_at or run.started_at
                    job.last_status = run.status
                    session.add(job)
                    session.commit()
                # prune old fired markers
                prune_key = datetime.now().strftime('%Y-%m-%d %H:%M')
                self._fired_keys = {x for x in self._fired_keys if x.endswith(prune_key)}
