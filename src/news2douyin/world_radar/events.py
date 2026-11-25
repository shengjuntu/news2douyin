"""A bounded view of reviewed developments and readable official event leads."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from math import isfinite
import re
from urllib.parse import quote, urlencode

from sqlalchemy import case, func, or_
from sqlmodel import select

from ..dedup.normalize import canonicalize_url, normalize_title
from ..desk.models import EventPlace
from ..desk.places import countries
from ..digest.calendar import BEIJING
from ..events.service import reviewed_day, timestamp
from ..hotspots.sources import _bounded_children
from ..intake.official_models import OfficialEvent
from ..selection.event_content import attach, candidates, mention_sources
from ..selection.service import _country_words
from ..selection.source_content import resolve_previews, source_url, text, usable_title
from ..storage.models import Article, ArticleEventLink, ArticleVersion, Event, EventMoment, EventState
from ..storage.queries import all_rows
from ..storage.sql import json_text
from ..storage.utils import loads


CATEGORIES = {
    'policy': '政策监管',
    'diplomacy': '外交',
    'conflict': '冲突与安全',
    'disaster': '自然灾害',
    'industry': '产业运营',
    'transport': '运输物流',
    'supply': '供需与库存',
    'other': '其他',
}
LOCAL_LIMIT = 90
OFFICIAL_LIMIT = 96
MOMENT_LIMIT = 90

_CATEGORIES = tuple((kind, re.compile(pattern, re.I)) for kind, pattern in (
    ('policy', r'制裁|禁运|出口(?:限制|管制|许可|关税|税)|进口(?:限制|配额|税)|关税|监管|法案|正式颁布|\b(?:sanctions?|embargo(?:es)?|tariffs?|export (?:controls?|duties|tax(?:es)?|ban(?:s)?|licen[cs]es?|restrictions?)|import (?:quotas?|duties|ban(?:s)?)|passed (?:a |the )?bill|enacted)\b'),
    ('disaster', r'地震|海啸|洪灾|洪涝|洪水|火山喷发|森林火灾|台风|飓风|山体滑坡|\b(?:earthquake|tsunami|flood(?:s|ing)?|typhoon|hurricane|volcanic eruption|wildfire|landslide)\b'),
    ('conflict', r'空袭|袭击|轰炸|停火|交战|交火|军事|武装冲突|\b(?:air[ -]?strikes?|missile (?:attack|strike)|ceasefire|military|armed conflict|shelling|invasion)\b'),
    ('transport', r'港口|铁路|封航|停航|改道|航运|运费|运输中断|桥梁(?:受损|关闭)|\b(?:ports?|railway|shipping|freight|bridges?|strait|rerout(?:e|ed|ing))\b'),
    ('industry', r'矿山|矿场|冶炼|炼厂|工厂|停产|复产|减产|停工|罢工|\b(?:mines?|smelters?|refiner(?:y|ies)|factories|plants?|output cuts?|shut(?:s|ting)? down|workforce strike)\b'),
    ('supply', r'供应|需求|库存|储量|交割|紧缺|\b(?:supply|demand|inventor(?:y|ies)|stockpiles?|shortages?)\b'),
    ('diplomacy', r'峰会|会晤|外交|会谈|使馆|\b(?:summits?|diploma(?:cy|tic)|bilateral talks|ministerial meetings?)\b'),
))
_CAMEO_CATEGORIES = {
    **dict.fromkeys(('04', '05', '06', '07', '08'), 'diplomacy'),
    **dict.fromkeys(('13', '15', '18', '19', '20'), 'conflict'),
    '14': 'conflict',
}
_STAGES = tuple((label, re.compile(pattern, re.I)) for label, pattern in (
    ('宣布或拟议', r'宣布|拟议|拟于|将于|计划|\b(?:announc(?:es?|ed)|propos(?:es?|ed)|plans? to|will (?:take effect|impose|reopen|resume|close))\b'),
    ('已生效', r'开始实施|进入实施|已生效|正式生效|\b(?:t(?:akes?|ook) effect|came into force|entered into force)\b'),
    ('恢复或解除', r'恢复|解除|复产|解封|重开|\b(?:reopen(?:s|ed)?|resum(?:es?|ed)|lifts?|restor(?:es?|ed))\b'),
    ('暂停或中断', r'暂停|中断|停产|停航|关闭|\b(?:suspend(?:s|ed)?|shuts? down|shutdown|closes?|closed|disrupt(?:ion|ed))\b'),
))


def _categories(title, cameo=''):
    return ([kind for kind, pattern in _CATEGORIES if pattern.search(title)]
            or [_CAMEO_CATEGORIES.get(cameo, 'other')])


def classify(title, cameo=''):
    return _categories(title, cameo)[0]


def _address(value):
    url = source_url(value)
    return '' if '\\' in url else url


def _day_bounds(start, end):
    first = start.astimezone(BEIJING).date()
    last = (end - timedelta(microseconds=1)).astimezone(BEIJING).date()
    return first.isoformat(), last.isoformat()


def _stamp(value):
    # Stored UTC strings use either Z or +00:00 and may contain fractions.
    # Compare through whole seconds for index selection and enforce exact times
    # on the bounded SQL results.
    return value.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')


def _article_path(key, revision=None):
    if not key:
        return ''
    url = '/articles/' + quote(key, safe='')
    return url + '?' + urlencode({'revision': revision}) if revision else url


def _card(*, key, title, source, source_kind, observed, occurred='', country='', place='',
          point=None, cameo='', event_key='', candidate_key='', moment_key='', status='', geography_basis=''):
    categories = _categories(title, cameo)
    category = categories[0]
    path = ('/events/' + quote(event_key, safe='') if event_key else
            '/organize?' + urlencode({'candidate': candidate_key}) if candidate_key else '')
    return dict(
        key=key, title=text(title, 250), excerpt=source['excerpt'], category=category,
        categories=categories, category_label=CATEGORIES[category],
        stage=next((label for label, pattern in _STAGES if pattern.search(title)), ''),
        observed_at=observed, occurred_on=occurred, country=country, place=place,
        latitude=point[0] if point else None, longitude=point[1] if point else None,
        precision='location' if point else 'country' if country else 'unknown',
        source_url=source['url'], source_title=source['title'], source_path=source['path'],
        source_kind=source_kind, event_path=path, event_key=event_key, candidate_key=candidate_key,
        moment_key=moment_key, status=status, geography_basis=geography_basis,
    )


def _source(url, title, excerpt, key='', revision=None):
    url = _address(url)
    return dict(url=url, title=text(title, 350), excerpt=text(excerpt, 650),
                path=_article_path(key, revision) or url) if url else None


def _local_query(query, country):
    query = query.outerjoin(EventState, EventState.event_key == Event.event_key).where(
        or_(EventState.merged_into.is_(None), EventState.merged_into == ''))
    if country:
        marker = (select(EventPlace.key).where(EventPlace.event == Event.event_key,
                    EventPlace.role == 'action', EventPlace.iso == country).correlate(Event).exists())
        query = query.where(marker)
    return query


def _places(session, keys):
    places = defaultdict(set)
    if keys:
        for key, iso, article, revision in all_rows(session, select(
                EventPlace.event, EventPlace.iso, EventPlace.article, EventPlace.revision
            ).where(EventPlace.event.in_(keys), EventPlace.role == 'action').distinct(),
            name='world_radar.action_places'):
            if iso in countries():
                places[key].add((iso, article, revision))
    return places


def _single_country(places, refs=None):
    values = {iso for iso, article, revision in places
              if refs is None or (article, revision) in refs}
    return next(iter(values)) if len(values) == 1 else ''


def _reviewed_source(evidence):
    if not isinstance(evidence, list):
        return None, set()
    sources, refs = [], set()
    for item in evidence:
        if not isinstance(item, dict) or not item.get('excerpt'):
            continue
        key, revision = item.get('article_key'), item.get('revision')
        if not isinstance(key, str) or not key or type(revision) is not int or revision < 1:
            continue
        source = _source(item.get('url'), item.get('title'), item['excerpt'], key, revision)
        if source:
            sources.append(source)
            refs.add((key, revision))
    return (sources[0] if sources else None), refs


def _saved_report_queries(before):
    version_id = (
        select(ArticleVersion.id).where(ArticleVersion.article_key == Article.article_key,
                                        ArticleVersion.observed_at < before)
        .order_by(ArticleVersion.id.desc()).limit(1).correlate(Article).scalar_subquery()
    )
    doc = ArticleVersion.payload_json

    def saved_value(key, unversioned_value, length):
        value = case((ArticleVersion.id.is_not(None), json_text(doc, key)), else_=unversioned_value)
        return func.substr(value, 1, length)

    def linked(key):
        return (
            select(ArticleEventLink.event_key, Article.article_key, ArticleVersion.revision,
                   saved_value('url', Article.url, 16001), saved_value('title', Article.title, 350),
                   saved_value('content', Article.content, 650))
            .join(Article, Article.article_key == ArticleEventLink.article_key)
            .outerjoin(ArticleVersion, ArticleVersion.id == version_id)
            .where(ArticleEventLink.event_key == key,
                   or_(ArticleVersion.id.is_not(None),
                       func.coalesce(func.nullif(Article.fetched_at, ''), Article.created_at) < before))
            .order_by(Article.is_duplicate, ArticleEventLink.article_key).limit(5)
        )
    return linked


def _local_cards(session, start, end, country):
    low, high = _stamp(start), _stamp(end + timedelta(seconds=bool(end.microsecond)))
    first, last = _day_bounds(start, end)
    timeline = _local_query(select(EventMoment, Event.last_seen_at)
                           .join(Event, Event.event_key == EventMoment.event_key), country)
    agenda = all_rows(session, timeline.where(EventMoment.reviewed.is_(True), EventMoment.deleted.is_(False),
                            EventMoment.time_kind == 'occurred', EventMoment.certainty == 'exact',
                            EventMoment.date_start.between(first, last))
                       .order_by(EventMoment.date_start.desc(), EventMoment.moment_key).limit(MOMENT_LIMIT + 1),
                       name='world_radar.moments')
    working = _local_query(select(Event.event_key, Event.last_seen_at,
                               func.substr(func.coalesce(EventState.title_override, Event.event_title), 1, 250)), country)
    rows = all_rows(session, working.where(Event.last_seen_at >= low, Event.last_seen_at < high)
                    .order_by(Event.last_seen_at.desc(), Event.event_key).limit(LOCAL_LIMIT + 1),
                    name='world_radar.events')
    places = _places(session, {row[0] for row in rows[:LOCAL_LIMIT]} |
                             {row[0].event_key for row in agenda[:MOMENT_LIMIT]})
    cards, with_moments = [], set()
    for moment, observed_at in agenda[:MOMENT_LIMIT]:
        evidence = loads(moment.sources_json, [])
        source, refs = _reviewed_source(evidence)
        day = reviewed_day([dict(reviewed=True, time_kind='occurred', certainty='exact',
                   date_start=moment.date_start, date_end=moment.date_end, sources=evidence)])
        if not source or not day or not usable_title(moment.title):
            continue
        iso = _single_country(places[moment.event_key], refs)
        if country and iso != country:
            continue
        cards.append(_card(key=moment.moment_key, title=moment.title, source=source, source_kind='local',
                   observed=observed_at, occurred=day, country=iso, event_key=moment.event_key,
                   moment_key=moment.moment_key, status='reviewed', geography_basis='reviewed_action' if iso else ''))
        with_moments.add(moment.event_key)

    selected = {key: (observed, title) for key, observed, title in rows[:LOCAL_LIMIT]
                if key not in with_moments and (when := timestamp(observed)) and start <= when < end}
    reports = {}
    for key, article, revision, url, title, excerpt in _bounded_children(
            session, list(selected), _saved_report_queries(high), name='world_radar.event_reports'):
        if key not in reports and usable_title(title) and (source := _source(url, title, excerpt, article, revision)):
            reports[key] = source
    for key, source in reports.items():
        observed, title = selected[key]
        iso = _single_country(places[key])
        if not country or country == iso:
            cards.append(_card(key='local:' + key, title=title if usable_title(title) else source['title'],
                         source=source, source_kind='local', observed=observed, country=iso, event_key=key,
                         status='curated', geography_basis='reviewed_action' if iso else ''))
    return cards, len(agenda) > MOMENT_LIMIT or len(rows) > LOCAL_LIMIT


def _official_point(row, iso):
    latitude, longitude = row.latitude, row.longitude
    if not iso or latitude is None or longitude is None or not row.action_place or not all(map(isfinite, (latitude, longitude))):
        return None
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180 or (latitude, longitude) == (0, 0):
        return None
    if row.action_place.strip().casefold() in _country_words():
        return None
    return latitude, longitude


def _official_cards(session, start, end, country):
    low, high = _stamp(start), _stamp(end + timedelta(seconds=bool(end.microsecond)))
    first, last = _day_bounds(start, end)
    query = select(OfficialEvent).where(OfficialEvent.last_seen >= low, OfficialEvent.last_seen < high,
                                         OfficialEvent.event_date.between(first, last))
    if country:
        query = query.where(OfficialEvent.action_country_iso2 == country)
    rows = all_rows(session, query.order_by(OfficialEvent.last_seen.desc(), OfficialEvent.event_id.desc())
                    .limit(OFFICIAL_LIMIT + 1), name='world_radar.official')
    events = [row for row in rows[:OFFICIAL_LIMIT] if (when := timestamp(row.last_seen)) and start <= when < end]
    selected = candidates(events, mention_sources(session, [row.event_id for row in events]), _address)
    urls = list(dict.fromkeys(lead['url'] for leads in selected.values() for lead in leads))
    previews = resolve_previews(session, urls)
    final, seen = [], set()
    for row in events:
        card = {}
        # The only time label on an official card comes from GDELT's record.
        # A URL preview describes a report and can discuss multiple actions.
        attach(card, selected[row.event_id], previews)
        preview = card.get('report_preview')
        if not preview or not usable_title(preview.get('title')):
            continue
        source = _source(preview.get('source_url'), preview['title'], preview.get('excerpt'), preview.get('article_key', ''))
        if not source:
            continue
        iso = row.action_country_iso2 if row.action_country_iso2 in countries() else ''
        signature = (canonicalize_url(source['url']), row.event_date, iso, row.action_place.casefold(),
                     row.event_base_code or row.event_code, row.actor1_name.casefold(), row.actor2_name.casefold())
        if signature in seen:
            continue
        seen.add(signature)
        key = f'gdelt:{row.event_id}'
        final.append(_card(key=key, title=source['title'], source=source, source_kind='gdelt', observed=row.last_seen,
                     occurred=row.event_date, country=iso, place=text(row.action_place, 120) if iso else '',
                     point=_official_point(row, iso), cameo=row.event_root_code, candidate_key=key,
                     status='coded', geography_basis='gdelt_action_geo' if iso else ''))
    return final, len(rows) > OFFICIAL_LIMIT


def _balanced(items, limit):
    """Show a regional spread first, then fill unused slots in event order."""
    counts = defaultdict(int)
    preferred, remaining = [], []
    for row in items:
        iso = row['country']
        (preferred if counts[iso] < 3 else remaining).append(row)
        counts[iso] += 1
    return [*preferred, *remaining][:limit]


def developments(session, *, start: datetime, end: datetime, limit=60, country='', category=''):
    """Read current indexed developments for a timezone-aware half-open window."""
    if start.utcoffset() is None or end.utcoffset() is None or not timedelta(0) < end - start <= timedelta(days=32):
        raise ValueError('请选择带时区、最长32天的事件窗口')
    if not 1 <= limit <= 100:
        raise ValueError('事件数须为1–100')
    country = country.lower()
    if country and country not in countries():
        raise ValueError('国家或地区代码无效')
    if category and category not in CATEGORIES:
        raise ValueError('事件分类无效')
    curated, local_limited = _local_cards(session, start, end, country)
    official, official_limited = _official_cards(session, start, end, country)
    signatures = {(canonicalize_url(row['source_url']), normalize_title(row['title']), row['category']) for row in curated}
    items = curated + [row for row in official if
             (canonicalize_url(row['source_url']), normalize_title(row['title']), row['category']) not in signatures]
    items.sort(key=lambda row: (row['occurred_on'] or row['observed_at'][:10], row['status'] == 'reviewed',
                               row['observed_at'], row['key']), reverse=True)
    matching = [row for row in items if not category or category in row['categories']]
    visible = matching[:limit] if country else _balanced(matching, limit)
    return dict(items=visible, sampled=len(items), truncated=local_limited or official_limited or len(matching) > limit)
