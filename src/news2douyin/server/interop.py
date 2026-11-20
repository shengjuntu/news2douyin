"""Read-only interoperability between independently deployed applications."""
from datetime import date, timedelta
from hmac import compare_digest
import json
import os
from typing import Annotated, Literal
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from fastapi import Depends, Header, HTTPException, Query, Response
from pydantic import BaseModel, FiniteFloat
import requests
from sqlalchemy import func, or_
from sqlmodel import select

from ..events.service import timestamp
from ..search.dates import date_range
from ..selection.source_content import source_url
from ..storage.db import session_scope
from ..storage.models import Article, ArticleEventLink, Event, EventMoment, EventState
from ..storage.utils import loads
from .research import same_origin


TIMEZONE = 'Asia/Shanghai'
MAX_DAYS = 366
MAX_MARKET_BYTES = 256 * 1024
MARKET_PATH = '/api/v1/interop/market-series'
DateQuery = Annotated[str, Query(pattern=r'^[0-9]{4}-[0-9]{2}-[0-9]{2}$')]
SymbolQuery = Annotated[str, Query(pattern=r'^[0-9]{6}(\.(SH|SZ|BJ|sh|sz|bj))?$')]


def checked_range(start, end):
    try:
        lower, upper = date_range('custom', start, end, TIMEZONE)
        if (date.fromisoformat(end) - date.fromisoformat(start)).days >= MAX_DAYS:
            raise ValueError(f'日期范围最多 {MAX_DAYS} 天')
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return lower, upper


def require_news_key(x_api_key: Annotated[str | None, Header()] = None):
    expected = os.getenv('NEWS2DOUYIN_INTEROP_API_KEY', '').strip()
    if not expected:
        raise HTTPException(503, '事件互通接口尚未配置')
    if not compare_digest((x_api_key or '').encode(), expected.encode()):
        raise HTTPException(401, '缺少有效的 X-API-Key')


def reviewed_day(moments):
    """Use only one unambiguous, evidence-backed day from the curated timeline."""
    reviewed = [m for m in moments if m['reviewed'] and m['time_kind'] == 'occurred']
    if len(reviewed) != 1:
        return None
    moment = reviewed[0]
    start = moment['date_start']
    if moment['certainty'] != 'exact' or not start or moment['date_end'] not in ('', start):
        return None
    try:
        if date.fromisoformat(start).isoformat() == start and moment['sources']:
            return start
    except ValueError:
        pass
    return None


def list_events(session, lower, upper, q, limit):
    title = func.coalesce(EventState.title_override, Event.event_title)
    primary_source = (
        select(Article.url)
        .join(ArticleEventLink, ArticleEventLink.article_key == Article.article_key)
        .where(ArticleEventLink.event_key == Event.event_key,
               or_(Article.url.startswith('https://'), Article.url.startswith('http://')))
        .order_by(Article.is_duplicate, Article.id)
        .limit(1)
        .correlate(Event)
        .scalar_subquery()
    )
    # Strip the UTC suffix for bounds so both Z and +00:00 spellings compare
    # equally at midnight. The storage writer normalizes timestamps to UTC.
    stmt = (
        select(Event.event_key, title, Event.topic, Event.first_seen_at, primary_source)
        .outerjoin(EventState, EventState.event_key == Event.event_key)
        .where(or_(EventState.merged_into.is_(None), EventState.merged_into == ''),
               Event.first_seen_at >= lower[:19], Event.first_seen_at < upper[:19])
        .order_by(Event.first_seen_at.desc(), Event.event_key)
        .limit(limit + 1)
        .execution_options(n2d_query_name='interop.events.page')
    )
    if q:
        term = q.lower()
        stmt = stmt.where(or_(func.lower(title).contains(term, autoescape=True),
                              func.lower(Event.topic).contains(term, autoescape=True)))
    rows = session.exec(stmt).all()
    items = [
        dict(event_key=key, title=title, topic=topic, first_seen_at=seen,
             source_url=source_url(url) or None, event_path='/events/' + quote(key, safe=''))
        for key, title, topic, seen, url in rows[:limit]
    ]
    if items:
        # At most one reviewed occurrence provides a usable day. Count candidates
        # in SQL so long event timelines are never loaded into the feed.
        one_moment = (
            select(EventMoment.event_key, func.min(EventMoment.moment_key).label('moment_key'))
            .where(EventMoment.event_key.in_([item['event_key'] for item in items]),
                   EventMoment.deleted.is_(False), EventMoment.reviewed.is_(True),
                   EventMoment.time_kind == 'occurred')
            .group_by(EventMoment.event_key)
            .having(func.count() == 1)
            .subquery()
        )
        moment_rows = session.exec(
            select(EventMoment).join(one_moment, one_moment.c.moment_key == EventMoment.moment_key)
            .execution_options(n2d_query_name='interop.events.reviewed_dates')
        ).all()
        dates = {}
        for moment in moment_rows:
            evidence = loads(moment.sources_json, [])
            day = reviewed_day([dict(reviewed=moment.reviewed, time_kind=moment.time_kind,
                                     certainty=moment.certainty, date_start=moment.date_start,
                                     date_end=moment.date_end, sources=evidence)])
            if day:
                dates[moment.event_key] = day
        for item in items:
            if day := dates.get(item['event_key']):
                item['reviewed_moment_date'] = day
    return items, len(rows) > limit


def trade_view_settings():
    base = os.getenv('NEWS2DOUYIN_TRADE_VIEW_BASE_URL', '').strip().rstrip('/')
    key = os.getenv('NEWS2DOUYIN_TRADE_VIEW_API_KEY', '').strip()
    if not base or not key or any(ord(char) < 33 for char in base):
        return None
    try:
        parsed = urlsplit(base)
        port = parsed.port
        if (parsed.scheme in ('http', 'https') and parsed.hostname and port != 0
                and not (parsed.username or parsed.password or parsed.query or parsed.fragment)):
            return base, key
    except ValueError:
        pass
    return None


class MarketBar(BaseModel):
    date: date
    open: FiniteFloat | None = None
    high: FiniteFloat | None = None
    low: FiniteFloat | None = None
    close: FiniteFloat | None = None
    volume: FiniteFloat | None = None
    amount: FiniteFloat | None = None
    source: str | None = None
    adjustment: str | None = None
    price_unit: str | None = None
    volume_unit: str | None = None
    amount_unit: str | None = None


class MarketSeries(BaseModel):
    symbol: str
    frequency: Literal['1d']
    timezone: Literal['Asia/Shanghai']
    start: date
    end: date
    availability: Literal['available', 'no_data']
    source: str | None = None
    items: list[MarketBar]


def trade_view_get(base, key, path, params=None):
    try:
        with requests.Session() as client:
            client.trust_env = False
            with client.get(base + path, params=params,
                            headers={'X-API-Key': key, 'Accept': 'application/json'},
                            timeout=(3, 8), allow_redirects=False, stream=True) as upstream:
                if upstream.status_code == 404:
                    raise HTTPException(404, 'TradeView 未找到该证券或接口')
                if upstream.status_code in (400, 422):
                    raise HTTPException(422, 'TradeView 无法识别证券代码或日期范围')
                if upstream.status_code != 200:
                    raise HTTPException(502, 'TradeView 未能提供行情')
                body = bytearray()
                for chunk in upstream.iter_content(chunk_size=8192):
                    body.extend(chunk)
                    if len(body) > MAX_MARKET_BYTES:
                        raise HTTPException(502, 'TradeView 行情响应超出大小限制')
        data = json.loads(body)
        if not isinstance(data, dict) or data.get('ok') is not True or not isinstance(data.get('data'), dict):
            raise ValueError('Invalid TradeView envelope')
        return data['data']
    except requests.exceptions.Timeout:
        raise HTTPException(504, 'TradeView 行情查询超时') from None
    except (requests.RequestException, ValueError):
        raise HTTPException(502, 'TradeView 行情连接或响应无效') from None


def read_market_series(base, key, symbol, start, end):
    payload = trade_view_get(base, key, MARKET_PATH, dict(symbol=symbol, start=start, end=end))
    try:
        series = MarketSeries.model_validate(payload)
        days = [bar.date for bar in series.items]
        code, _, exchange = series.symbol.partition('.')
        symbol_matches = (series.symbol == symbol or (code == symbol and exchange in {'SH', 'SZ', 'BJ'}))
        if (not symbol_matches or series.start.isoformat() != start or series.end.isoformat() != end
                or len(days) > MAX_DAYS or days != sorted(set(days))
                or bool(days) != (series.availability == 'available')
                or any(not series.start <= day <= series.end for day in days)):
            raise ValueError('Mismatched TradeView series')
    except ValueError:
        raise HTTPException(502, 'TradeView 行情连接或响应无效') from None
    return series.model_dump(mode='json')


def event_market_context(view, first_seen_at):
    if not trade_view_settings() or view['merged_into']:
        return None
    day = reviewed_day(view['moments'])
    if day:
        point, basis = date.fromisoformat(day), '已核对的进展日期'
    else:
        recorded = timestamp(first_seen_at)
        if not recorded:
            return None
        point = recorded.astimezone(ZoneInfo(TIMEZONE)).date()
        basis = '最早记录日期'
    lower = min(5, (point - date.min).days)
    upper = min(5, (date.max - point).days)
    return dict(day=point.isoformat(), basis=basis,
                start=(point - timedelta(days=lower)).isoformat(),
                end=(point + timedelta(days=upper)).isoformat())


def register(app, engine):
    from .event_window import register as register_window
    register_window(app)

    @app.get('/api/v1/interop/news-events', dependencies=[Depends(require_news_key)])
    def news_events(response: Response, start: DateQuery, end: DateQuery,
                    q: Annotated[str, Query(max_length=120)] = '',
                    limit: Annotated[int, Query(ge=1, le=100)] = 25):
        lower, upper = checked_range(start, end)
        with session_scope(engine) as session:
            items, truncated = list_events(session, lower, upper, q.strip(), limit)
        response.headers['Cache-Control'] = 'no-store'
        return dict(ok=True, data=dict(start=start, end=end, timezone=TIMEZONE,
                                      time_basis='first_seen_at', items=items, truncated=truncated))

    @app.get(MARKET_PATH, dependencies=[Depends(same_origin)])
    def market_series(response: Response, symbol: SymbolQuery, start: DateQuery, end: DateQuery):
        settings = trade_view_settings()
        if not settings:
            raise HTTPException(503, 'TradeView 行情接口尚未配置')
        checked_range(start, end)
        response.headers['Cache-Control'] = 'no-store'
        return dict(ok=True, data=read_market_series(*settings, symbol.upper(), start, end))
