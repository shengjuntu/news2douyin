"""Hand a saved investigation to the same durable RunDesk research queue."""
from datetime import date, datetime, timedelta, timezone
import os
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from ..desk.focus_lifecycle import require_active
from ..research import service as research
from ..research.models import ResearchRun
from ..research.rundesk import RemoteError
from ..search.dates import timezone_name
from ..storage.articles import lock_news
from ..storage.models import utc_now_iso
from .chronology import interval
from .models import CausalAgentLink, CausalFocus, CausalJob, CausalProgram
from .service import ACTIVE, CausalConflict, _require, view


def check_config(runtime):
    settings = runtime.settings.get()
    if not all(settings.get(key) for key in ('instance_id', 'workspace_id')) and not os.getenv(
        'NEWS2DOUYIN_CAUSAL_RUN_PRESET', ''
    ).strip():
        raise CausalConflict('请先在研究助手设置中连接 RunDesk')


def _date_from(case, requested, existing):
    if not requested and existing:
        return existing
    zone = ZoneInfo(timezone_name())
    now = datetime.now(timezone.utc)
    target = interval(case.event_at)
    end = min(now, target[0] if target else now).astimezone(zone).date()
    if not requested:
        return (end - timedelta(days=min(90, (end - date.min).days))).isoformat()
    try:
        value = date.fromisoformat(requested)
    except (TypeError, ValueError):
        raise ValueError('最早日期须为 YYYY-MM-DD') from None
    if value.isoformat() != requested or interval(requested) is None:
        raise ValueError('最早日期须为 YYYY-MM-DD')
    if value > end:
        raise ValueError('最早日期不能晚于目标日期')
    return value.isoformat()


def bind(session, case_key, requested_from):
    """The investigation, its research case and scope are committed together."""
    research.locked(session)
    lock_news(session, 'causal:' + case_key)
    case = _require(session, case_key)
    focus = session.get(CausalFocus, case_key)
    if focus:
        require_active(session, focus.focus_key)
    if session.exec(select(CausalJob.node_key).where(CausalJob.case_key == case_key,
                                                      CausalJob.state.in_(ACTIVE)).limit(1)).first():
        raise CausalConflict('本地查找还在运行，完成后可交给 RunDesk')

    link = session.get(CausalAgentLink, case_key)
    program = session.get(CausalProgram, case_key)
    if program and requested_from and requested_from != program.date_from:
        raise CausalConflict('RunDesk 解读使用本次搜索树的原有日期范围')
    existing_from = program.date_from if program else link.date_from if link else ''
    earliest = _date_from(case, requested_from, existing_from)
    if link:
        run = session.get(ResearchRun, link.current_run_id) if link.current_run_id else None
        if run and run.status in research.ACTIVE:
            if earliest != link.date_from:
                raise CausalConflict('RunDesk 正在调查，请在本轮结束后调整最早日期')
            return link.research_case_id, False
    else:
        research_case_id = 'research_' + case_key
        research.create_case(session.get_bind(), {
            'title': ('事件溯源 · ' + case.text)[:250],
            'goal': case.text,
            'event_key': case.event_key,
        }, fixed_case_id=research_case_id, allow_empty=True, session=session)
        link = CausalAgentLink(case_key=case_key, research_case_id=research_case_id, date_from=earliest)
    dossier = research.require_case(session, link.research_case_id)
    link.date_from = dossier.date_from = earliest
    link.last_error = ''
    dossier.updated_at = case.updated_at = utc_now_iso()
    session.add(link)
    session.add(dossier)
    return link.research_case_id, True


def submit(engine, runtime, case_key, research_case_id, enqueue):
    if enqueue:
        try:
            runtime.enqueue(research_case_id)
        except (ValueError, RemoteError) as exc:
            # Keep a visible, retryable investigation even if environment
            # resolution failed before the normal research run was queued.
            with Session(engine) as session:
                research.locked(session)
                link = session.get(CausalAgentLink, case_key)
                active = session.get(ResearchRun, link.current_run_id) if link.current_run_id else None
                if not active or active.status not in research.ACTIVE:
                    link.last_error = str(exc)[:500]
                session.commit()
    with Session(engine) as session:
        return view(session, case_key)
