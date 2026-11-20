"""Read-only views of the daily geopolitical briefing."""
from datetime import date
from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, or_
from sqlmodel import select

from ..dedup.normalize import canonicalize_url
from ..georisk.service import dashboard, evidence_for_day
from ..georisk.config import settings
from ..selection.source_content import source_url
from ..storage.db import session_scope
from ..storage.models import Article, ArticleEventLink, Event, EventState


PATH = '/radar/georisk'
Day = Annotated[str, Query(pattern=r'^(?:[0-9]{4}-[0-9]{2}-[0-9]{2})?$')]


def read_dashboard(session, scope, category, days, end_day):
    try:
        if not category:
            categories = settings().categories
            category = 'conflict' if 'conflict' in categories else next(iter(categories))
        return dashboard(session, scope=scope, category=category, days=days, end_day=end_day)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None


def related_items(session, evidence, back):
    reports = [{**item, 'url': address} for item in evidence
               if (address := source_url(item.get('url')))]
    if not reports:
        return [], []
    urls = {canonicalize_url(item['url']) for item in reports}
    stmt = (select(Event.event_key, func.coalesce(EventState.title_override, Event.event_title))
            .select_from(Article)
            .join(ArticleEventLink, ArticleEventLink.article_key == Article.article_key)
            .join(Event, Event.event_key == ArticleEventLink.event_key)
            .outerjoin(EventState, EventState.event_key == Event.event_key)
            .where(Article.canonical_url.in_(urls),
                   or_(EventState.merged_into.is_(None), EventState.merged_into == ''))
            .distinct().order_by(Event.event_key).limit(12))
    return reports, [
        dict(key=key, title=title, path='/events/' + quote(key, safe='') + '?' + urlencode({'back': back}))
        for key, title in session.exec(stmt)
    ]


def focus_seed(session, risk):
    scope, separator, suffix = risk.partition('/')
    category, separator2, raw_day = suffix.partition('/')
    try:
        if not separator or not separator2 or date.fromisoformat(raw_day).isoformat() != raw_day:
            raise ValueError('风险线索日期无效')
        evidence = evidence_for_day(session, scope, category, raw_day)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    if not evidence:
        raise HTTPException(404, '该日期暂无可用于跟踪的报道')
    item = evidence[0]
    title = item.get('title') or '地缘风险'
    return title[:80], '持续跟踪「' + title[:200] + '」的后续进展。'


def register(app, engine, render):
    def analysis_status(data):
        worker = app.state.georisk
        if not data['enabled']:
            return 'disabled'
        if worker.status_error or worker.last_error:
            return 'error'
        if not worker.lake.path.is_file():
            return 'archive_absent'
        if not any(item['archived_batches'] is not None for item in data['items']):
            return 'unchecked'
        return 'ready'

    def read(scope, category, days, end_day, day):
        with session_scope(engine) as session:
            data = read_dashboard(session, scope, category, days, end_day)
            category = data['category']
            rows = data['items']
            candidates = [item for item in rows if item.get('spike')] or [
                item for item in rows if item.get('score') is not None]
            selected = next((item for item in rows if item['day'] == day), None) if day else None
            if not day and candidates:
                selected = max(candidates, key=lambda item: item['day'])
            if day and selected is None:
                raise HTTPException(404, '所选日期不在当前窗口内')
            if selected is not None:
                query = urlencode(dict(scope=scope, category=category, days=days, end_day=data['end'], day=selected['day']))
                evidence, events = related_items(session, evidence_for_day(session, scope, category, selected['day']),
                                                PATH + '?' + query)
                focus_url = '/focus?' + urlencode({'new': 1, 'risk': f"{scope}/{category}/{selected['day']}"})
                selected = {**selected, 'evidence': evidence, 'events': events, 'focus_url': focus_url if evidence else None}
            return {**data, 'selected': selected, 'analysis_status': analysis_status(data)}

    @app.get('/api/radar/georisk')
    def risk_api(scope: Annotated[str, Query(max_length=32)] = 'global',
                 category: Annotated[str, Query(max_length=32)] = '',
                 days: Annotated[int, Query(ge=7, le=90)] = 30, end_day: Day = '', day: Day = ''):
        return read(scope, category, days, end_day, day)

    @app.get('/radar/hotspots/risk', include_in_schema=False)
    def legacy_risk_page(request: Request):
        query = request.url.query
        return RedirectResponse(PATH + ('?' + query if query else ''), status_code=307)

    @app.get(PATH, response_class=HTMLResponse)
    def risk_page(scope: Annotated[str, Query(max_length=32)] = 'global',
                  category: Annotated[str, Query(max_length=32)] = '',
                  days: Annotated[int, Query(ge=7, le=90)] = 30, end_day: Day = '', day: Day = ''):
        data = read(scope, category, days, end_day, day)
        scores = [row['score'] for row in data['items'] if row['score'] is not None]
        return render('georisk.html', active='radar', risk=data, chart_max=max(scores, default=0) or 1,
                      show_chart=bool(scores), days=days)
