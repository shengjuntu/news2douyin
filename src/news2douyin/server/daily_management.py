"""Read local archive coverage and enqueue daily maintenance on explicit POST."""
from datetime import date, datetime, timedelta, timezone
import json

from fastapi import Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session, select

from ..digest import service
from ..digest.calendar import BEIJING, days_before
from ..digest.models import DailyDigestIssue
from .research import same_origin


WINDOW = 7
ACTIVE_JOBS = {'queued', 'running', 'submit_digest', 'awaiting_digest'}
JOB_LABELS = {
    'queued': '已排队', 'running': '正在补采', 'partial': '部分缺口未补齐',
    'submit_digest': '等待提交日报重算', 'awaiting_digest': '正在等待日报重算',
    'completed': '已完成', 'completed_partial': '已按部分批次重算', 'failed': '失败',
}
BUILD_LABELS = {'pending': '已排队', 'building': '正在生成', 'failed': '生成失败'}


def parsed_day(value, today):
    try:
        result = date.fromisoformat(value)
    except ValueError:
        result = None
    if result is None or result.isoformat() != value or result > today:
        raise HTTPException(422, '请选择今天或以前的日期，格式为 YYYY-MM-DD（北京时间）')
    return result


def _issue_rows(engine, start, end):
    issue = DailyDigestIssue
    # Read only the status fields required by this overview.
    query = (select(issue.day, issue.status, issue.requested, issue.generated_at, issue.error, issue.stats_json)
             .where(issue.day >= start.isoformat(), issue.day <= end.isoformat()))
    with Session(engine) as session:
        return {key: (state, requested, generated, error, json.loads(stats))
                for key, state, requested, generated, error, stats in session.exec(query)}


def _edition(day, row):
    state, requested, generated, error, stats = row or (None, False, '', '', {})
    result = service.issue_metadata(day, state, requested, generated, error, stats)
    building = result['build_status']
    has_snapshot = result['status'] == 'ready'
    # Missing days are eligible for generation without already being queued.
    label = '未生成'
    if has_snapshot:
        label = '当日版' if result['edition_kind'] == 'intraday' else '已生成'
    if building in BUILD_LABELS:
        label = BUILD_LABELS[building]
        if has_snapshot:
            label += ' · 可读上版'
    result.update(label=label, has_snapshot=has_snapshot, selected=stats.get('selected') if has_snapshot else None)
    return result


def _batch_clock(stamp):
    try:
        utc = datetime.strptime(stamp, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return str(stamp)
    return utc.astimezone(BEIJING).strftime('%m-%d %H:%M')


def overview(engine, day_value='', end_value='', *, now=None):
    from ..digest import backfill
    now = now or datetime.now(BEIJING)
    today = now.astimezone(BEIJING).date()
    day = parsed_day(day_value, today) if day_value else None
    end = parsed_day(end_value, today) if end_value else (day or today)
    selected = day or end
    start = days_before(end, WINDOW - 1)
    if not start <= selected <= end:
        raise HTTPException(422, '查看日期须在当前七天窗口内')
    calendar = backfill.coverage(engine, start, end, now=now)
    issues = _issue_rows(engine, start, end)
    days = []
    for row in sorted(calendar['days'], key=lambda r: r['day'], reverse=True):
        key = row['day']
        edition = _edition(date.fromisoformat(key), issues.get(key))
        days.append(dict(row, issue=edition))
    detail = next(row for row in days if row['day'] == selected.isoformat())
    jobs = backfill.list_jobs(engine, limit=20)
    pending_build = any(row['issue']['build_status'] in ('pending', 'building') for row in days)
    active = pending_build or any(job['status'] in ACTIVE_JOBS for job in jobs)
    if detail.get('job') and detail['job']['status'] in ACTIVE_JOBS:
        active = True
    return dict(days=days, detail=detail, jobs=jobs, enabled_feeds=calendar['enabled_feeds'],
                receiver_enabled=calendar['receiver_enabled'], digest_enabled=service.worker_enabled(),
                selected=selected.isoformat(), start=start.isoformat(), end=end.isoformat(), today=today.isoformat(),
                prev_end=days_before(start, 1).isoformat() if start > date.min else '',
                next_end=(end + timedelta(days=min(WINDOW, (today-end).days))).isoformat() if end < today else '',
                running=active)


def register(app, engine, render):
    def page(day, end, message='', error=False):
        data = overview(engine, day, end)
        return render('daily_management.html', active='admin', data=data, message=message, error=error,
                      job_labels=JOB_LABELS, active_jobs=ACTIVE_JOBS, batch_clock=_batch_clock)

    @app.get('/admin/daily-radar', response_class=HTMLResponse, include_in_schema=False)
    def manage_daily(day: str='', end: str=''):
        return page(day, end)

    @app.get('/api/admin/daily-radar')
    def manage_data(day: str='', end: str=''):
        return overview(engine, day, end)

    def enqueue(day, action):
        from ..digest import backfill
        chosen = parsed_day(day, datetime.now(BEIJING).date())
        if action == 'rebuild':
            issue = service.request_rebuild(engine, chosen)
            return {'day': day, 'build_status': issue['build_status'], 'action': action}
        if action == 'publish-partial':
            return {'day': day, 'action': action, 'job': backfill.publish_available(engine, chosen)}
        if action not in ('backfill', 'retry'):
            raise HTTPException(404, '未知日报操作')
        return {'day': day, 'action': action, 'job': backfill.enqueue(engine, chosen, retry=action=='retry')}

    @app.post('/api/admin/daily-radar/{day}/{action}', status_code=202, dependencies=[Depends(same_origin)])
    def manage_submit(day: str, action: str):
        try:
            return enqueue(day, action)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(422 if isinstance(exc, ValueError) else 409, str(exc)) from exc

    @app.post('/admin/daily-radar/{day}/{action}', dependencies=[Depends(same_origin)], include_in_schema=False)
    def manage_form(day: str, action: str, end: str=''):
        today = datetime.now(BEIJING).date()
        chosen = parsed_day(day, today)
        window_end = parsed_day(end, today) if end else chosen
        if not days_before(window_end, WINDOW - 1) <= chosen <= window_end:
            raise HTTPException(422, '查看日期须在当前七天窗口内')
        try:
            enqueue(day, action)
        except (ValueError, RuntimeError) as exc:
            response = page(day, end, str(exc), error=True)
            response.status_code = 422 if isinstance(exc, ValueError) else 409
            return response
        # A refresh shows progress without submitting the same form a second time.
        return RedirectResponse(f'/admin/daily-radar?day={day}&end={window_end.isoformat()}#day-detail', 303)
