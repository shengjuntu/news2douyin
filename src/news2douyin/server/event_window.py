"""Expose cached Trade View analysis and choose source-backed event anchors."""
from datetime import date, datetime
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Query, Response

from ..selection.source_content import source_url
from .interop import trade_view_get, trade_view_settings
from .research import same_origin


WINDOW_PATH = '/api/v1/interop/event-window'
CATALOG_PATH = '/api/v1/overseas/series'
Series = Annotated[str, Query(pattern=r'^(?:[A-Z][A-Z0-9_-]{0,24}|[0-9]{6}(?:\.(?:SH|SZ|BJ))?)$')]


def anchors_for_date(raw):
    if not raw:
        return None
    try:
        if len(raw) == 10:
            return date.fromisoformat(raw).isoformat(), 'date'
        stamp = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if stamp.utcoffset() is None or not (stamp.hour or stamp.minute or stamp.second or stamp.microsecond):
            return stamp.date().isoformat(), 'date'
        return stamp.isoformat(), 'timestamp'
    except (TypeError, ValueError):
        return None


def anchor_options(view):
    options = []
    for row in view['sources']:
        url = source_url(row.get('url'))
        parsed = anchors_for_date(row.get('published_at'))
        if url and parsed and not row.get('is_duplicate') and not row.get('requires_original_text'):
            anchor, precision = parsed
            options.append(dict(at=anchor, precision=precision, url=url,
                                title=(row.get('title') or row.get('source_domain') or '原文')[:110],
                                basis='报道发布时间'))
    reviewed = []
    for moment in view['moments']:
        if not (moment['reviewed'] and moment['time_kind'] == 'occurred' and moment['certainty'] == 'exact'
                and moment['date_start'] and moment['date_end'] in ('', moment['date_start'])):
            continue
        parsed = anchors_for_date(moment['date_start'])
        url = next((link for item in moment['sources'] if (link := source_url(item.get('url')))), '')
        if url and parsed and parsed[1] == 'date':
            reviewed.append(dict(at=parsed[0], precision='date', url=url, title=moment['title'][:110],
                                 basis='已核对进展日'))
    options.sort(key=lambda item: (item['precision'] != 'timestamp', item['at']))
    return (options + reviewed)[:24]


def peer(response):
    settings = trade_view_settings()
    if not settings:
        raise HTTPException(503, 'TradeView 行情接口尚未配置')
    response.headers['Cache-Control'] = 'no-store'
    return settings


def register(app):
    @app.get('/api/v1/interop/overseas-series', dependencies=[Depends(same_origin)])
    def overseas_catalog(response: Response):
        data = trade_view_get(*peer(response), CATALOG_PATH)
        items = data.get('items')
        if not isinstance(items, list) or len(items) > 100 or any(not isinstance(item, dict) for item in items):
            raise HTTPException(502, 'TradeView 海外指标目录无效')
        return {'ok': True, 'data': data}

    @app.get(WINDOW_PATH, dependencies=[Depends(same_origin)])
    def event_window(response: Response, series_id: Series, anchor_at: Annotated[str, Query(max_length=45)],
                     precision: Literal['date', 'timestamp'], benchmark_id: Series | None = None):
        params = dict(series_id=series_id, anchor_at=anchor_at, precision=precision)
        if benchmark_id:
            params['benchmark_id'] = benchmark_id
        data = trade_view_get(*peer(response), WINDOW_PATH, params)
        for part in ('anchor', 'series'):
            if not isinstance(data.get(part), dict):
                raise HTTPException(502, 'TradeView 事件窗口响应无效')
        for part, maximum in (('windows', 3), ('sessions', 50)):
            rows = data.get(part)
            if not isinstance(rows, list) or len(rows) > maximum or any(not isinstance(row, dict) for row in rows):
                raise HTTPException(502, 'TradeView 事件窗口响应无效')
        return {'ok': True, 'data': data}
