"""Read bounded native commodity series from the configured TradeView service."""
from datetime import date
from math import isfinite
import re
from urllib.parse import urlencode

from fastapi import HTTPException


SERIES_TOOL = 'research_commodity_series'
OBSERVATIONS_TOOL = 'research_commodity_observations'
TOOLS = {SERIES_TOOL, OBSERVATIONS_TOOL}
SERIES_PATH = '/api/v1/commodities/series'
OBSERVATIONS_PATH = '/api/v1/commodities/observations'


def _day(value):
    day = date.fromisoformat(value)
    if day.isoformat() != value:
        raise ValueError('日期须为 YYYY-MM-DD')
    return day


def query(name, args):
    if name == SERIES_TOOL:
        product = args.get('product_id', '')
        q = args.get('q', '').strip()
        if product and not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', product):
            raise ValueError('商品编号格式无效')
        if len(q) > 120 or not (product or q):
            raise ValueError('请填写商品编号或 1–120 字的搜索词')
        return SERIES_PATH, dict(product_id=product or None, q=q or None, limit=24)

    series = args['series_id']
    if len(series) > 160 or not re.fullmatch(r'[^:,\s]+:[^,\s]+\.[^,\s]+', series):
        raise ValueError('请使用序列目录返回的原生 series_id')
    start, end = _day(args['start']), _day(args['end'])
    if not 0 <= (end - start).days < 90:
        raise ValueError('一次可读取最长 90 天的统计期')
    return OBSERVATIONS_PATH, dict(series_id=series, start=start.isoformat(), end=end.isoformat())


def fetch(path, params):
    from ..server.interop import trade_view_get, trade_view_settings

    settings = trade_view_settings()
    if not settings:
        raise ValueError('TradeView 商品接口未配置，请记录行情缺口')
    try:
        data = trade_view_get(*settings, path, params)
    except HTTPException as exc:
        messages = {
            404: 'TradeView 找不到此商品序列或接口',
            422: 'TradeView 拒绝此商品或统计期',
            504: 'TradeView 商品查询超时',
        }
        raise ValueError(messages.get(exc.status_code, 'TradeView 商品服务暂不可用')) from None
    if data.get('vintage') != 'latest':
        raise ValueError('TradeView 行情修订口径未知')
    if path == SERIES_PATH:
        rows = data.get('items')
        if not isinstance(rows, list) or len(rows) > params['limit']:
            raise ValueError('TradeView 序列目录响应无效')
        return {key: data.get(key) for key in ('items', 'mapping_state', 'product_id', 'truncated', 'vintage')}
    _observations(data, params)
    return data


def _observations(data, request):
    rows = data.get('items')
    metadata = data.get('metadata')
    if (any(data.get(key) != request[key] for key in ('series_id', 'start', 'end'))
        or not isinstance(metadata, dict) or metadata.get('series_id') != request['series_id']
        or not metadata.get('source') or not metadata.get('freq')
        or not isinstance(rows, list) or len(rows) > 90):
        raise ValueError('TradeView 商品观测响应无效')
    previous = None
    for row in rows:
        try:
            day = _day(row['date'])
            value = row['value']
            if (not request['start'] <= day.isoformat() <= request['end']
                or previous and day <= previous
                or value is not None and (type(value) not in (int, float) or not isfinite(value))):
                raise ValueError()
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError('TradeView 商品观测日期或数值无效') from None
        previous = day


def citation(data, params):
    from ..server.product_pages import trade_view_public_url

    meta = data['metadata']
    series_id = data['series_id']
    title = str(meta.get('name') or series_id)
    lines = [
        f'{title} · {series_id}', '数据口径 最新修订值',
        f'来源 {meta["source"]} · 频率 {meta["freq"]} · 单位 {meta.get("unit") or "未注明"}',
        f'统计期 {data["start"]} 至 {data["end"]}',
    ]
    lines.extend(f'{item["date"]} | {item["value"] if item["value"] is not None else "缺值"}'
                 for item in data['items'])
    if not data['items']:
        lines.append('所选统计期未返回观测')
    # Link only to the administrator's browser-facing endpoint. The raw
    # observation snapshot and immutable citation are saved inside the case.
    public = trade_view_public_url()
    url = public + OBSERVATIONS_PATH + '?' + urlencode(params) if public else ''
    return dict(title=title, url=url, text='\n'.join(lines), kind='market_observation',
                provenance=dict(provider='tradeview', time_basis='latest_revised',
                                series_id=series_id, metadata=meta, query=params))
