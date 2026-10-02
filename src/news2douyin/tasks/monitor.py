"""Read-only queue and task history. Queue ranks always use the full waiting set."""
from __future__ import annotations

from datetime import datetime, timezone as utc
import math
import time
from urllib.parse import urlencode

from sqlalchemy import func, or_
from sqlmodel import Session, select

from ..search.dates import date_range, timezone_name
from ..storage.models import TaskRecord, ScriptGeneration, DailyScriptGeneration, VideoProduction
from .service import ACTIVE, TERMINAL, task_dict

KINDS = {'all', 'collect', 'trial', 'daily_script', 'script', 'video'}
STATUSES = {'all', 'active', 'queued', 'running', 'cancel_requested', *TERMINAL}


def _read_session(engine):
    session = Session(engine)
    try:
        # Python sqlite's legacy transaction mode does not BEGIN for SELECT.
        # Hold one short read snapshot for counts, ranks and displayed rows.
        if engine.dialect.name == 'sqlite':
            session.connection().exec_driver_sql('BEGIN')
    except Exception:
        session.close()
        raise
    return session


def _epoch(value):
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=utc.utc)
        return moment.timestamp()
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None


def _elapsed(start, end):
    if start is None or end is None or not math.isfinite(start) or not math.isfinite(end):
        return None
    return max(0, int(end - start))


def timing(task, now):
    # queued_at changes on retry/recovery; created_at is the original submission.
    queued_at = task.queued_at if task.queued_at and task.queued_at > 0 else None
    return dict(
        queued_seconds=_elapsed(queued_at, now) if task.status == 'queued' else None,
        execution_seconds=_elapsed(_epoch(task.started_at), now) if task.status in ACTIVE else None,
        total_seconds=_elapsed(_epoch(task.created_at), _epoch(task.finished_at) if task.status in TERMINAL else now),
        needs_recovery=task.status in ACTIVE and (not task.lease_until or task.lease_until <= now))


def _queue():
    return select(TaskRecord.task_id.label('queued_id'),
        func.row_number().over(order_by=(TaskRecord.queued_at, TaskRecord.task_id)).label('position'))\
        .where(TaskRecord.status == 'queued').subquery()


def _item(task, rank, now):
    return dict(task=task_dict(task), queue_position=int(rank) if rank is not None else None,
                task_url='/tasks/' + task.task_id, timing=timing(task, now), result_url=None,
                result_label=None, diagnostics_url=None)


def _links(session, items):
    """Batch result lookup without parsing frozen documents or touching files."""
    ids = [item['task']['task_id'] for item in items]
    if not ids:
        return
    scripts = dict(session.exec(select(ScriptGeneration.task_id, ScriptGeneration.package_key)
                                .where(ScriptGeneration.task_id.in_(ids))).all())
    scripts.update(dict(session.exec(select(DailyScriptGeneration.task_id, DailyScriptGeneration.package_key)
                                     .where(DailyScriptGeneration.task_id.in_(ids))).all()))
    videos = set(session.exec(select(VideoProduction.task_id).where(VideoProduction.task_id.in_(ids))).all())
    for item in items:
        task = item['task']; key = task['task_id']; kind = task['kind']
        if scripts.get(key):
            item.update(result_url='/scripts/' + scripts[key], result_label='打开草稿')
        elif key in videos:
            item.update(result_url='/videos/' + key, result_label='视频与产物')
        elif kind == 'collect' and task['status'] in TERMINAL:
            item.update(result_url='/daily?' + urlencode(dict(task_id=key, day=task['collection_date'], timezone=task['timezone'])),
                        result_label='选择本次新闻')
        if kind in {'collect', 'trial'}:
            item['diagnostics_url'] = '/tasks/' + key + '/diagnostics'


def _summary(session, now, local_worker_running):
    counts = {name: 0 for name in ('queued', 'running', 'cancel_requested', 'succeeded', 'failed', 'cancelled')}
    counts.update(dict(session.exec(select(TaskRecord.status, func.count()).group_by(TaskRecord.status)).all()))
    counts['active'] = counts['queued'] + counts['running'] + counts['cancel_requested']
    counts['total'] = sum(value for key, value in counts.items() if key not in {'active', 'total'})
    stale = session.exec(select(func.count()).select_from(TaskRecord).where(
        TaskRecord.status.in_(ACTIVE), TaskRecord.lease_until <= now)).one()
    executing = list(session.exec(select(TaskRecord).where(TaskRecord.status.in_(ACTIVE))
                    .order_by(func.julianday(TaskRecord.started_at), TaskRecord.task_id).limit(5)).all())
    return dict(counts=counts, local_worker_running=bool(local_worker_running),
                executing_count=counts['running'] + counts['cancel_requested'], needs_recovery_count=stale,
                executing=[_item(t, None, now) for t in executing], snapshot_at=datetime.fromtimestamp(now,utc.utc).isoformat())


def task_page(engine, *, query='', kind='all', status='all', date_from='', date_to='', timezone='',
              offset=0, limit=25, local_worker_running=False):
    if kind not in KINDS: raise ValueError('无效任务类型')
    if status not in STATUSES: raise ValueError('无效任务状态')
    if not isinstance(query, str) or len(query) > 200: raise ValueError('搜索词最多 200 字符')
    if not 1 <= limit <= 100 or offset < 0: raise ValueError('每页需为 1–100 条，起始位置不得为负数')
    zone = timezone_name(timezone)
    start, end = date_range('custom', date_from, date_to, zone)
    conditions = []
    term = query.strip()
    if term:
        escaped = term.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        conditions.append(or_(TaskRecord.profile_name.ilike('%'+escaped+'%', escape='\\'),
                              TaskRecord.task_id.ilike('%'+escaped+'%', escape='\\')))
    if kind == 'collect': conditions.append(TaskRecord.trigger_type.notin_(['profile_test', 'daily_script', 'script', 'video']))
    elif kind != 'all': conditions.append(TaskRecord.trigger_type == ('profile_test' if kind=='trial' else kind))
    if status == 'active': conditions.append(TaskRecord.status.in_(['queued', *ACTIVE]))
    elif status != 'all': conditions.append(TaskRecord.status == status)
    if start: conditions.append(func.julianday(TaskRecord.created_at) >= func.julianday(start))
    if end: conditions.append(func.julianday(TaskRecord.created_at) < func.julianday(end))
    now = time.time(); queue = _queue()
    statement = select(TaskRecord, queue.c.position).outerjoin(queue, TaskRecord.task_id == queue.c.queued_id).where(*conditions)
    order = [TaskRecord.queued_at, TaskRecord.task_id] if status == 'queued' else [func.julianday(TaskRecord.created_at).desc(), TaskRecord.task_id.desc()]
    with _read_session(engine) as session:
        summary = _summary(session, now, local_worker_running)
        total = session.exec(select(func.count()).select_from(TaskRecord).where(*conditions)).one()
        rows = session.exec(statement.order_by(*order).offset(offset).limit(limit)).all()
        items = [_item(task, rank, now) for task, rank in rows]; _links(session, items)
    return dict(items=items, total=total, offset=offset, limit=limit, timezone=zone,
                order='queue' if status=='queued' else 'newest_submission', summary=summary)


def task_position(engine, task_id, *, local_worker_running=False):
    now = time.time(); queue = _queue()
    with _read_session(engine) as session:
        pair = session.exec(select(TaskRecord, queue.c.position).outerjoin(queue, TaskRecord.task_id == queue.c.queued_id)
                            .where(TaskRecord.task_id == task_id)).first()
        if not pair: raise KeyError('任务不存在')
        result = _item(pair[0], pair[1], now); _links(session, [result])
        result['summary'] = _summary(session, now, local_worker_running)
        return result
