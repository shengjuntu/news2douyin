from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from loguru import logger
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..storage.models import (CollectJob, RunRecord, ScheduleOccurrence, TaskRecord,
                              TaskEvent, TaskCheckpoint, TaskItem, utc_now_iso)
from ..storage.utils import dumps, loads
from .control import TaskCancelled, TaskConflict, TaskLeaseLost, TaskWorkerStopping

TERMINAL = frozenset({'succeeded', 'failed', 'cancelled'})
ACTIVE = ('running', 'cancel_requested')


def task_dict(task: TaskRecord) -> dict:
    # Deliberately exclude the frozen provider configuration and lease token.
    fields = ('task_id', 'profile_name', 'status', 'stage', 'progress_current',
              'progress_total', 'attempts', 'max_attempts', 'trigger_type', 'job_id',
              'run_id', 'error_text', 'created_at', 'updated_at', 'started_at', 'finished_at')
    profile = loads(task.profile_json, {})
    return {name: getattr(task, name) for name in fields} | {
        'kind': task.trigger_type if task.trigger_type in {'video', 'script', 'daily_script'} else ('trial' if task.trigger_type == 'profile_test' else 'collect'),
        'collection_date': profile.get('date_str', ''), 'timezone': profile.get('timezone', 'Asia/Shanghai')}


def add_event(session, task, event_type):
    task.updated_at = utc_now_iso()
    session.add(task)
    session.add(TaskEvent(task_id=task.task_id, event_type=event_type,
                          payload_json=dumps(task_dict(task))))


def update_schedule(session, task):
    if task.occurrence_key:
        occurrence = session.get(ScheduleOccurrence, task.occurrence_key)
        if occurrence:
            occurrence.status = task.status
            occurrence.run_id = task.run_id
            occurrence.error_text = task.error_text
            occurrence.finished_at = task.finished_at
            session.add(occurrence)
    if task.job_id:
        # An older task finishing must not overwrite the latest scheduled task.
        session.flush()
        latest = session.exec(select(TaskRecord).where(TaskRecord.job_id == task.job_id)
                              .order_by(TaskRecord.queued_at.desc(), TaskRecord.task_id.desc())).first()
        job = session.get(CollectJob, task.job_id)
        if job and latest and latest.task_id == task.task_id:
            job.last_status = task.status
            job.last_run_at = task.finished_at or task.started_at or task.created_at
            session.add(job)


class TaskService:
    def __init__(self, engine):
        self.engine = engine

    def get(self, task_id):
        with Session(self.engine) as session:
            row = session.get(TaskRecord, task_id)
            if row is None:
                raise KeyError('task not found')
            return task_dict(row)

    def list(self, limit=50):
        with Session(self.engine) as session:
            return [task_dict(t) for t in session.exec(select(TaskRecord)
                    .order_by(TaskRecord.queued_at.desc()).limit(max(1, min(limit, 500))))]

    def submit(self, profile_name, override=None, *, idempotency_key=None,
               max_attempts=3, scheduled=None, trial=False):
        """A scheduled occurrence and its queued task commit together."""
        from ..collect.service import get_profile
        from ..collect.profiles import normalize_profile_dict
        if not 1 <= max_attempts <= 10:
            raise ValueError('max_attempts must be between 1 and 10')
        if idempotency_key is not None and not 1 <= len(idempotency_key) <= 128:
            raise ValueError('idempotency_key must contain 1 to 128 characters')
        key = ('manual:' + idempotency_key) if idempotency_key else None
        if scheduled:
            key = 'schedule:' + scheduled['occurrence_key']
        request_hash = hashlib.sha256(json.dumps(
            [profile_name, override or {}, max_attempts] + (['profile_test'] if trial else []), sort_keys=True,
            ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        with Session(self.engine) as session:
            from ..storage.articles import lock_news
            from ..collect.management import require_profile, profile_enabled
            lock_news(session)
            if key:
                old = session.exec(select(TaskRecord).where(TaskRecord.idempotency_key == key)).first()
                if old:
                    return task_dict(old) if scheduled else self._same_request(old, request_hash)
            if scheduled:
                if session.get(ScheduleOccurrence, scheduled['occurrence_key']):
                    return None  # Legacy occurrence: do not guess whether its effects ran.
                job = session.get(CollectJob, scheduled['job_id'])
                if not job or not job.enabled or job.schedule_type == 'archived' or job.profile_name != profile_name:
                    return None
                from datetime import datetime
                from zoneinfo import ZoneInfo
                from ..scheduler.cron import cron_matches
                local = datetime.fromisoformat(scheduled['scheduled_at'].replace('Z', '+00:00')).astimezone(ZoneInfo(job.timezone))
                if not cron_matches(job.cron_expr, local) or not profile_enabled(session, profile_name):
                    return None
                override = dict(override or {}, date_str=local.date().isoformat(), timezone=job.timezone)
            require_profile(session, profile_name, enabled=True)
            profile = get_profile(session, profile_name)
            profile.update({k: v for k, v in (override or {}).items() if v is not None})
            profile = normalize_profile_dict(profile)
            from ..search.dates import calendar_day, timezone_name
            profile['timezone'] = timezone_name(profile.get('timezone', ''))
            profile['date_str'] = calendar_day(profile.get('date_str', ''), profile['timezone'])
            task = TaskRecord(task_id=uuid4().hex, profile_name=profile_name,
                              profile_json=dumps(profile), request_hash=request_hash,
                              idempotency_key=key, max_attempts=max_attempts)
            if trial:
                task.trigger_type = 'profile_test'
            if scheduled:
                task.trigger_type = 'schedule'
                task.job_id = scheduled['job_id']
                task.occurrence_key = scheduled['occurrence_key']
                session.add(ScheduleOccurrence(**scheduled, status='queued'))
            add_event(session, task, 'queued')
            try:
                update_schedule(session, task)
                session.commit()
            except IntegrityError:
                session.rollback()
                old = session.exec(select(TaskRecord).where(TaskRecord.idempotency_key == key)).first() if key else None
                if old:
                    return task_dict(old) if scheduled else self._same_request(old, request_hash)
                if scheduled and session.get(ScheduleOccurrence, scheduled['occurrence_key']):
                    return None
                raise
            session.refresh(task)
            return task_dict(task)

    @staticmethod
    def _same_request(task, request_hash):
        if task.request_hash != request_hash:
            raise TaskConflict('idempotency_key was already used for a different request')
        return task_dict(task)

    def events(self, task_id, after=0, limit=200):
        self.get(task_id)
        limit = max(1, min(limit, 500))
        with Session(self.engine) as session:
            rows = list(session.exec(select(TaskEvent).where(TaskEvent.task_id == task_id,
                        TaskEvent.id > max(0, after)).order_by(TaskEvent.id).limit(limit + 1)))
            return {'items': [{'id': e.id, 'type': e.event_type, 'created_at': e.created_at,
                              'task': loads(e.payload_json, {})} for e in rows[:limit]],
                    'next_cursor': rows[min(len(rows), limit) - 1].id if rows else max(0, after),
                    'has_more': len(rows) > limit}

    def cancel(self, task_id):
        with Session(self.engine) as session:
            # Update first to serialize against claims and terminal transitions.
            result = session.exec(update(TaskRecord).where(TaskRecord.task_id == task_id,
                       TaskRecord.status.in_(('queued', 'running')))
                       .values(updated_at=utc_now_iso()))
            row = session.get(TaskRecord, task_id)
            if row is None:
                raise KeyError('task not found')
            if result.rowcount:
                row.status = 'cancelled' if row.status == 'queued' else 'cancel_requested'
                if row.status == 'cancelled':
                    row.finished_at = utc_now_iso()
                add_event(session, row, row.status)
                update_schedule(session, row)
            session.commit()
            session.refresh(row)
            return task_dict(row)

    def retry(self, task_id):
        with Session(self.engine) as session:
            result = session.exec(update(TaskRecord).where(TaskRecord.task_id == task_id,
                       TaskRecord.status.in_(('failed', 'cancelled')))
                       .values(updated_at=utc_now_iso()))
            task = session.get(TaskRecord, task_id)
            if task is None:
                raise KeyError('task not found')
            if not result.rowcount:
                raise TaskConflict('only failed or cancelled tasks can be retried')
            if task.trigger_type == 'daily_script':
                from ..editorial.daily_tasks import require_current
                require_current(session, task_id)
            task.status, task.stage = 'queued', 'queued'
            task.lease_owner, task.lease_until = None, 0
            task.error_text, task.finished_at = None, None
            task.max_attempts = max(task.max_attempts, task.attempts + 3)
            task.queued_at = time.time()
            add_event(session, task, 'retried')
            update_schedule(session, task)
            session.commit()
            session.refresh(task)
            return task_dict(task)

    def claim(self, lease_seconds=30):
        with Session(self.engine) as session:
            ids = list(session.exec(select(TaskRecord.task_id).where(TaskRecord.status == 'queued')
                       .order_by(TaskRecord.queued_at, TaskRecord.task_id).limit(20)))
            for task_id in ids:
                now, owner = time.time(), uuid4().hex
                result = session.exec(update(TaskRecord).where(TaskRecord.task_id == task_id,
                         TaskRecord.status == 'queued').values(status='running', stage='starting',
                         lease_owner=owner, lease_until=now + lease_seconds,
                         attempts=TaskRecord.attempts + 1, started_at=utc_now_iso(),
                         finished_at=None, error_text=None, run_id=None))
                if not result.rowcount:
                    session.rollback()
                    continue
                task = session.get(TaskRecord, task_id)
                add_event(session, task, 'started')
                update_schedule(session, task)
                session.commit()
                session.refresh(task)
                return task
        return None

    def renew(self, task_id, owner, lease_seconds):
        with Session(self.engine) as session:
            now = time.time()
            result = session.exec(update(TaskRecord).where(TaskRecord.task_id == task_id,
                       TaskRecord.lease_owner == owner, TaskRecord.status.in_(ACTIVE),
                       TaskRecord.lease_until > now).values(lease_until=now + lease_seconds))
            session.commit()
            return bool(result.rowcount)

    def recover(self):
        with Session(self.engine) as session:
            now = time.time()
            ids = list(session.exec(select(TaskRecord.task_id).where(
                       TaskRecord.status.in_(ACTIVE), TaskRecord.lease_until <= now)))
            for task_id in ids:
                result = session.exec(update(TaskRecord).where(TaskRecord.task_id == task_id,
                           TaskRecord.status.in_(ACTIVE), TaskRecord.lease_until <= now)
                           .values(lease_until=0))
                if not result.rowcount:
                    session.rollback()
                    continue
                task = session.get(TaskRecord, task_id)
                status = ('cancelled' if task.status == 'cancel_requested' else
                          'failed' if task.attempts >= task.max_attempts else 'queued')
                self._finish(session, task, status, 'worker lease expired', 'interrupted')
                session.commit()
            return len(ids)

    @staticmethod
    def _finish(session, task, status, error=None, event_type=None):
        run = session.get(RunRecord, task.run_id) if task.run_id else None
        if run and run.status == 'running':
            run.status = 'interrupted' if event_type == 'interrupted' else status
            run.finished_at, run.error_text = utc_now_iso(), error
            session.add(run)
            # Human-readable exports are best effort; DB is authoritative. A
            # unique attempt directory prevents a recovered task sharing files.
            if run.storage_path:
                try:
                    target = Path(run.storage_path) / 'meta.json'
                    data = {'run_key': run.run_key, 'task_id': task.task_id,
                            'profile': loads(task.profile_json, {}), 'status': run.status,
                            'stats': loads(run.stats_json, {}), 'error_text': error}
                    temporary = target.with_suffix('.json.tmp')
                    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
                    temporary.replace(target)
                except OSError:
                    logger.exception('Could not write terminal run metadata')
        task.status = status
        task.error_text = error
        task.finished_at = utc_now_iso() if status in TERMINAL else None
        task.lease_owner, task.lease_until = None, 0
        if status == 'queued':
            task.queued_at = time.time()
        add_event(session, task, event_type or status)
        update_schedule(session, task)


class TaskContext:
    def __init__(self, service, task, stop_event):
        self.service, self.task_id, self.owner = service, task.task_id, task.lease_owner
        self.stop_event = stop_event

    def check(self):
        with Session(self.service.engine) as session:
            self._validate(session.get(TaskRecord, self.task_id))

    def _validate(self, task, *, allow_cancel=False, allow_stop=False):
        if (not task or task.lease_owner != self.owner or task.status not in ACTIVE
                or task.lease_until <= time.time()):
            raise TaskLeaseLost('task lease lost')
        if task.status == 'cancel_requested' and not allow_cancel:
            raise TaskCancelled('cancellation requested')
        if self.stop_event.is_set() and not allow_stop:
            raise TaskWorkerStopping('worker stopping')

    def fence(self, session, *, allow_cancel=False, allow_stop=False):
        # This UPDATE obtains the write lock in the caller's transaction. Keep it
        # held through article + event + ledger commit: stale owners cannot write.
        now = time.time()
        result = session.exec(update(TaskRecord).where(TaskRecord.task_id == self.task_id,
                    TaskRecord.lease_owner == self.owner, TaskRecord.status.in_(ACTIVE),
                    TaskRecord.lease_until > now).values(updated_at=utc_now_iso())
                    .execution_options(synchronize_session=False))
        if not result.rowcount:
            raise TaskLeaseLost('task lease lost')
        task = session.get(TaskRecord, self.task_id, populate_existing=True)
        self._validate(task, allow_cancel=allow_cancel, allow_stop=allow_stop)
        return task

    def attach_run(self, session, run):
        task = self.fence(session)
        session.add(run)
        session.flush()
        task.run_id = run.id
        add_event(session, task, 'run_created')
        update_schedule(session, task)

    def progress(self, stage, current=0, total=0):
        with Session(self.service.engine) as session:
            task = self.fence(session)
            task.stage, task.progress_current, task.progress_total = stage, current, total
            add_event(session, task, 'progress')
            session.commit()

    def checkpoint(self, name):
        self.check()
        with Session(self.service.engine) as session:
            row = session.get(TaskCheckpoint, self.task_id + ':' + name)
            return loads(row.payload_json, None) if row else None

    def save_checkpoint(self, name, value, *, replace=False):
        with Session(self.service.engine) as session:
            self.fence(session)
            key = self.task_id + ':' + name
            row = session.get(TaskCheckpoint, key) if replace else None
            if row:
                row.payload_json = dumps(value)
            else:
                row = TaskCheckpoint(checkpoint_key=key, task_id=self.task_id, payload_json=dumps(value))
            session.add(row)
            session.commit()

    def item_done(self, session, index):
        return session.get(TaskItem, f'{self.task_id}:{index}') is not None

    def record_item(self, session, index, article_key, disposition, duplicate=False, event_created=False):
        session.add(TaskItem(item_key=f'{self.task_id}:{index}', task_id=self.task_id,
                    input_index=index, article_key=article_key, disposition=disposition,
                    is_duplicate=duplicate, event_created=event_created))

    def items(self, session):
        return list(session.exec(select(TaskItem).where(TaskItem.task_id == self.task_id)
                                 .order_by(TaskItem.input_index)))

    def complete(self, session, run=None):
        task = self.fence(session)
        task.stage = 'complete'
        task.progress_current = task.progress_total
        # run and task success are committed by the pipeline in one transaction.
        if run is not None:
            session.add(run)
        self.service._finish(session, task, 'succeeded')

    def terminate(self, status, error=None, interrupted=False):
        with Session(self.service.engine) as session:
            task = self.fence(session, allow_cancel=True, allow_stop=True)
            if task.status == 'cancel_requested':
                status = 'cancelled'
            if status == 'queued' and task.attempts >= task.max_attempts:
                status = 'failed'
            self.service._finish(session, task, status, error, 'interrupted' if interrupted else None)
            session.commit()
