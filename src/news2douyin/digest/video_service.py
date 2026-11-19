"""A separate, versioned Top 100 for short-video editorial selection."""
from datetime import date
import json
import os

from sqlmodel import Session, select

from . import service
from .calendar import days_before
from .models import VideoDigestIssue
from .runtime import DigestWorker, due_day
from .service import worker_enabled  # shared master switch for both edition workers

STRATEGY = 'video_story'
LIMIT = 100


def read_issue(session: Session, day: date) -> dict:
    return service.read_issue(session, day, model=VideoDigestIssue)


def read_issue_state(session: Session, day: date) -> dict:
    return service.read_issue_state(session, day, model=VideoDigestIssue)


def request_issue(engine, day: date) -> dict:
    return service.request_issue(engine, day, model=VideoDigestIssue)


def request_rebuild(engine, day: date, *, require_worker=True) -> dict:
    return service.request_rebuild(engine, day, require_worker=require_worker, model=VideoDigestIssue)


def _plan():
    from .selection import create_selection

    return create_selection(STRATEGY, limit=LIMIT)


def _attach_history(items, session, day, ceiling):
    if not items:
        return
    from .video_story import HISTORY_DAYS, attach_history

    issue = VideoDigestIssue
    first = days_before(day, HISTORY_DAYS).isoformat()
    # The stored source observations and edition cutoff define eligibility in
    # a retrospective rebuild. The wall clock when that edition ran does not.
    rows = session.exec(select(issue.day, issue.items_json, issue.stats_json).where(
        issue.day >= first, issue.day < day.isoformat(), issue.status.in_(service.PUBLISHED))
        .order_by(issue.day.desc()))
    editions = (dict(day=stamp, items=json.loads(items_json),
                     cutoff_at=(json.loads(stats_json).get('publication') or {}).get('cutoff_at', ''))
                for stamp, items_json, stats_json in rows)
    attach_history(items, editions, day=day, cutoff=ceiling)


def build_issue(engine, day: date, *, storage_root=None, stop_event=None, only_due=False, now=None) -> dict:
    return service.build_issue(engine, day, storage_root=storage_root, stop_event=stop_event, only_due=only_due,
                               now=now, model=VideoDigestIssue, load_plan=_plan, decorate=_attach_history)


class VideoDigestWorker(DigestWorker):
    thread_name = 'video-candidates'

    def _due(self, session, now):
        automatic = os.getenv('NEWS2DOUYIN_VIDEO_DIGEST_AUTOMATIC', '0').strip().lower() in {'1', 'true', 'on', 'yes'}
        return due_day(session, now, model=VideoDigestIssue, automatic=automatic)

    def _build(self, day):
        return build_issue(self.engine, day, storage_root=self.storage_root, stop_event=self.stop_event, only_due=True)
