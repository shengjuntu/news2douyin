"""GDELT DOC 2.0 article discovery. The API supplies metadata, not article text."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import requests

from ...search.dates import calendar_day, date_range
from ...storage.models import utc_now_iso


ENDPOINT = 'https://api.gdeltproject.org/api/v2/doc/doc'
CODES = json.loads(Path(__file__).with_name('gdelt_codes.json').read_text(encoding='utf-8'))
COUNTRIES = CODES['countries']  # ISO -> FIPS, GDELT name, ISO display name, Chinese name
LANGUAGES = CODES['languages']  # ISO -> GDELT language name
COUNTRY_NAMES = {name.casefold(): iso for iso, (_, gdelt, english, _) in COUNTRIES.items()
                 for name in (gdelt, english)}
LANGUAGE_NAMES = {name.casefold(): iso for iso, name in LANGUAGES.items()}



def request_timeouts():
    """Shared DOC/GKG network deadlines, configurable without patching source."""
    values=[]
    for name,default in [('NEWS2DOUYIN_GDELT_CONNECT_TIMEOUT','30'),('NEWS2DOUYIN_GDELT_READ_TIMEOUT','90')]:
        try:value=float(os.getenv(name,default))
        except (TypeError,ValueError):raise ValueError(name+' 必须是 1–600 秒')
        if not 1<=value<=600:raise ValueError(name+' 必须是 1–600 秒')
        values.append(value)
    return tuple(values)


def query_for(config):
    if config.get('collection_mode', 'search') != 'search':
        raise ValueError('GDELT 仅支持主动关键词检索')
    query = config.get('search_query')
    if not isinstance(query, str) or not 3 <= len(query.strip()) <= 100:
        raise ValueError('GDELT 检索词需要 3–100 个字符')
    terms = [query.strip()]
    country = config.get('country', '').strip().lower()
    language = config.get('language', '').strip().lower()
    if country:
        if country not in COUNTRIES:
            raise ValueError('GDELT 未配置此来源国的代码映射，请选择列表中的国家或选择全球')
        terms.append('sourcecountry:' + COUNTRIES[country][0])
    if language:
        if language not in LANGUAGES:
            raise ValueError('GDELT 不支持所选报道语言')
        terms.append('sourcelang:' + LANGUAGES[language].lower())
    if not 1 <= int(config.get('max_items', 100)) <= 250:
        raise ValueError('GDELT 单次最多 250 条，至少 1 条')
    return ' '.join(terms)


def _article(data, fetched_at):
    if not isinstance(data, dict):
        return None
    title, url = data.get('title'), data.get('url')
    if not isinstance(title, str) or not title.strip() or not isinstance(url, str):
        return None
    try:
        parsed = urlsplit(url.strip())
        if parsed.scheme not in {'https', 'http'} or not parsed.hostname or parsed.username is not None:
            return None
    except ValueError:
        return None
    raw_country = str(data.get('sourcecountry') or '').strip()
    raw_language = str(data.get('language') or '').strip()
    try:
        stamp = datetime.strptime(data.get('seendate', ''), '%Y%m%dT%H%M%SZ')
        seen_at = stamp.strftime('%Y-%m-%dT%H:%M:%SZ')
    except (ValueError, TypeError):
        seen_at = ''
    return {'title': title.strip(), 'url': url.strip(), 'content': '', 'published_at': '',
            'fetched_at': fetched_at, 'source': {'domain': parsed.hostname.lower()},
            'country': COUNTRY_NAMES.get(raw_country.casefold(), ''),
            'language': LANGUAGE_NAMES.get(raw_language.casefold(), ''),
            'gdelt': {'seen_at': seen_at, 'source_country': raw_country, 'source_language': raw_language}}


def fetch_news(config):
    query = query_for(config)
    zone = config.get('timezone', '')
    day = calendar_day(config.get('date_str', ''), zone)
    lower, upper = date_range('custom', day, day, zone)
    start = datetime.fromisoformat(lower)
    end = min(datetime.fromisoformat(upper) - timedelta(seconds=1), datetime.now(timezone.utc))
    if start > end:
        raise ValueError('GDELT 采集日期不能在未来')
    params = {'query': query, 'mode': 'ArtList', 'format': 'json', 'sort': 'DateDesc',
              'maxrecords': int(config.get('max_items', 100)),
              'startdatetime': start.strftime('%Y%m%d%H%M%S'), 'enddatetime': end.strftime('%Y%m%d%H%M%S')}
    try:
        response = requests.get(ENDPOINT, params=params, timeout=request_timeouts(), allow_redirects=False)
    except requests.RequestException as exc:
        raise RuntimeError('GDELT 连接失败或超时，请稍后重试') from exc
    if response.status_code == 429:
        raise RuntimeError('GDELT HTTP 429，请降低采集频率并稍后重试')
    if response.status_code != 200:
        raise RuntimeError(f'GDELT HTTP {response.status_code}，请检查服务连通性后重试')
    try:
        payload = response.json()
    except ValueError as exc:
        message = ' '.join(response.text.split())[:240]
        raise RuntimeError(f'GDELT 未返回 JSON，请检查检索条件。上游消息：{message}') from exc
    if payload == {}:
        return []
    if not isinstance(payload, dict) or not isinstance(payload.get('articles'), list):
        raise RuntimeError('GDELT 响应缺少有效的文章列表')
    rows = payload['articles']
    fetched_at = utc_now_iso()
    items = [item for data in rows[:params['maxrecords']] if (item := _article(data, fetched_at))]
    if rows and not items:
        raise RuntimeError('GDELT 返回的文章均缺少有效标题或原文链接')
    # Ignore records outside the day even if the upstream silently adjusts an old window.
    lower_iso = start.strftime('%Y-%m-%dT%H:%M:%SZ')
    upper_iso = end.strftime('%Y-%m-%dT%H:%M:%SZ')
    in_window = [item for item in items if lower_iso <= item['gdelt']['seen_at'] <= upper_iso]
    if items and not in_window:
        raise RuntimeError('GDELT 返回的报道无法匹配所选收录日期，请检查上游检索窗口')
    return in_window
