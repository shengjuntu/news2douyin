"""Persistent investigation tree and bounded branch expansion."""
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from time import time
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, update
from sqlmodel import Session, select

from ..digest.calendar import BEIJING
from ..desk.focus_lifecycle import require_active
from ..desk.models import WatchEvent
from ..desk.service import current_membership
from ..intake.models import DailyRepairJob
from ..research import service as research_service
from ..research.models import ResearchRun
from ..search.dates import timezone_name
from ..storage.articles import lock_news
from ..storage.models import Event, EventMoment, EventState, utc_now_iso
from ..storage.utils import dumps, loads
from . import rounds
from .chronology import bind_citation, eligible, excerpt_time, inferred, interval, sqlite_utc_bound
from .models import (CausalAgentLink, CausalAgentNote, CausalArchiveLink, CausalFocus,
                     CausalInvestigation, CausalJob, CausalNode, CausalProgram,
                     CausalProgramRetrieval, CausalProgramStep, CausalRetiredVisit)

ACTIVE = ('pending', 'running')
BEAM_WIDTH = 3
LOOKBACK = timedelta(days=90)
LEASE_SECONDS = 480
MAX_DEPTH = 10
MAX_TREE_NODES = 160
PROGRAM_MAX_STEPS = 18
RESULTS = 8


class CausalConflict(ValueError):
    """The requested expansion cannot enter the queue."""


class CausalLeaseLost(RuntimeError):
    """A stale worker must stop without publishing results."""


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


def _program_date(root_at, date_from, now):
    point = interval(root_at)
    if point is None:
        raise ValueError('逐层查找需要根事件的发生日期，请填写事件时间或选择已有核对日期的事件')
    if point[0] > now:
        raise ValueError('根事件发生日期不能晚于当前时间')
    if not date_from:
        local_day = point[0].astimezone(ZoneInfo(timezone_name())).date()
        date_from = (local_day - timedelta(days=min(LOOKBACK.days, (local_day - date.min).days))).isoformat()
    beginning = interval(date_from)
    if len(date_from) != 10 or not beginning:
        raise ValueError('最早查找日期须为 YYYY-MM-DD')
    if beginning[0] >= point[0]:
        raise ValueError('最早查找日期须早于根事件发生时间')
    return date_from


def create(session, text, event_at, event_key, automatic, *, focus_key='', queue_local=True,
           program=False, date_from=''):
    if focus_key:
        lock_news(session, 'watch:' + focus_key)
        require_active(session, focus_key)
        if event_key and not session.exec(
            select(WatchEvent.event).where(WatchEvent.watch == focus_key,
                                          WatchEvent.event == event_key,
                                          WatchEvent.choice != 'exclude', current_membership())
        ).first():
            raise ValueError('该事件不在当前焦点，请从图谱重新选择')
    event, reviewed_at = _event(session, event_key)
    now = datetime.now(timezone.utc)
    if event_at and interval(event_at) is None:
        raise ValueError('请输入有效日期或 ISO 时间')
    known = event_at or reviewed_at or inferred(text, now)
    if program:
        date_from = _program_date(known, date_from, now)
    case = CausalInvestigation(key='ci_' + uuid4().hex, text=text, event_at=known, event_key=event_key)
    root = CausalNode(key='cn_' + uuid4().hex, case_key=case.key, text=text, event_key=event_key,
                      ref=event.event_key if event else '', occurred_at=known, time_status='root')
    session.add(case)
    if focus_key:
        session.add(CausalFocus(case_key=case.key, focus_key=focus_key))
    if program:
        session.add(CausalProgram(case_key=case.key, date_from=date_from))
    session.add(root)
    if queue_local:
        session.add(_new_job(case, root, automatic))
        session.commit()
    else:
        session.flush()
    return case.key


def _require(session, case_key):
    case = session.get(CausalInvestigation, case_key)
    if case is None:
        raise KeyError('调查不存在')
    return case


def _count(session, case_key):
    return session.exec(select(func.count()).select_from(CausalNode)
                        .where(CausalNode.case_key == case_key)).one()


def program_citation(sources, reason):
    """Resolve a saved edge's cited source, including pre-0.56 evidence."""
    marked = [source for source in sources if source.get('cited_quote')]
    if marked:
        if len(marked) == 1 and marked[0]['cited_quote'] in marked[0].get('quote', ''):
            return marked[0]
        return None
    # Older explanations include their verbatim anchor after a standard suffix.
    _, separator, suffix = reason.rpartition('\n来源摘录：“')
    if separator and suffix.endswith('”') and len(suffix) > 1:
        quote = suffix[:-1]
        matches = [source for source in sources if quote in source.get('quote', '')]
        if len(matches) == 1:
            return {**matches[0], 'cited_quote': quote}
        return None
    return sources[0] if len(sources) == 1 else None


def _program_child(node):
    stamp = interval(node.published_at)
    if node.time_status != 'earlier' or stamp is None:
        return False
    cited = program_citation(loads(node.sources_json, []), node.reason)
    return (cited is not None and bool(cited.get('quote')) and cited.get('kind') != 'local_headline'
            and interval(excerpt_time(cited)) == stamp)


def program_leaf_reason(node, *, steps, node_count):
    if node['time_status'] != 'earlier':
        return '原文或事件的前置时间待核，本分支暂不展开'
    if node['depth'] >= MAX_DEPTH or node_count >= MAX_TREE_NODES or steps >= PROGRAM_MAX_STEPS:
        return '已到达本次调查的搜索上限'
    if not node['expandable']:
        return '缺少带日期的原文依据，本分支暂不展开'
    return '已保留线索，本层优先展开其他候选'


def expand(session, case_key, node_key, automatic, *, commit=True, continue_history=False, repair_serial=None):
    lock_news(session, 'causal:' + case_key)
    case = _require(session, case_key)
    if session.get(CausalAgentLink, case_key):
        raise CausalConflict('这项调查由 RunDesk 执行，请使用“交给 RunDesk”继续')
    node = session.get(CausalNode, node_key)
    if node is None or node.case_key != case_key:
        raise KeyError('调查中没有这条线索')
    if commit:
        from ..history_archive.models import HistoryJob
        if session.exec(select(HistoryJob.key).where(HistoryJob.case_key == case_key,
                        HistoryJob.state.in_(('planning', 'running'))).limit(1)).first():
            raise CausalConflict('节点补档尚未完成，完成后将自动重查')
    existing = session.get(CausalJob, node_key)
    if existing and existing.state in ACTIVE:
        return
    if session.exec(select(CausalJob.node_key).where(CausalJob.case_key == case_key,
                                                    CausalJob.state.in_(ACTIVE)).limit(1)).first():
        raise CausalConflict('当前调查还在运行，完成后可继续展开')
    program = session.get(CausalProgram, case_key)
    if node.depth >= MAX_DEPTH or (_count(session, case_key) >= MAX_TREE_NODES and not (program and existing)):
        raise CausalConflict('这次调查已达到线索上限，可以从该线索新开一次调查')
    stored = session.get(CausalProgramRetrieval, node_key) if program else None
    report = loads(stored.report_json, {}) if stored else {}
    if continue_history and not report.get('history', {}).get('scan', {}).get('can_continue'):
        raise CausalConflict('该节点没有可继续的归档窗口')
    if program:
        if node.depth and not _program_child(node):
            raise CausalConflict('这条线索缺少可核对的时间或原文依据')
        if existing is None and _visits_used(session, case_key) >= PROGRAM_MAX_STEPS:
            raise CausalConflict('本次调查已达到搜索节点上限')
        if existing:
            rounds.snapshot(session, case_key, node_key, view(session, case_key)['nodes'])
        if stored:
            report.update(continue_history=continue_history, repair_job_serial=repair_serial)
            stored.report_json = dumps(report)
            session.add(stored)
    session.merge(_new_job(case, node, automatic))
    case.updated_at = utc_now_iso()
    if commit:
        session.commit()


def recent(session, focus_key=''):
    statement = select(CausalInvestigation)
    if focus_key:
        require_active(session, focus_key)
        statement = statement.join(CausalFocus, CausalFocus.case_key == CausalInvestigation.key).where(
            CausalFocus.focus_key == focus_key
        )
    rows = session.exec(statement.order_by(CausalInvestigation.updated_at.desc(),
                                           CausalInvestigation.key.desc()).limit(20)).all()
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


def view(session, case_key, *, focus_key=''):
    from ..history_archive.service import requests_for_case

    case = _require(session, case_key)
    focus = session.get(CausalFocus, case_key)
    bound_focus = focus.focus_key if focus else ''
    if focus_key and focus_key != bound_focus:
        raise KeyError('调查不在该焦点')
    program_scope = session.get(CausalProgram, case_key)
    archive_requests = requests_for_case(session, case_key) if program_scope else {}
    rows = session.exec(select(CausalNode, CausalJob, CausalAgentNote, CausalProgramStep, CausalProgramRetrieval)
                        .outerjoin(CausalJob, CausalJob.node_key == CausalNode.key)
                        .outerjoin(CausalAgentNote, CausalAgentNote.node_key == CausalNode.key)
                        .outerjoin(CausalProgramStep, CausalProgramStep.node_key == CausalNode.key)
                        .outerjoin(CausalProgramRetrieval, CausalProgramRetrieval.node_key == CausalNode.key)
                        .where(CausalNode.case_key == case_key)
                        .order_by(CausalNode.depth, CausalNode.score.desc(), CausalNode.key)).all()
    revisions = rounds.latest_revisions(session, case_key) if program_scope else {}
    visits = _visits_used(session, case_key) if program_scope else 0
    can_start = len(rows) < MAX_TREE_NODES and visits < PROGRAM_MAX_STEPS
    nodes = []
    for node, job, note, step, report in rows:
        item = node.model_dump(exclude={'case_key', 'ref', 'sources_json'})
        relation = note.relation if note and node.parent_key else ''
        if program_scope:
            grounded = not node.depth or _program_child(node)
            if not grounded and item['time_status'] == 'earlier':
                item['time_status'] = 'unknown'
                if relation == 'possible_cause':
                    relation = 'context'
            item.update(queries=loads(step.queries_json, []) if step else [],
                        candidate_count=step.candidate_count if step else None,
                        retrieval_mode=step.retrieval_mode if step else '',
                        retrieval=loads(report.report_json, {}) if report else {},
                        archive_request=archive_requests.get(node.key), previous_revision=revisions.get(node.key),
                        expandable=bool((job or can_start) and grounded
                                        and node.depth < MAX_DEPTH))
        nodes.append(dict(**item, sources=loads(node.sources_json, []), job_state=job.state if job else '',
                          job_error=job.error if job else '', stop_reason=(step.stop_reason if step else '')
                          or (note.stop_reason if note else ''),
                          relation=relation))
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
    link = session.get(CausalAgentLink, case_key)
    agent = None
    if link:
        run = session.get(ResearchRun, link.current_run_id) if link.current_run_id else None
        agent = dict(status='failed' if link.last_error else run.status if run else 'draft',
                     error=link.last_error or (run.error if run else ''),
                     run_id=run.run_id if run else '', date_from=link.date_from,
                     research_path='/research/' + quote(link.research_case_id, safe=''))
    program = None
    if program_scope:
        states = [item['job_state'] for item in nodes]
        queued = sum(status in ACTIVE for status in states)
        completed, failed = states.count('ready'), states.count('failed')
        status = ('running' if completed or failed or 'running' in states else 'pending') if queued else (
            'partial' if failed else 'completed')
        if not queued and any(request['state'] in ('queued', 'waiting') for request in archive_requests.values()):
            status = 'awaiting_archive'
        program = dict(date_from=program_scope.date_from, status=status, queued=queued, completed=completed, failed=failed)
        if not queued:
            for item in nodes:
                if item['depth'] and not item['job_state'] and not item['stop_reason']:
                    item['stop_reason'] = program_leaf_reason(item, steps=visits, node_count=len(nodes))
    return dict(**case.model_dump(), nodes=nodes, focus_key=bound_focus, agent=agent, program=program,
                working=any(item['job_state'] in ACTIVE for item in nodes)
                or bool(program and program['status'] == 'awaiting_archive')
                or bool(agent and agent['status'] in research_service.ACTIVE),
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
    program: bool = False


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
        result = Claim(row.case_key, row.node_key, row.lease, row.automatic,
                       session.get(CausalProgram, row.case_key) is not None)
        session.commit()
        return result


def renew(engine, claimed, *, hypotheses=None):
    now = time()
    with Session(engine) as session:
        refreshed = session.execute(update(CausalJob).where(
            CausalJob.case_key == claimed.case_key, CausalJob.node_key == claimed.node_key,
            CausalJob.state == 'running', CausalJob.lease == claimed.lease, CausalJob.due_at > now,
        ).values(due_at=now + LEASE_SECONDS)).rowcount
        if not refreshed:
            raise CausalLeaseLost()
        if hypotheses is not None:
            session.merge(CausalProgramStep(node_key=claimed.node_key, case_key=claimed.case_key,
                                           queries_json=dumps(hypotheses)))
            saved = session.get(CausalProgramRetrieval, claimed.node_key)
            progress = {'phase': 'retrieving'}
            if saved:
                previous = loads(saved.report_json, {})
                history = previous.get('history', {})
                if history.get('scan'):
                    progress['history'] = history
                for field in ('continue_history', 'repair_job_serial'):
                    if field in previous:
                        progress[field] = previous[field]
            session.merge(CausalProgramRetrieval(node_key=claimed.node_key, report_json=dumps(progress)))
        session.commit()


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
    from .model import configure, propose_hypotheses, rank
    from .retrieval import retrieve

    target, anchor, as_of, exclude = _snapshot(engine, claimed)
    settings = configure()
    upper = sqlite_utc_bound(as_of)
    lower = sqlite_utc_bound(as_of - LOOKBACK) if as_of.year > 1 else '0001-01-01T00:00:00'
    queries = propose_hypotheses(settings, target, lower=lower, upper=upper)
    if not queries:
        return []
    candidates = [
        allowed for item in retrieve(engine, queries, lower=lower, upper=upper, exclude=exclude)
        if (allowed := eligible(item, anchor, as_of))
    ]
    if not candidates:
        return []
    by_ref = {item['ref']: item for item in candidates}
    decisions = rank(settings, target, candidates, hypotheses=queries,
                     before_batch=lambda: renew(engine, claimed))
    results = [bind_citation(by_ref[decision['ref']], decision, anchor, as_of)
               for decision in decisions if decision['ref'] in by_ref]
    return sorted(results, key=lambda item: (item['time_status'] != 'earlier', -item['score'], item['ref']))[:RESULTS]


def _visits_used(session, case_key):
    live = select(func.count()).select_from(CausalJob).where(CausalJob.case_key == case_key)
    retired = select(func.count()).select_from(CausalRetiredVisit).where(CausalRetiredVisit.case_key == case_key)
    return session.exec(live).one() + session.exec(retired).one()


def _schedule_program(session, case, depth):
    jobs = select(CausalJob.node_key).where(CausalJob.case_key == case.key)
    if session.exec(jobs.where(CausalJob.state.in_(ACTIVE)).limit(1)).first():
        return
    remaining = PROGRAM_MAX_STEPS - _visits_used(session, case.key)
    if remaining <= 0 or depth + 1 >= MAX_DEPTH or _count(session, case.key) >= MAX_TREE_NODES:
        return
    live = session.exec(select(CausalNode.ref, CausalNode.depth, CausalNode.beam_selected)
                        .where(CausalNode.key.in_(jobs))).all()
    retired = session.exec(select(CausalRetiredVisit.ref, CausalRetiredVisit.depth, CausalRetiredVisit.beam_selected)
                           .where(CausalRetiredVisit.case_key == case.key)).all()
    visited = live + retired
    slots = min(remaining, BEAM_WIDTH - sum(level == depth + 1 and automatic for _, level, automatic in visited))
    if slots <= 0:
        return
    scheduled = {ref for ref, _, _ in visited}
    candidates = session.exec(select(CausalNode, CausalAgentNote.relation)
        .outerjoin(CausalAgentNote, CausalAgentNote.node_key == CausalNode.key)
        .where(CausalNode.case_key == case.key, CausalNode.depth == depth + 1,
               CausalNode.time_status == 'earlier', CausalNode.key.not_in(jobs))
        .order_by(CausalNode.score.desc(), CausalNode.key)).all()
    eligible = [(child, relation) for child, relation in candidates
                if child.ref not in scheduled and _program_child(child)]
    # Reserve one slot for the highest-ranked supported cause. The other slots
    # still use evidence rank, including counter-evidence and context.
    cause = next((child for child, relation in eligible if relation == 'possible_cause'), None)
    ordered = ([cause] if cause else []) + [child for child, _ in eligible]
    for child in ordered:
        if child.ref in scheduled:
            continue
        child.beam_selected = True
        session.add(_new_job(case, child, False))
        scheduled.add(child.ref)
        slots -= 1
        if not slots:
            break


def complete(engine, claimed, results=(), error='', *, queries=(), candidate_count=0,
             retrieval_mode='', stop_reason='', retrieval_report=None):
    with Session(engine) as session:
        lock_news(session, 'causal:' + claimed.case_key)
        job = session.exec(select(CausalJob).where(CausalJob.node_key == claimed.node_key)
                           .with_for_update()).first()
        if job is None or job.lease != claimed.lease or job.state != 'running' or job.due_at <= time():
            return
        case = _require(session, claimed.case_key)
        node = session.get(CausalNode, claimed.node_key)
        existing = {item.ref: item for item in session.exec(
            select(CausalNode).where(CausalNode.case_key == case.key, CausalNode.parent_key == node.key)
        )}
        previous_branches = set(session.exec(
            select(CausalNode.ref).join(CausalJob, CausalJob.node_key == CausalNode.key)
            .where(CausalNode.case_key == case.key, CausalNode.ref != '', CausalNode.parent_key != node.key)
        )) if not claimed.program else set()
        if claimed.program and not error:
            results = list(results)[:RESULTS]
            selected = {item['ref']: item for item in results}
            removed = []
            for ref, child in list(existing.items()):
                replacement = selected.get(ref)
                # A changed anchor invalidates the time window of every descendant.
                if (replacement is None or child.occurred_at != replacement['occurred_at']
                        or child.published_at != replacement['published_at'] or child.time_status != replacement['time_status']):
                    removed.append(child.key)
                    del existing[ref]
            rounds.retire(session, case.key, removed)
        remaining = MAX_TREE_NODES - _count(session, case.key)
        stored = 0
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
            stored += 1
            child.text, child.event_key = item['text'], item['event_key']
            child.occurred_at, child.published_at = item['occurred_at'], item['published_at']
            child.time_status, child.reason, child.score = item['time_status'], item['reason'], item['score']
            child.sources_json = dumps(item['sources'])
            session.add(child)
            if claimed.program:
                note = session.get(CausalAgentNote, child.key) or CausalAgentNote(
                    node_key=child.key, case_key=case.key)
                note.relation = item.get('relation') or 'context'
                session.add(note)
            if (not claimed.program and claimed.automatic and child.time_status == 'earlier' and child.depth < MAX_DEPTH
                    and ref not in previous_branches and session.get(CausalJob, child.key) is None):
                next_nodes.append(child)
        for child in next_nodes[:BEAM_WIDTH]:
            child.beam_selected = True
            session.add(_new_job(case, child, False))
        job.lease, job.due_at = '', 0
        job.state, job.error = ('failed' if error else 'ready'), error
        case.updated_at = utc_now_iso()
        if claimed.program:
            session.merge(CausalProgramStep(node_key=node.key, case_key=case.key, queries_json=dumps(queries),
                                            candidate_count=candidate_count, retrieval_mode=retrieval_mode,
                                            stop_reason=stop_reason or error))
            report = dict(retrieval_report or {})
            if 'selected_count' in report or stored:
                report['selected_count'] = stored
            if error and existing:
                report['previous_results_visible'] = True
            session.merge(CausalProgramRetrieval(node_key=node.key, report_json=dumps(report)))
            session.flush()
            _schedule_program(session, case, node.depth)
        session.commit()
