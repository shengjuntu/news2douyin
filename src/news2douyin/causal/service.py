"""Persistent investigation tree and bounded branch expansion."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from time import time
from urllib.parse import quote
from uuid import uuid4

from sqlalchemy import func, or_
from sqlmodel import Session, select

from ..digest.calendar import BEIJING
from ..intake.models import DailyRepairJob
from ..storage.articles import lock_news
from ..storage.models import Event, EventMoment, EventState, utc_now_iso
from ..storage.utils import dumps, loads
from .chronology import eligible, inferred, interval, sqlite_utc_bound
from .models import CausalArchiveLink, CausalInvestigation, CausalJob, CausalNode

ACTIVE = ('pending', 'running')
BEAM_WIDTH = 3
LOOKBACK = timedelta(days=90)
LEASE_SECONDS = 480
MAX_DEPTH = 10
MAX_TREE_NODES = 160
RESULTS = 8


class CausalConflict(ValueError):
    """The requested expansion cannot enter the queue."""


def _event(session, key):
    if not key:
        return None, ''
    row = session.exec(select(Event).where(Event.event_key == key)).first()
    state = session.get(EventState, key)
    if not row or (state and state.merged_into):
        raise KeyError('未找到当前有效的整理事件')
    dates = set(session.exec(select(EventMoment.date_start)
                            .where(EventMoment.event_key == key, EventMoment.deleted.is_(False),
                                   EventMoment.reviewed.is_(True), EventMoment.time_kind == 'occurred',
                                   EventMoment.certainty == 'exact',
                                   or_(EventMoment.date_end == '', EventMoment.date_end == EventMoment.date_start))
                            .distinct().limit(2)).all())
    candidate = next(iter(dates)) if len(dates) == 1 else ''
    return row, candidate if interval(candidate) else ''


def seed(session, key):
    event, at = _event(session, key)
    if event is None:
        return {}
    return dict(text='\n'.join(part for part in (event.event_title, event.summary) if part)[:2_000],
                event_key=event.event_key, event_at=at)


def _new_job(case, node, automatic):
    return CausalJob(case_key=case.key, node_key=node.key,
                     due_at=time(), state='pending', automatic=automatic)


def create(session, text, event_at, event_key, automatic):
    event, reviewed_at = _event(session, event_key)
    now = datetime.now(timezone.utc)
    if event_at and interval(event_at) is None:
        raise ValueError('请输入有效日期或 ISO 时间')
    known = event_at or reviewed_at or inferred(text, now)
    case = CausalInvestigation(key='ci_' + uuid4().hex, text=text, event_at=known, event_key=event_key)
    root = CausalNode(key='cn_' + uuid4().hex, case_key=case.key, text=text, event_key=event_key,
                      ref=event.event_key if event else '', occurred_at=known, time_status='root')
    session.add(case)
    session.add(root)
    session.add(_new_job(case, root, automatic))
    session.commit()
    return case.key


def _require(session, case_key):
    case = session.get(CausalInvestigation, case_key)
    if case is None:
        raise KeyError('调查不存在')
    return case


def _count(session, case_key):
    return session.exec(select(func.count()).select_from(CausalNode)
                        .where(CausalNode.case_key == case_key)).one()


def expand(session, case_key, node_key, automatic):
    lock_news(session, 'causal:' + case_key)
    case = _require(session, case_key)
    node = session.get(CausalNode, node_key)
    if node is None or node.case_key != case_key:
        raise KeyError('调查中没有这条线索')
    existing = session.get(CausalJob, node_key)
    if existing and existing.state in ACTIVE:
        return
    if session.exec(select(CausalJob.node_key).where(CausalJob.case_key == case_key,
                                                    CausalJob.state.in_(ACTIVE)).limit(1)).first():
        raise CausalConflict('当前调查还在运行，完成后可继续展开')
    if node.depth >= MAX_DEPTH or _count(session, case_key) >= MAX_TREE_NODES:
        raise CausalConflict('这次调查已达到线索上限，可以从该线索新开一次调查')
    session.merge(_new_job(case, node, automatic))
    case.updated_at = utc_now_iso()
    session.commit()


def recent(session):
    rows = session.exec(select(CausalInvestigation)
                        .order_by(CausalInvestigation.updated_at.desc(), CausalInvestigation.key.desc())
                        .limit(20)).all()
    return [dict(key=row.key, text=row.text, created_at=row.created_at, updated_at=row.updated_at)
            for row in rows]


def request_archive(engine, case_key, day, retry):
    from ..digest.backfill import enqueue

    with Session(engine) as session:
        _require(session, case_key)
    enqueue(engine, day, retry=retry)
    with Session(engine) as session:
        lock_news(session, 'causal:' + case_key)
        case = _require(session, case_key)
        session.merge(CausalArchiveLink(case_key=case_key, day=day.isoformat()))
        case.updated_at = utc_now_iso()
        session.commit()
        return view(session, case_key)


def view(session, case_key):
    case = _require(session, case_key)
    rows = session.exec(select(CausalNode, CausalJob)
                        .outerjoin(CausalJob, CausalJob.node_key == CausalNode.key)
                        .where(CausalNode.case_key == case_key)
                        .order_by(CausalNode.depth, CausalNode.score.desc(), CausalNode.key)).all()
    nodes = []
    for node, job in rows:
        item = node.model_dump(exclude={'case_key', 'ref', 'sources_json'})
        nodes.append(dict(**item, sources=loads(node.sources_json, []), job_state=job.state if job else '',
                          job_error=job.error if job else ''))
    context = interval(case.event_at) or interval(case.created_at)
    day = min(context[0], datetime.now(timezone.utc)).astimezone(BEIJING).date()
    previous_day = day - timedelta(days=1) if day.year > 1 else day
    repairs = [dict(day=day, status=status, archive_path='/admin/daily-radar?day=' + day)
               for day, status in session.exec(
                   select(CausalArchiveLink.day, DailyRepairJob.status)
                   .join(DailyRepairJob, DailyRepairJob.day == CausalArchiveLink.day)
                   .where(CausalArchiveLink.case_key == case_key).order_by(CausalArchiveLink.day.desc()).limit(20)
               )]
    reference = case.event_key or next((item['event_key'] for item in nodes if item['event_key']), '')
    research = '/events/' + quote(reference, safe='') + '#research' if reference else '/research/settings'
    return dict(**case.model_dump(), nodes=nodes,
                working=any(item['job_state'] in ACTIVE for item in nodes),
                coverage=dict(message='可按日期核对 GDELT 归档。补采完成后，在原调查重新查该分支。',
                              archive_path='/admin/daily-radar?day=' + previous_day.isoformat(),
                              suggested_day=previous_day.isoformat(), repairs=repairs),
                research_path=research)


@dataclass(frozen=True)
class Claim:
    case_key: str
    node_key: str
    lease: str
    automatic: bool


def claim(engine):
    now = time()
    due = (CausalJob.state.in_(ACTIVE), CausalJob.due_at <= now)
    with Session(engine) as session:
        if session.exec(select(CausalJob.node_key).where(*due).limit(1)).first() is None:
            return None
        if engine.dialect.name == 'sqlite':
            lock_news(session)
        row = session.exec(select(CausalJob).where(*due).order_by(CausalJob.due_at, CausalJob.node_key)
                           .limit(1).with_for_update(skip_locked=True)).first()
        if row is None:
            return None
        row.lease = uuid4().hex
        row.due_at = now + LEASE_SECONDS
        row.state = 'running'
        result = Claim(row.case_key, row.node_key, row.lease, row.automatic)
        session.commit()
        return result


def _snapshot(engine, claimed):
    with Session(engine) as session:
        case = _require(session, claimed.case_key)
        node = session.get(CausalNode, claimed.node_key)
        ancestors, exclude = [], set()
        current = node
        while current:
            ancestors.append(current)
            if current.ref:
                exclude.add(current.ref)
            current = session.get(CausalNode, current.parent_key) if current.parent_key else None
        as_of = interval(case.created_at)[0]
        anchor = None
        for ancestor in ancestors:
            point = interval(ancestor.occurred_at) or interval(ancestor.published_at)
            if point is not None and anchor is None:
                anchor = point
            if point:
                as_of = min(as_of, point[1])
        return node.text, anchor, as_of, exclude


def investigate(engine, claimed):
    from .model import configure, propose, rank
    from .retrieval import retrieve

    target, anchor, as_of, exclude = _snapshot(engine, claimed)
    settings = configure()
    queries = propose(settings, target) or [target]
    upper = sqlite_utc_bound(as_of)
    lower = sqlite_utc_bound(as_of - LOOKBACK) if as_of.year > 1 else '0001-01-01T00:00:00'
    candidates = [
        allowed for item in retrieve(engine, queries, lower=lower, upper=upper, exclude=exclude)
        if (allowed := eligible(item, anchor, as_of))
    ]
    if not candidates:
        return []
    by_ref = {item['ref']: item for item in candidates}
    decisions = rank(settings, target, candidates)
    results = [{**by_ref[decision['ref']], **decision} for decision in decisions if decision['ref'] in by_ref]
    return sorted(results, key=lambda item: (item['time_status'] != 'earlier', -item['score'], item['ref']))[:RESULTS]


def complete(engine, claimed, results=(), error=''):
    with Session(engine) as session:
        lock_news(session, 'causal:' + claimed.case_key)
        job = session.exec(select(CausalJob).where(CausalJob.node_key == claimed.node_key)
                           .with_for_update()).first()
        if job is None or job.lease != claimed.lease:
            return
        case = _require(session, claimed.case_key)
        node = session.get(CausalNode, claimed.node_key)
        existing = {item.ref: item for item in session.exec(
            select(CausalNode).where(CausalNode.case_key == case.key, CausalNode.parent_key == node.key)
        )}
        previous_branches = set(session.exec(
            select(CausalNode.ref).join(CausalJob, CausalJob.node_key == CausalNode.key)
            .where(CausalNode.case_key == case.key, CausalNode.ref != '', CausalNode.parent_key != node.key)
        ))
        remaining = MAX_TREE_NODES - _count(session, case.key)
        next_nodes = []
        for item in results:
            ref = item['ref']
            child = existing.get(ref)
            if child is None:
                if remaining <= 0:
                    continue
                child = CausalNode(key='cn_' + uuid4().hex, case_key=case.key,
                                   parent_key=node.key, ref=ref, text=item['text'], depth=node.depth + 1)
                remaining -= 1
            child.text, child.event_key = item['text'], item['event_key']
            child.occurred_at, child.published_at = item['occurred_at'], item['published_at']
            child.time_status, child.reason, child.score = item['time_status'], item['reason'], item['score']
            child.sources_json = dumps(item['sources'])
            session.add(child)
            if (claimed.automatic and child.time_status == 'earlier' and child.depth < MAX_DEPTH
                    and ref not in previous_branches and session.get(CausalJob, child.key) is None):
                next_nodes.append(child)
        for child in next_nodes[:BEAM_WIDTH]:
            child.beam_selected = True
            session.add(_new_job(case, child, False))
        job.lease, job.due_at = '', 0
        job.state, job.error = ('failed' if error else 'ready'), error
        case.updated_at = utc_now_iso()
        session.commit()
