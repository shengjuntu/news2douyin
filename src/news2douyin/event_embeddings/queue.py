"""Transactional enqueue and bounded historical discovery."""
import os
from time import time
from uuid import uuid4

from sqlalchemy import case, delete, func, or_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from ..search.dates import date_range
from ..storage.articles import lock_news
from ..storage.models import Event, EventState
from .models import EventEmbeddingBackfill, EventEmbeddingJob, EventEmbeddingRecord

BACKFILL_PAGE = 128
BACKFILL_LOCK = 'event-embedding-backfill'
ACTIVE = ('pending', 'running')


def enabled():
    return os.getenv('NEWS2DOUYIN_EVENT_EMBEDDING_ENABLED', '1').strip().lower() not in {
        '0', 'off', 'false', 'no',
    }


def enqueue_enabled():
    return enabled() and bool(os.getenv('EMBEDDING_BASE_URL', '').strip())


def _insert(session, model):
    factory = pg_insert if session.get_bind().dialect.name == 'postgresql' else sqlite_insert
    return factory(model)


def enqueue_event(session, key):
    pending = dict(state='pending', lease='', available_at=time(), attempts=0, error='')
    if not os.getenv('EMBEDDING_BASE_URL', '').strip():
        # Existing vectors must be invalidated even if the endpoint was removed.
        session.exec(update(EventEmbeddingJob).where(EventEmbeddingJob.event_key == key).values(**pending))
        return
    session.execute(_insert(session, EventEmbeddingJob).values(event_key=key, **pending)
                    .on_conflict_do_update(index_elements=['event_key'], set_=pending))


def discard_event(session, key):
    # Merging expires any in-flight lease and removes the now-obsolete vector,
    # including when automatic embedding has been paused.
    session.exec(update(EventEmbeddingJob).where(EventEmbeddingJob.event_key == key)
                 .values(state='skipped', lease='', available_at=0, error=''))
    session.exec(delete(EventEmbeddingRecord).where(EventEmbeddingRecord.event_key == key))


def bounds(start_date, end_date):
    lower, upper = date_range('custom', start_date.isoformat(), end_date.isoformat())
    # As in the interop feed, compare both UTC suffix spellings at the boundary.
    return lower[:19], upper[:19]


def counts(session, batch_id=None):
    result = {state: 0 for state in (*ACTIVE, 'ready', 'failed', 'skipped')}
    statement = select(EventEmbeddingJob.state, func.count()).group_by(EventEmbeddingJob.state)
    if batch_id is not None:
        statement = statement.where(EventEmbeddingJob.batch_id == batch_id)
    for state, number in session.exec(statement):
        result[state] = number
    return result


def describe_backfill(session, batch):
    if batch is None:
        return None
    return dict(start_date=batch.start_date, end_date=batch.end_date, state=batch.state,
                scanned=batch.scanned, counts=counts(session, batch.batch_id))


def request_backfill(session, start_date, end_date):
    lower, upper = bounds(start_date, end_date)
    start, end = start_date.isoformat(), end_date.isoformat()
    lock_news(session, BACKFILL_LOCK)
    existing = session.get(EventEmbeddingBackfill, 1)
    if existing:
        info = describe_backfill(session, existing)
        active = existing.state == 'scanning' or any(info['counts'][state] for state in ACTIVE)
        if active:
            if (existing.start_date, existing.end_date) != (start, end):
                raise ValueError('已有补算正在运行，请等当前日期范围处理完毕')
            session.commit()
            return info
    ceiling = session.exec(select(func.max(Event.id))).one() or 0
    row = EventEmbeddingBackfill(batch_id=uuid4().hex, start_date=start, end_date=end,
                                 lower=lower, upper=upper, ceiling=ceiling)
    session.merge(row)
    session.commit()
    return dict(start_date=start, end_date=end, state='scanning', scanned=0,
                counts={state: 0 for state in (*ACTIVE, 'ready', 'failed', 'skipped')})


def scan_step(engine, current_profile=None):
    with Session(engine) as session:
        pending = session.get(EventEmbeddingBackfill, 1)
        if not pending or pending.state != 'scanning':
            return False
        lock_news(session, BACKFILL_LOCK)
        batch = session.get(EventEmbeddingBackfill, 1)
        if not batch or batch.state != 'scanning':
            return False
        rows = session.exec(
            select(Event.id, Event.event_key, EventEmbeddingRecord.profile, EventEmbeddingJob.state)
            .outerjoin(EventState, EventState.event_key == Event.event_key)
            .outerjoin(EventEmbeddingJob, EventEmbeddingJob.event_key == Event.event_key)
            .outerjoin(EventEmbeddingRecord, EventEmbeddingRecord.event_key == Event.event_key)
            .where(Event.id > batch.cursor, Event.id <= batch.ceiling,
                   Event.first_seen_at >= batch.lower, Event.first_seen_at < batch.upper,
                   or_(EventState.merged_into.is_(None), EventState.merged_into == ''))
            .order_by(Event.id).limit(BACKFILL_PAGE)
        ).all()
        if rows:
            # Consistent lock order for overlapping batches and event writes.
            ready = current_profile is not None
            data = [
                dict(event_key=key, state='ready' if ready and stored == current_profile and state == 'ready' else 'pending',
                     batch_id=batch.batch_id, available_at=time(), lease='', attempts=0, error='')
                for _, key, stored, state in sorted(rows, key=lambda row: row[1])
            ]
            statement = _insert(session, EventEmbeddingJob).values(data)
            job = EventEmbeddingJob.__table__.c
            keep = job.state.in_(ACTIVE)
            keep_ready = (job.state == 'ready') & (statement.excluded.state == 'ready')
            session.execute(statement.on_conflict_do_update(index_elements=['event_key'], set_=dict(
                batch_id=statement.excluded.batch_id,
                state=case((keep | keep_ready, job.state), else_='pending'),
                available_at=case((keep | keep_ready, job.available_at), else_=statement.excluded.available_at),
                lease=case((keep, job.lease), else_=''),
                attempts=case((keep, job.attempts), else_=0),
                error=case((keep, job.error), else_=''),
            )))
            batch.cursor = rows[-1][0]
            batch.scanned += len(rows)
        if len(rows) < BACKFILL_PAGE:
            batch.state = 'queued'
        session.add(batch)
        session.commit()
    return True


def save_record(session, key, profile, model, content_hash, value, updated_at):
    values = dict(profile=profile, model=model, content_hash=content_hash,
                  embedding=value, updated_at=updated_at)
    session.execute(_insert(session, EventEmbeddingRecord).values(event_key=key, **values)
                    .on_conflict_do_update(index_elements=['event_key'], set_=values))
