"""Bounded observations from already persisted news indexes and editions."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from urllib.parse import urlsplit

from sqlalchemy import func, or_, union_all
from sqlmodel import select

from ..attention.models import AttentionItem, AttentionTopic
from ..candidates.models import SourceReport
from ..desk.models import EventPlace
from ..desk.places import countries
from ..digest.calendar import BEIJING
from ..digest.models import DailyDigestIssue
from ..digest.service import PUBLISHED
from ..intake.official_models import OfficialEvent, OfficialMention
from ..selection.service import CAMEO_LABELS
from ..selection.source_content import resolve_previews, source_url, text, usable_title
from ..storage.models import Article, ArticleEventLink, Event, EventState
from ..storage.queries import all_rows
from ..storage.utils import loads


DAILY_PER_DAY = 100
REPORTS_PER_PERIOD = 128
WORKING_PER_PERIOD = 36
ATTENTION_PER_PERIOD = 32
OFFICIAL_PER_ROOT_PERIOD = 12
OFFICIAL_ROOTS = ('04', '05', '06', '07', '14', '18', '19', '20')
GROUP_READ_SIZE = 24


def _stamp(value):
    return value.isoformat().replace('+00:00', 'Z')


def _date(value):
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (AttributeError, TypeError, ValueError):
        return None
    return result.astimezone(timezone.utc) if result.utcoffset() is not None else None


def _sql_bounds(start, end):
    floor = start.replace(microsecond=0)
    ceiling = end.replace(microsecond=0) + timedelta(seconds=bool(end.microsecond))
    # Bare second prefixes include both canonical UTC formats (whole seconds
    # and fractional seconds); the input adapter checks the exact UTC bounds.
    return floor.isoformat()[:19], ceiling.isoformat()[:19]


def _inside(value, start, end):
    moment = _date(value)
    return _stamp(moment) if moment is not None and start <= moment < end else ''


def _country(value):
    return value.lower() if isinstance(value, str) and value.lower() in countries() else ''


def _words(value, limit=8):
    return [text(word, 100) for word in value[:limit] if isinstance(word, str) and word.strip()] if isinstance(value, (list, tuple)) else []


def _candidate(value):
    if isinstance(value, str) and 1 <= len(value) <= 240 and value.startswith(('gkg:', 'gdelt:', 'local:', 'newsnow:', 'report:')):
        if all(c not in value for c in (' ', '\n', '\r', '\\', '\x00')):
            return value
    return ''


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _safe_url(value):
    url = source_url(value)
    return url if '\\' not in url else ''


def _record(*, adapter, title, excerpt, observed, url, source_kind, source_key='', group='', **signals):
    url = _safe_url(url)
    if not url or not isinstance(title, str) or not usable_title(title) or not observed:
        return None
    candidate = _candidate(source_key)
    key = sha256(f'{adapter}\0{group}\0{url}\0{observed}'.encode()).hexdigest()[:24]
    return dict(key=f'{adapter}:{key}', adapter=adapter, title=text(title, 400), excerpt=text(excerpt, 600),
                observed_at=observed, source_url=url, source_kind=text(source_kind, 32), source_key=candidate,
                domain=urlsplit(url).hostname or '', cluster_key=group, **signals)


def _add(items, **kwargs):
    if record := _record(**kwargs):
        items.append(record)


def _daily(session, start, end):
    first = start.astimezone(BEIJING).date().isoformat()
    last = end.astimezone(BEIJING).date().isoformat()
    rows = all_rows(session, select(DailyDigestIssue.day, DailyDigestIssue.items_json,
                                   DailyDigestIssue.generated_at)
                    .where(DailyDigestIssue.day.between(first, last), DailyDigestIssue.status.in_(PUBLISHED))
                    .order_by(DailyDigestIssue.day.desc()).limit(8), name='hotspots.daily')
    items, limited = [], False
    for _, encoded, generated in rows:
        revision_time = _date(generated)
        if revision_time is not None and revision_time >= end:
            continue
        cards = loads(encoded, [])
        if not isinstance(cards, list):
            continue
        limited |= len(cards) > DAILY_PER_DAY
        for card in cards[:DAILY_PER_DAY]:
            if not isinstance(card, dict):
                continue
            observed = _inside(card.get('source_observed_at') or card.get('observed_at'), start, end)
            group = f'daily:{text(card.get("key"), 160)}' if card.get('key') else ''
            key = card.get('source_key')
            kind = card.get('source_kind') or 'daily'
            geo = _mapping(card.get('geography'))
            category = _mapping(card.get('category'))
            country = _country(geo.get('primary') or card.get('country')) if kind == 'gdelt' or geo.get('basis') == 'official_action' else ''
            official_id = str(key).removeprefix('gdelt:') if str(key).startswith('gdelt:') else ''
            local_key = str(key).removeprefix('local:') if str(key).startswith('local:') else ''
            provenance = _mapping(card.get('source_ref'))
            _add(items, adapter='daily', title=card.get('title'), excerpt=card.get('excerpt'),
                 observed=observed, url=card.get('source_url'), source_kind=kind, source_key=key, group=group,
                 language=text(card.get('language'), 32), provider=text(provenance.get('provider'), 64),
                 country=country, place=text(card.get('action_place'), 120), actors=_words(card.get('actors')),
                 event_key=_candidate(key) if local_key or official_id else '',
                 official_event_id=official_id, local_event_key=local_key, category_hint=text(category.get('label'), 80),
                 themes=_words((category.get('theme'),)), attention_only=kind == 'newsnow')
            # A saved edition establishes group membership for its source sample.
            # It does not store a different observation clock for every source.
            related = card.get('sources')
            observed = _inside(card.get('observed_at'), start, end)
            seen = {_safe_url(card.get('source_url'))}
            for sibling in related[:3] if isinstance(related, list) else ():
                sibling = _mapping(sibling)
                url = _safe_url(sibling.get('url'))
                if not url or url in seen:
                    continue
                seen.add(url)
                kind = sibling.get('source_kind') or 'daily'
                source = _mapping(sibling.get('source_ref'))
                _add(items, adapter='daily', title=sibling.get('title'), excerpt=sibling.get('excerpt'),
                     observed=observed, url=url, source_kind=kind, source_key=sibling.get('source_key'),
                     group=group, provider=text(source.get('provider'), 64), language=text(sibling.get('language'), 32),
                     clock_kind='edition_group', attention_only=kind == 'newsnow')
    return items, limited


def _time_pages(session, query, column, key, periods, limit, *, name):
    rows, limited = [], False
    for start, end in periods:
        low, high = _sql_bounds(start, end)
        page = all_rows(session, query.where(column >= low, column < high)
                        .order_by(column.desc(), key.desc()).limit(limit + 1), name=name)
        limited |= len(page) > limit
        eligible = [row for row in page if _inside(row._mapping[column.key], start, end)]
        rows.extend(eligible[:limit])
    return rows, limited


def _bounded_children(session, identities, make_query, *, name):
    # Portable bounded index scans per parent. Each UNION branch has an explicit
    # per-parent LIMIT, without ranking the complete relation with a window sort.
    for offset in range(0, len(identities), GROUP_READ_SIZE):
        chunk = identities[offset:offset + GROUP_READ_SIZE]
        branches = [make_query(ident).subquery() for ident in chunk]
        parts = [select(*branch.c) for branch in branches]
        query = union_all(*parts) if len(parts) > 1 else parts[0]
        for row in all_rows(session, query, name=name):
            yield row


def _reports(session, start, end, periods):
    query = select(SourceReport.key, SourceReport.provider, func.substr(SourceReport.title, 1, 400),
                   func.substr(SourceReport.url, 1, 16001),
                   func.substr(SourceReport.excerpt, 1, 600), SourceReport.published_at,
                   SourceReport.observed_at, SourceReport.language)
    rows, limited = _time_pages(session, query, SourceReport.observed_at, SourceReport.key, periods,
                                REPORTS_PER_PERIOD, name='hotspots.reports')
    items = []
    for key, provider, title, url, excerpt, published, observed, language in rows:
        _add(items, adapter='reports', title=title, excerpt=excerpt, observed=_inside(observed, start, end),
             url=url, source_kind='report', source_key=key, provider=text(provider, 64),
             published_at=published, language=text(language, 32))
    return items, limited


def _working(session, start, end, periods):
    query = select(Event.event_key, Event.last_seen_at).outerjoin(EventState, EventState.event_key == Event.event_key).where(
        or_(EventState.merged_into.is_(None), EventState.merged_into == ''))
    selected, limited = _time_pages(session, query, Event.last_seen_at, Event.event_key, periods,
                                    WORKING_PER_PERIOD, name='hotspots.working')
    events = dict(selected)
    if not events:
        return [], limited
    places = defaultdict(set)
    for key, iso in all_rows(session, select(EventPlace.event, EventPlace.iso)
                             .where(EventPlace.event.in_(events), EventPlace.role == 'action').distinct(),
                             name='hotspots.working_places'):
        if country := _country(iso):
            places[key].add(country)

    def related(key):
        return (select(ArticleEventLink.event_key, func.substr(Article.title, 1, 400),
                       func.substr(Article.url, 1, 16001), Article.provider,
                       Article.language, Article.published_at, Article.fetched_at)
                .join(Article, Article.article_key == ArticleEventLink.article_key)
                .where(ArticleEventLink.event_key == key)
                .order_by(ArticleEventLink.article_key).limit(4))

    items = []
    for key, title, url, provider, language, published, fetched in _bounded_children(session, list(events), related,
                                                                                     name='hotspots.working_reports'):
        observed = _inside(fetched or events[key], start, end)
        country = next(iter(places[key])) if len(places[key]) == 1 else ''
        source_key = f'local:{key}'
        _add(items, adapter='working', title=title, excerpt='', observed=observed, url=url, source_kind='local',
             source_key=source_key, group=source_key, event_key=source_key, local_event_key=key,
             country=country, published_at=published, language=text(language, 32), provider=text(provider, 64))
    return items, limited


def _attention(session, start, end, periods):
    query = select(AttentionTopic.key, AttentionTopic.last_seen)
    selected, limited = _time_pages(session, query, AttentionTopic.last_seen, AttentionTopic.key, periods,
                                    ATTENTION_PER_PERIOD, name='hotspots.attention')
    topics = dict(selected)
    low, high = _sql_bounds(start, end)

    def related(key):
        return (select(AttentionItem.topic, func.substr(AttentionItem.title, 1, 400),
                       func.substr(AttentionItem.url, 1, 16001),
                       func.substr(AttentionItem.excerpt, 1, 600), AttentionItem.last_seen, AttentionItem.platform)
                .where(AttentionItem.topic == key, AttentionItem.last_seen >= low, AttentionItem.last_seen < high)
                .order_by(AttentionItem.last_seen.desc(), AttentionItem.key.desc()).limit(2))

    items = []
    for key, title, url, excerpt, observed, platform in _bounded_children(session, list(topics), related,
                                                                          name='hotspots.attention_items'):
        source_key = f'newsnow:{key}'
        _add(items, adapter='attention', title=title, excerpt=excerpt, observed=_inside(observed, start, end),
             url=url, source_kind='newsnow', source_key=source_key, group=source_key,
             attention_only=True, provider='newsnow', platform=text(platform, 80))
    return items, limited


def _official(session, start, end, periods):
    query = select(OfficialEvent.event_id, OfficialEvent.event_date, OfficialEvent.last_seen,
                   OfficialEvent.actor1_name, OfficialEvent.actor2_name, OfficialEvent.actor1_country_iso2,
                   OfficialEvent.actor2_country_iso2, OfficialEvent.event_code, OfficialEvent.event_root_code,
                   OfficialEvent.action_country_iso2, OfficialEvent.action_place, OfficialEvent.source_url)
    events, limited = [], False
    for root in OFFICIAL_ROOTS:
        rows, cap = _time_pages(session, query.where(OfficialEvent.event_root_code == root), OfficialEvent.last_seen,
                                OfficialEvent.event_id, periods, OFFICIAL_PER_ROOT_PERIOD, name='hotspots.official')
        events.extend(rows)
        limited |= cap
    if not events:
        return [], limited

    low, high = _sql_bounds(start, end)

    def related(key):
        return (select(OfficialMention.event_id, OfficialMention.doc_identifier, OfficialMention.first_seen)
                .where(OfficialMention.event_id == key, OfficialMention.last_seen >= low, OfficialMention.last_seen < high)
                .order_by(OfficialMention.last_seen.desc(), OfficialMention.key.desc()).limit(3))

    reports = defaultdict(dict)
    for key, url, first in _bounded_children(session, [e.event_id for e in events], related, name='hotspots.official_mentions'):
        if url := _safe_url(url):
            reports[key][url] = _inside(first, start, end)
    for event in events:
        if url := _safe_url(event.source_url):
            # The primary URL by itself carries no per-document observation
            # time. Keep the source event clock distinct from a mention clock.
            reports[event.event_id].setdefault(url, _inside(event.last_seen, start, end))
    urls = sorted({url for report in reports.values() for url in report})
    previews = {}
    for offset in range(0, len(urls), 250):
        previews.update(resolve_previews(session, urls[offset:offset + 250]))

    items = []
    for event in events:
        if not event.event_id.isascii() or not event.event_id.isdecimal():
            continue
        key = f'gdelt:{event.event_id}'
        actors = _words((event.actor1_name, event.actor2_name), 2)
        actor_countries = [_country(iso) for iso in (event.actor1_country_iso2, event.actor2_country_iso2)]
        fallback = ''
        if event.event_root_code in ('18', '19', '20') and len(actors) == 2 and len(set(filter(None, actor_countries))) == 2:
            # Use an explicit, recent event date before surfacing metadata
            # alone. Recirculation of old event records is common upstream.
            if start.date().isoformat() <= event.event_date <= end.date().isoformat():
                fallback = f'{actors[0]} · {CAMEO_LABELS[event.event_root_code]} · {actors[1]}'
        for url, observed in reports[event.event_id].items():
            preview = previews.get(url, {})
            fetched = _date(preview.get('fetched_at'))
            readable = bool(preview.get('has_content') and fetched is not None and fetched < end)
            title = preview.get('title') if readable else fallback
            _add(items, adapter='official', title=title, excerpt=preview.get('excerpt') if readable else '',
                 observed=observed, url=url, source_kind='gdelt', source_key=key, group=key, event_key=key,
                 official_event_id=event.event_id, official_only=not readable, actors=actors, actor_countries=actor_countries,
                 event_code=event.event_code, event_root_code=event.event_root_code, provider='gdelt',
                 country=_country(event.action_country_iso2), place=text(event.action_place, 120),
                 occurred_on=event.event_date, language=text(preview.get('language'), 32) if readable else '')
    return items, limited


def read_window(session, *, as_of=None, recent_hours=36, previous_hours=36):
    """Read rolling UTC observations, with recent and comparison slices kept separate."""
    if any(type(hours) is not int or not 1 <= hours <= 72 for hours in (recent_hours, previous_hours)):
        raise ValueError('热点时间窗口须为 1–72 小时')
    end = datetime.now(timezone.utc) if as_of is None else as_of
    if not isinstance(end, datetime) or end.utcoffset() is None:
        raise ValueError('热点查询时间须包含时区')
    end = end.astimezone(timezone.utc)
    split = end - timedelta(hours=recent_hours)
    start = split - timedelta(hours=previous_hours)
    periods = ((split, end), (start, split))
    result = {'items': [], 'sources': {}, 'window': dict(start=_stamp(start), split=_stamp(split), end=_stamp(end),
                                                        recent_hours=recent_hours, previous_hours=previous_hours)}
    for key, (items, limited) in (
        ('daily', _daily(session, start, end)),
        ('official', _official(session, start, end, periods)),
        ('working', _working(session, start, end, periods)),
        ('reports', _reports(session, start, end, periods)),
        ('attention', _attention(session, start, end, periods)),
    ):
        result['items'].extend(items)
        result['sources'][key] = dict(count=len(items), limited=limited,
                                      latest_at=max((item['observed_at'] for item in items), default=''))
    return result
