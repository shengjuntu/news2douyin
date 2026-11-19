"""Refresh today, close unfinished days, and process explicit per-day requests."""
from datetime import date, datetime, timedelta
import logging
from threading import Event, Thread

from sqlalchemy import case, or_
from sqlmodel import Session, select

from .calendar import days_before
from .models import DailyDigestIssue
from .service import BEIJING, FAILED, HISTORY_DAYS, QUEUED, RUNNING, build_issue, default_day, last_closed_day, worker_enabled

log = logging.getLogger(__name__)


def due_day(session: Session, now: datetime, *, model=DailyDigestIssue, automatic=True) -> date | None:
    local = now.astimezone(BEIJING)
    today = default_day(local)
    tick = now.timestamp()
    issue = model
    eligible = or_(issue.status.in_(QUEUED),
                   issue.status.in_(FAILED) & (issue.next_retry <= tick),
                   issue.status.in_(RUNNING) & (issue.lease_until <= tick))
    # A click is a background job, including before the midnight ingestion grace.
    queued = session.exec(select(issue.day).where(issue.requested.is_(True),
                          issue.day <= today.isoformat(), eligible)
                          .order_by(case((issue.day == today.isoformat(), 0), else_=1), issue.day).limit(1)).first()
    if queued:
        return date.fromisoformat(queued)
    # Close any provisional editions, including after a multi-day outage.
    # Empty historical editions with no provisional snapshot stay immutable.
    close = session.exec(select(issue.day).where(issue.day < today.isoformat(), or_(
        issue.status.in_(('live', 'refresh_pending')),
        (issue.status == 'refresh_failed') & (issue.next_retry <= tick),
        issue.status.in_(('refreshing', 'refreshing_again')) & (issue.lease_until <= tick)
    )).order_by(issue.day).limit(1)).first()
    if close:
        return date.fromisoformat(close)
    if not automatic:
        return None

    live = session.exec(select(issue.status, issue.lease_until, issue.next_retry)
                        .where(issue.day == today.isoformat())).one_or_none()
    if live is None:
        return today
    state, lease, retry = live
    if (state in QUEUED or state in RUNNING and lease <= tick or
            state in (*FAILED, 'live') and retry <= tick):
        return today

    target = last_closed_day(local)
    if local.hour == 0 and local.minute < 30:
        target = days_before(target, 1)
    first_seen = session.exec(select(issue.day).where(
        issue.day <= target.isoformat(), issue.requested.is_(False)
    ).order_by(issue.day).limit(1)).first()
    first = max(date.fromisoformat(first_seen), days_before(target, HISTORY_DAYS - 1)) if first_seen else target
    rows = {key: (state, lease, retry) for key, state, lease, retry in session.exec(
        select(issue.day, issue.status, issue.lease_until, issue.next_retry)
        .where(issue.day >= first.isoformat(), issue.day <= target.isoformat()))}
    for elapsed in range((target - first).days + 1):
        day = first + timedelta(days=elapsed)
        state, lease, retry = rows.get(day.isoformat(), ('pending', 0, 0))
        if state == 'ready':
            if day == last_closed_day(local) and 0 < retry <= tick:
                return day
            continue
        if state in RUNNING and lease > tick:
            return None
        if state in FAILED and retry > tick:
            continue
        return day
    return None


class DigestWorker:
    thread_name = 'daily-radar'

    def __init__(self, engine, storage_root):
        self.engine = engine
        self.storage_root = storage_root
        self.stop_event = Event()
        self._wake_event = Event()
        self.thread = None

    def start(self):
        if not worker_enabled() or (self.thread and self.thread.is_alive()):
            return
        self.stop_event.clear()
        self._wake_event.clear()
        self.thread = Thread(target=self._loop, name=self.thread_name, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake()
        if self.thread:
            self.thread.join(timeout=5)

    def wake(self):
        self._wake_event.set()

    def _due(self, session, now):
        return due_day(session, now)

    def _build(self, day):
        return build_issue(self.engine, day, storage_root=self.storage_root, stop_event=self.stop_event, only_due=True)

    def step(self):
        with Session(self.engine) as session:
            day = self._due(session, datetime.now(BEIJING))
        if day is None:
            return False
        result = self._build(day)
        return result['build_status'] == 'idle'

    def _loop(self):
        wait_seconds = 2
        while not self.stop_event.is_set():
            self._wake_event.wait(wait_seconds)
            self._wake_event.clear()
            if self.stop_event.is_set():
                break
            try:
                wait_seconds = 1 if self.step() else 30
            except Exception:
                log.exception('%s worker deferred', self.thread_name)
                wait_seconds = 30
