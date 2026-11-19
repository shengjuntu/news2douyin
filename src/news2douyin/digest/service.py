"""Publish an atomic, versioned reading list for each Beijing calendar day."""
from __future__ import annotations

from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
import time
from uuid import uuid4

from sqlalchemy import case, or_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from ..storage.models import utc_now_iso
from .calendar import BEIJING, day_bounds_utc, days_before
from .models import DailyDigestIssue

log = logging.getLogger(__name__)
HISTORY_DAYS = 7
LEASE_SECONDS = 15 * 60
EMPTY_RETRY_SECONDS = 60 * 60
FAILURE_RETRY_SECONDS = 5 * 60
LIVE_REFRESH_SECONDS = 15 * 60

# The previous edition stays in the same row while the worker owns a lease.
# Unmodified 0.34 rows are already valid, final editions.
PUBLISHED = ('ready', 'live', 'refresh_pending', 'refreshing', 'refreshing_again', 'refresh_failed')
RUNNING = ('building', 'building_again', 'refreshing', 'refreshing_again')
QUEUED = ('pending', 'refresh_pending')
FAILED = ('failed', 'refresh_failed')


def default_day(now: datetime | None = None) -> date:
    return (now or datetime.now(BEIJING)).astimezone(BEIJING).date()


def last_closed_day(now: datetime | None = None) -> date:
    return days_before(default_day(now), 1)


def issue_metadata(day: date, stored_status: str | None, requested: bool, generated_at: str,
                   error: str, stats: dict) -> dict:
    """Describe both the published edition and current job without loading cards."""
    published = stored_status in PUBLISHED
    if stored_status in RUNNING:
        build_status = 'building'
    elif stored_status in FAILED:
        build_status = 'failed'
    elif stored_status in QUEUED:
        build_status = 'pending'
    else:
        build_status = 'idle'
    publication = stats.get('publication') or {}
    status = 'ready' if published else (build_status if stored_status else 'pending')
    return dict(day=day.isoformat(), status=status, build_status=build_status,
                requested=requested, generated_at=generated_at, error=error,
                revision=publication.get('revision', int(published)),
                backfill_id=(stats.get('sources') or {}).get('backfill_id') or '',
                edition_kind=publication.get('kind') or ('intraday' if day == default_day() else 'closed'),
                cutoff_at=publication.get('cutoff_at', ''))


def read_issue(session: Session, day: date, *, model=DailyDigestIssue) -> dict:
    row = session.get(model, day.isoformat())
    if row is None:
        return dict(issue_metadata(day, None, False, '', '', {}), items=[], stats={})
    stats = json.loads(row.stats_json)
    return dict(issue_metadata(day, row.status, row.requested, row.generated_at, row.error, stats),
                items=json.loads(row.items_json), stats=stats)


def read_issue_state(session: Session, day: date, *, model=DailyDigestIssue) -> dict:
    issue = model
    row = session.exec(select(issue.status, issue.requested, issue.generated_at, issue.error, issue.stats_json)
                       .where(issue.day == day.isoformat())).one_or_none()
    if row is None:
        return issue_metadata(day, None, False, '', '', {})
    stored_status, requested, generated_at, error, encoded = row
    return issue_metadata(day, stored_status, requested, generated_at, error, json.loads(encoded))


def worker_enabled() -> bool:
    return os.getenv('NEWS2DOUYIN_DIGEST_ENABLED', '1').strip().lower() not in {'0', 'false', 'off', 'no'}


def request_issue(engine, day: date, *, model=DailyDigestIssue) -> dict:
    """Queue a new day, refresh today's data, or return a published historical issue."""
    return _request(engine, day, rebuild=False, model=model)


def request_rebuild(engine, day: date, *, require_worker=True, model=DailyDigestIssue) -> dict:
    """Queue an explicit replacement after additional data became available."""
    return _request(engine, day, rebuild=True, require_worker=require_worker, model=model)


def _request(engine, day, *, rebuild, require_worker=True, model=DailyDigestIssue):
    today = default_day()
    if day > today:
        raise ValueError('不能生成未来日期的日报')
    if require_worker and not worker_enabled():
        with Session(engine) as session:
            state = read_issue(session, day, model=model)
        if day < today and not rebuild and state['status'] == 'ready':
            return state
        raise RuntimeError('日报生成已暂停，请启用 NEWS2DOUYIN_DIGEST_ENABLED')
    issue = model
    insert = pg_insert if engine.dialect.name == 'postgresql' else sqlite_insert
    # A user clicking an in-flight refresh joins that run. An explicit repair
    # may have supplied data after its cutoff, so keep one follow-up request.
    status = issue.status
    enqueue = ['failed']
    if rebuild or day == today:
        enqueue += ['ready', 'live', 'refresh_failed']
    if rebuild:
        enqueue += ['building', 'refreshing']
    next_status = case((status == 'building', 'building_again'), (status == 'refreshing', 'refreshing_again'),
                       (status.in_(PUBLISHED), 'refresh_pending'), else_='pending')
    with engine.begin() as conn:
        statement = insert(issue).values(day=day.isoformat(), requested=True)
        conn.execute(statement.on_conflict_do_update(index_elements=['day'],
                     set_={'status': next_status, 'requested': True, 'next_retry': 0, 'error': ''},
                     where=or_(status.in_(enqueue), status.in_(QUEUED) & issue.requested.is_(False))))
    with Session(engine) as session:
        return read_issue(session, day, model=model)


def prior_keys(session: Session, day: date, *, model=DailyDigestIssue) -> set[str]:
    from .ranking import card_progress_key

    first = days_before(day, HISTORY_DAYS).isoformat()
    rows = session.exec(select(model.keys_json, model.items_json).where(
        model.day >= first, model.day < day.isoformat(), model.status.in_(PUBLISHED)))
    keys = set()
    for saved_keys, items in rows:
        keys.update(json.loads(saved_keys))
        # Published editions stay immutable across upgrades. Compute the new
        # lookup alias from each stored headline, including variants that are
        # absent from today's archive. This reads at most seven daily editions.
        keys.update(card_progress_key(card) for card in json.loads(items))
    return keys


def _claim(engine, day: date, token: str, today: date, only_due: bool, now: float, cutoff: str, *, model=DailyDigestIssue):
    """Atomically acquire the job and return whether it had a snapshot and its version."""
    key = day.isoformat()
    issue = model
    insert = pg_insert if engine.dialect.name == 'postgresql' else sqlite_insert
    retry_due = issue.next_retry <= now
    eligible = or_(issue.status.in_(QUEUED),
                   issue.status.in_(FAILED) & (retry_due if only_due else True),
                   issue.status.in_(RUNNING) & (issue.lease_until <= now),
                   (issue.status == 'live') & (retry_due if day == today and only_due else True),
                   # 0.34 retried a just-published empty issue with late indexing.
                   (issue.status == 'ready') & (issue.next_retry > 0) & retry_due
                   & (issue.day == days_before(today, 1).isoformat()))
    if not only_due and day == today:
        eligible = or_(eligible, issue.status == 'ready')
    with engine.begin() as conn:
        conn.execute(insert(issue).values(day=key).on_conflict_do_nothing(index_elements=['day']))
        status = case((issue.status.in_(PUBLISHED), 'refreshing'), else_='building')
        result = conn.execute(update(issue).where(issue.day == key, eligible).values(
            status=status, lease_token=token, lease_until=now + LEASE_SECONDS, error='', next_retry=0))
        if result.rowcount != 1:
            return None
        state, encoded = conn.execute(select(issue.status, issue.stats_json).where(issue.day == key)).one()
        published = state == 'refreshing'
        publication = json.loads(encoded).get('publication') or {}
        if published and publication.get('cutoff_at', '') > cutoff:
            # A delayed caller may have captured its cutoff before another edition
            # was published. Leave both that edition and any queued request intact.
            conn.rollback()
            return None
        return published, publication.get('revision', int(published))


def _change_owned(engine, day: date, token: str, *, model=DailyDigestIssue, **changes) -> bool:
    with engine.begin() as conn:
        result = conn.execute(update(model).where(
            model.day == day.isoformat(), model.lease_token == token).values(**changes))
        return result.rowcount == 1


class _Interrupted(Exception):
    pass


def _json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def build_issue(engine, day: date, *, storage_root=None, stop_event=None, only_due=False, now=None,
                model=DailyDigestIssue, load_plan=None, decorate=None) -> dict:
    """Claim, build outside write transactions, then atomically publish the configured shortlist.

    A second caller observes the durable state of the in-flight/completed edition.
    Runtime errors are recorded with a safe exception type and retried by the worker.
    """
    from .focus_hints import annotate_focus, read_focus_hints
    from .selection import load_selection
    from ..candidates.service import iter_day

    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError('生成时间需要包含时区')
    today = default_day(instant)
    if day > today:
        raise ValueError('不能生成未来日期的日报')
    token = uuid4().hex
    clock = time.time if now is None else now.timestamp
    _, closed_at = day_bounds_utc(day)
    intraday = day == today
    # Source timestamps are recorded to seconds; include the current second.
    observed_at = min(instant.astimezone(timezone.utc).replace(microsecond=0), closed_at)
    cutoff = observed_at.isoformat(timespec='seconds').replace('+00:00', 'Z')
    owned = _claim(engine, day, token, today, only_due, clock(), cutoff, model=model)
    if owned is None:
        with Session(engine) as session:
            return read_issue(session, day, model=model)
    published, revision = owned
    ceiling = min(observed_at + timedelta(seconds=1), closed_at) if intraday else closed_at
    renew_at = time.monotonic() + LEASE_SECONDS / 4

    def checkpoint():
        nonlocal renew_at
        if stop_event is not None and stop_event.is_set():
            raise _Interrupted
        if time.monotonic() >= renew_at:
            if not _change_owned(engine, day, token, model=model, lease_until=clock() + LEASE_SECONDS):
                raise _Interrupted
            renew_at = time.monotonic() + LEASE_SECONDS / 4

    try:
        checkpoint()
        plan = (load_plan or load_selection)()
        source_stats = {}
        with Session(engine) as session:
            history = prior_keys(session, day, model=model)
            matcher = read_focus_hints(session, day, cutoff=ceiling)
            archived = {}
            if storage_root is not None:
                from .backfill import restored_gkg
                job_id, stamps = restored_gkg(session, day)
                if job_id:
                    archived['restored_stamps'] = stamps
                    source_stats['backfill_id'] = job_id
        with closing(iter_day(engine, storage_root, day, stats=source_stats, cutoff=ceiling,
                              checkpoint=checkpoint, coarse=plan.needs_coarse, **archived)) as stream:
            annotated = annotate_focus(stream, matcher, stats=source_stats)
            result = plan.select(annotated, day=day, cutoff=ceiling, prior_keys=history)
        checkpoint()
        items = result['items']
        from .geography import enrich_selected
        enrich_selected(items, storage_root=storage_root)
        from .why_follow import attach_focus
        from .actor_context import attach_actor_context
        with Session(engine) as session:
            attach_focus(items, session, matcher, ceiling)
            attach_actor_context(items, session, day, ceiling)
            if decorate is not None:
                decorate(items, session, day, ceiling)
        keys = [item['key'] for item in items]
        if len(items) > plan.limit or len(set(keys)) != len(keys):
            raise ValueError('Invalid daily edition')
        checkpoint()
        publication = dict(revision=revision + 1, kind='intraday' if intraday else 'closed',
                           cutoff_at=cutoff)
        stats = dict(result['stats'], sources=source_stats, publication=publication)
        ready = 'live' if intraday else 'ready'
        followup = model.status.in_(('building_again', 'refreshing_again'))
        next_retry = 0
        if intraday:
            next_retry = clock() + LIVE_REFRESH_SECONDS
        elif not items and day == days_before(today, 1):
            next_retry = clock() + EMPTY_RETRY_SECONDS
        _change_owned(engine, day, token, model=model, status=case((followup, 'refresh_pending'), else_=ready),
                      items_json=_json(items), keys_json=_json(keys), stats_json=_json(stats),
                      generated_at=utc_now_iso(), lease_token='', lease_until=0, next_retry=next_retry)
    except _Interrupted:
        _change_owned(engine, day, token, model=model,
                      status='refresh_pending' if published else 'pending', lease_token='', lease_until=0)
    except Exception as exc:
        log.exception('Failed to generate %s for %s', model.__name__, day)
        _change_owned(engine, day, token, model=model,
                      status='refresh_failed' if published else 'failed', lease_token='', lease_until=0,
                      next_retry=clock() + FAILURE_RETRY_SECONDS, error=type(exc).__name__)
    with Session(engine) as session:
        return read_issue(session, day, model=model)
