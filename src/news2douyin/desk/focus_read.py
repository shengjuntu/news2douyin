"""Bounded reads over the published Focus membership and local source versions."""
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as Base64Error
from hashlib import sha256
import json
from math import isfinite
import re
from urllib.parse import quote

from sqlalchemy import case, func, or_, true, tuple_
from sqlmodel import select

from ..research.materials import is_lead, raw_metadata, source_document
from ..search.dates import calendar_day
from ..selection.source_content import source_url
from ..storage.articles import snapshot
from ..storage.models import Article, ArticleEventLink, ArticleVersion, Event, EventMoment, EventState
from ..storage.queries import all_rows, exists, scalar
from ..storage.sql import time_value
from ..storage.utils import loads
from .focus_lifecycle import require_active, visible_watch
from .models import EventPlace, GkgSource, RadarEntry, Watch, WatchEvent
from .places import countries
from .service import current_membership


DATE_BASES = {'observed': '最近材料的日期（UTC+08:00）', 'occurred': '最早的已核对发生日期'}
COUNTRY_ROLES = {'any': '已标注行动地或影响地', 'action': '已标注行动地', 'impact': '已标注影响地',
                 'mention': 'GDELT GKG 报道提及地', 'media': '采集报道所登记的国家'}


def checked_dates(date_from='', date_to='', date_basis='observed'):
    if date_basis not in DATE_BASES:
        raise ValueError('日期依据必须为 observed 或 occurred')
    if date_from:
        calendar_day(date_from, 'Etc/GMT-8')
    if date_to:
        calendar_day(date_to, 'Etc/GMT-8')
    if date_from and date_to and date_from > date_to:
        raise ValueError('开始日期不能晚于结束日期')
    return date_from, date_to


def entry_day(session, basis='observed'):
    """Date of an indexed event, retaining a NULL for unknown dates."""
    if basis == 'occurred':
        return (select(func.min(EventMoment.date_start)).where(EventMoment.event_key == RadarEntry.key,
                EventMoment.deleted.is_(False), EventMoment.reviewed.is_(True),
                EventMoment.time_kind == 'occurred', func.length(EventMoment.date_start) == 10)
                .correlate(RadarEntry).scalar_subquery())
    if basis != 'observed':
        raise ValueError('日期依据必须为 observed 或 occurred')
    if session.get_bind().dialect.name == 'postgresql':
        return func.to_char(func.timezone('Etc/GMT-8', func.n2d_timestamp_or_null(RadarEntry.seen)), 'YYYY-MM-DD')
    return func.date(RadarEntry.seen, '+8 hours')


def _size(value, maximum):
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f'分页大小须为 1 至 {maximum} 的整数')
    return value


def _cursor_token(scope, position):
    return urlsafe_b64encode(json.dumps([scope, position], ensure_ascii=False,
                                       separators=(',', ':')).encode()).decode().rstrip('=')


def _read_cursor(cursor, scope, width):
    if not cursor:
        return None
    try:
        if len(cursor) > 2048 or not re.fullmatch(r'[A-Za-z0-9_-]+', cursor):
            raise ValueError()
        key, position = json.loads(urlsafe_b64decode(cursor + '=' * (-len(cursor) % 4)))
        if key != scope or not isinstance(position, list) or len(position) != width:
            raise ValueError()
        if not all(isinstance(value, str) for value in position):
            raise ValueError()
        return position
    except (ValueError, TypeError, UnicodeError, Base64Error) as exc:
        raise ValueError('分页游标无效，或对应的筛选条件已变化') from exc


def _scope_token(*parts):
    return sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]


def list_focuses(session, *, query='', limit=30, cursor=''):
    limit = _size(limit, 100)
    if len(query) > 200:
        raise ValueError('筛选文字最多 200 字')
    query = query.strip().lower()
    scope = _scope_token('focuses', query)
    position = _read_cursor(cursor, scope, 2)
    sort_name = func.lower(Watch.name)
    stmt = select(Watch.key, Watch.name, Watch.kind, Watch.enabled, sort_name.label('sort_name')).where(visible_watch())
    if query:
        stmt = stmt.where(sort_name.contains(query, autoescape=True))
    if position:
        stmt = stmt.where(tuple_(sort_name, Watch.key) > tuple_(*position))
    rows = all_rows(session, stmt.order_by(sort_name, Watch.key).limit(limit + 1), name='focus_read.focuses')
    more, rows = len(rows) > limit, rows[:limit]
    items = [dict(key=row.key, name=row.name, kind=row.kind, enabled=row.enabled,
                  path='/focus/' + quote(row.key, safe='')) for row in rows]
    next_cursor = _cursor_token(scope, [rows[-1].sort_name, rows[-1].key]) if more else None
    return dict(items=items, next_cursor=next_cursor)


def _filters(date_from, date_to, date_basis, country, country_role, category, branch):
    checked_dates(date_from, date_to, date_basis)
    if country_role not in COUNTRY_ROLES:
        raise ValueError('地理角色必须为 any、action、impact、mention 或 media')
    country = country.lower()
    if country and not re.fullmatch(r'[a-z]{2}', country):
        raise ValueError('国家或地区须为两位 ISO 代码')
    if len(category) > 200 or len(branch) > 200:
        raise ValueError('分类或焦点分支最多 200 字')
    return dict(date_from=date_from, date_to=date_to, date_basis=date_basis, country=country,
                country_role=country_role, category=category, branch=branch)


def _country_rows(session, role):
    """One relational query per role; keep media and entity mentions explicit."""
    if role in {'any', 'action', 'impact'}:
        roles = ('action', 'impact') if role == 'any' else (role,)
        return (select(EventPlace.event.label('event'), func.lower(EventPlace.iso).label('code'))
                .where(EventPlace.role.in_(roles), func.length(EventPlace.iso) == 2))
    if role == 'media':
        return (select(ArticleEventLink.event_key.label('event'), func.lower(Article.country).label('code'))
                .join(Article, Article.article_key == ArticleEventLink.article_key)
                .where(func.length(Article.country) == 2))
    if session.get_bind().dialect.name == 'postgresql':
        values = func.n2d_jsonb_or_null(GkgSource.attributes).op('->')('mention_iso')
        safe_values = case((func.jsonb_typeof(values) == 'array', values),
                           else_=func.n2d_jsonb_or_null('[]'))
        items = func.jsonb_array_elements_text(safe_values).table_valued('value')
    else:
        document = case((func.json_valid(GkgSource.attributes), GkgSource.attributes), else_='{}')
        values = func.json_extract(document, '$.mention_iso')
        safe_values = case((func.json_type(document, '$.mention_iso') == 'array', values), else_='[]')
        items = func.json_each(safe_values).table_valued('value')
    return (select(ArticleEventLink.event_key.label('event'), func.lower(items.c.value).label('code'))
            .select_from(ArticleEventLink).join(GkgSource, GkgSource.article_key == ArticleEventLink.article_key)
            .join(items, true()).where(func.length(items.c.value) == 2))


def _membership(focus):
    merged = select(EventState.event_key).where(EventState.event_key == RadarEntry.key,
                                               EventState.merged_into.is_not(None), EventState.merged_into != '')
    active = select(Watch.key).where(Watch.key == focus, visible_watch())
    return (select(RadarEntry.key.label('key')).select_from(WatchEvent)
            .join(RadarEntry, WatchEvent.event == RadarEntry.key)
            .where(WatchEvent.watch == focus, WatchEvent.choice != 'exclude', current_membership(),
                   active.exists(), ~merged.correlate(RadarEntry).exists()))


def _filtered(session, focus, filters):
    day = entry_day(session, filters['date_basis'])
    topic = func.coalesce(Event.topic, '')
    stmt = (_membership(focus).outerjoin(Event, Event.event_key == RadarEntry.key)
            .add_columns(func.coalesce(day, '').label('day'), topic.label('category'),
                         WatchEvent.branch.label('branch'), RadarEntry.seen.label('observed_at')))
    if filters['date_from']:
        stmt = stmt.where(day >= filters['date_from'])
    if filters['date_to']:
        stmt = stmt.where(day <= filters['date_to'])
    if filters['category']:
        stmt = stmt.where(topic == filters['category'])
    if filters['branch']:
        stmt = stmt.where(WatchEvent.branch == filters['branch'])
    if filters['country']:
        places = _country_rows(session, filters['country_role']).subquery()
        match = select(places.c.event).where(places.c.event == RadarEntry.key, places.c.code == filters['country'])
        stmt = stmt.where(match.correlate(RadarEntry).exists())
    return stmt


def _event_dict(row):
    return dict(key=row.key, title=row.title, observed_at=row.observed_at, category=row.category,
                branch=row.branch, day=row.day, membership=row.membership,
                reason=row.reason, path='/events/' + quote(row.key, safe=''))


def _event_columns(stmt):
    return stmt.add_columns(func.substr(RadarEntry.title, 1, 500).label('title'),
                            WatchEvent.choice.label('membership'), func.substr(WatchEvent.reason, 1, 500).label('reason'))


def focus_members(session, focus, *, date_from='', date_to='', date_basis='observed', country='',
                  country_role='any', category='', branch='', limit=30, cursor=''):
    watch = require_active(session, focus)
    limit = _size(limit, 100)
    filters = _filters(date_from, date_to, date_basis, country, country_role, category, branch)
    scope = _scope_token('members', focus, watch.version, filters)
    position = _read_cursor(cursor, scope, 3)
    clock = func.coalesce(time_value(RadarEntry.seen), -1).label('sort_time')
    stmt = _event_columns(_filtered(session, focus, filters)).add_columns(clock).subquery()
    query = select(*stmt.c)
    if position:
        day, seen, key = position
        try:
            seen = float(seen)
        except ValueError as exc:
            raise ValueError('分页游标时间无效') from exc
        if not isfinite(seen):
            raise ValueError('分页游标时间无效')
        query = query.where(or_(tuple_(stmt.c.day, stmt.c.sort_time) < tuple_(day, seen),
                                (stmt.c.day == day) & (stmt.c.sort_time == seen) & (stmt.c.key > key)))
    query = query.order_by(stmt.c.day.desc(), stmt.c.sort_time.desc(), stmt.c.key).limit(limit + 1)
    rows = all_rows(session, query, name='focus_read.members')
    more, rows = len(rows) > limit, rows[:limit]
    next_cursor = _cursor_token(scope, [rows[-1].day, str(rows[-1].sort_time), rows[-1].key]) if more else None
    return dict(focus=focus, date_basis=date_basis, date_basis_label=DATE_BASES[date_basis], filters=filters,
                items=[_event_dict(row) for row in rows], next_cursor=next_cursor)


def focus_overview(session, focus, *, date_from='', date_to='', date_basis='observed', country='',
                   country_role='any', category='', branch='', group_by='day', limit=40):
    watch = require_active(session, focus)
    limit = _size(limit, 100)
    if group_by not in {'day', 'country', 'category', 'branch'}:
        raise ValueError('分组依据必须为 day、country、category 或 branch')
    filters = _filters(date_from, date_to, date_basis, country, country_role, category, branch)
    members = _filtered(session, focus, filters).subquery()
    total = scalar(session, select(func.count()).select_from(members), name='focus_read.total')
    if group_by == 'country':
        places = _country_rows(session, country_role).subquery()
        dtype = places.c.code
        count = func.count(func.distinct(members.c.key))
        groups = select(dtype.label('key'), count.label('count')).select_from(members).join(places, places.c.event == members.c.key)
        if country:
            groups = groups.where(dtype == filters['country'])
    else:
        dtype = members.c[group_by]
        count = func.count()
        groups = select(dtype.label('key'), count.label('count')).select_from(members)
    order = (dtype.desc(),) if group_by == 'day' else (count.desc(), dtype)
    rows = all_rows(session, groups.group_by(dtype).order_by(*order).limit(limit + 1), name='focus_read.groups')
    labels = countries() if group_by == 'country' else {}
    empty_label = {'day': '日期待确认', 'category': '未分类', 'branch': '未分支'}.get(group_by, '')
    group_items = [dict(key=row.key, label=labels.get(row.key, row.key or empty_label), count=row.count) for row in rows[:limit]]
    result = dict(focus=focus, focus_name=watch.name, date_basis=date_basis, date_basis_label=DATE_BASES[date_basis],
                  filters=filters, group_by=group_by, total=total, groups=group_items, groups_truncated=len(rows) > limit)
    if group_by == 'country':
        result['country_role_label'] = COUNTRY_ROLES[country_role]
    return result


def _article_path(article, revision):
    return '/articles/' + quote(article, safe='') + (f'?revision={revision}' if revision else '')


def _require_event(session, focus, event):
    require_active(session, focus)
    if not exists(session, _membership(focus).where(RadarEntry.key == event), name='focus_read.event_exists'):
        raise KeyError('焦点未收录该事件')


def _source_page(session, focus, event, limit, cursor):
    limit = _size(limit, 50)
    scope = _scope_token('sources', focus, event)
    position = _read_cursor(cursor, scope, 1)
    latest = (select(ArticleVersion.id).where(ArticleVersion.article_key == Article.article_key)
              .order_by(ArticleVersion.revision.desc()).limit(1).correlate(Article).scalar_subquery())
    links = select(ArticleEventLink.article_key).where(ArticleEventLink.event_key == event)
    stmt = (select(Article.article_key, func.substr(Article.title, 1, 500).label('title'),
                   Article.url, Article.provider, Article.source_domain, Article.language, Article.published_at,
                   Article.country.label('media_country'), GkgSource.record_id.label('gkg_record_id'),
                   ArticleVersion.revision, ArticleVersion.content_hash, ArticleVersion.observed_at, ArticleVersion.origin)
            .outerjoin(GkgSource, GkgSource.article_key == Article.article_key)
            .outerjoin(ArticleVersion, ArticleVersion.id == latest).where(Article.article_key.in_(links)))
    if position:
        stmt = stmt.where(Article.article_key > position[0])
    rows = all_rows(session, stmt.order_by(Article.article_key).limit(limit + 1), name='focus_read.sources')
    more, rows = len(rows) > limit, rows[:limit]
    items = [dict(key=row.article_key, title=row.title, url=source_url(row.url), provider=row.provider,
                  domain=row.source_domain, language=row.language, published_at=row.published_at,
                  media_country=row.media_country, gkg_record_id=row.gkg_record_id, origin=row.origin,
                  revision=row.revision, observed_at=row.observed_at, content_hash=row.content_hash,
                  path=_article_path(row.article_key, row.revision)) for row in rows]
    return items, _cursor_token(scope, [rows[-1].article_key]) if more else None


def _evidence_links(raw):
    links = []
    for source in (raw if isinstance(raw, list) else [])[:8]:
        if not isinstance(source, dict) or not isinstance(source.get('article_key'), str):
            continue
        article = source['article_key']
        revision = source.get('revision')
        links.append(dict(article=article, revision=revision, excerpt=str(source.get('excerpt') or '')[:600],
                          url=source_url(source.get('url')), path=_article_path(article, revision)))
    return links


def focus_event(session, focus, event, *, source_limit=20, source_cursor=''):
    require_active(session, focus)
    filters = _filters('', '', 'observed', '', 'any', '', '')
    stmt = _event_columns(_filtered(session, focus, filters)).where(RadarEntry.key == event)
    row = session.execute(stmt.add_columns(func.substr(Event.summary, 1, 1200).label('stored_summary'))).first()
    if row is None:
        raise KeyError('焦点未收录该事件')
    item = dict(_event_dict(row), stored_summary=row.stored_summary or '')
    moments = all_rows(session, select(EventMoment).where(EventMoment.event_key == event, EventMoment.deleted.is_(False))
                      .order_by(EventMoment.date_start.desc(), EventMoment.moment_key).limit(31), name='focus_read.moments')
    moments_truncated = len(moments) > 30
    timeline = [dict(key=moment.moment_key, title=moment.title[:500], time_kind=moment.time_kind,
                     date_start=moment.date_start, date_end=moment.date_end, reviewed=moment.reviewed,
                     certainty=moment.certainty, time_note=moment.time_note[:500], sources=_evidence_links(loads(moment.sources_json, [])))
                for moment in moments[:30]]
    places = all_rows(session, select(EventPlace).where(EventPlace.event == event)
                     .order_by(EventPlace.role, EventPlace.iso, EventPlace.key).limit(41), name='focus_read.places')
    place_items = [dict(code=place.iso, role=place.role, label=countries().get(place.iso, place.iso),
                        article=place.article, revision=place.revision, quote=place.quote[:600],
                        path=_article_path(place.article, place.revision)) for place in places[:40]]
    sources, next_cursor = _source_page(session, focus, event, source_limit, source_cursor)
    return dict(focus=focus, event=item, moments=timeline, moments_truncated=moments_truncated,
                countries=place_items, countries_truncated=len(places) > 40, sources=sources, next_source_cursor=next_cursor)


def focus_source(session, focus, event, article, *, revision=None, offset=0, limit=5000):
    _require_event(session, focus, event)
    _size(limit, 10000)
    if type(offset) is not int or offset < 0:
        raise ValueError('正文位置须为非负整数')
    if revision is not None and (type(revision) is not int or revision < 1):
        raise ValueError('报道版本须为正整数')
    attached = select(ArticleEventLink.id).where(ArticleEventLink.event_key == event, ArticleEventLink.article_key == article)
    if not exists(session, attached, name='focus_read.attachment'):
        raise KeyError('该报道未关联到焦点内的事件')
    version = select(ArticleVersion).where(ArticleVersion.article_key == article)
    if revision is not None:
        version = version.where(ArticleVersion.revision == revision)
    row = session.exec(version.order_by(ArticleVersion.revision.desc()).limit(1)).first()
    if row:
        document = loads(row.payload_json, {})
        document = source_document(document if isinstance(document, dict) else {})
        observed_at, content_hash, revision, origin = row.observed_at, row.content_hash, row.revision, row.origin
    elif revision is None:
        original = session.exec(select(Article).where(Article.article_key == article)).first()
        if original is None:
            raise KeyError('报道不存在')
        document = source_document(dict(snapshot(original), raw=raw_metadata(original)))
        observed_at, content_hash, origin = original.fetched_at, None, None
    else:
        raise KeyError('报道版本不存在')
    content = document.get('content')
    content = content if isinstance(content, str) else ''
    stop = offset + limit
    raw = document.get('raw')
    raw = raw if isinstance(raw, dict) else {}
    material_kind = document.get('material_kind') or raw.get('material_kind') or ''
    content_kind = document.get('content_kind') or raw.get('content_kind') or ''
    lead = (is_lead(document) or not content.strip() or document.get('requires_original_text') is True
            or raw.get('requires_original_text') is True or material_kind == 'discovery_metadata')
    return dict(focus=focus, event=event, key=article, revision=revision, content_hash=content_hash,
                title=str(document.get('title') or '')[:500], url=source_url(document.get('url')),
                provider=document.get('provider') or '', domain=document.get('source_domain') or '',
                language=document.get('language') or '', published_at=document.get('published_at') or '',
                observed_at=observed_at, origin=origin, material_kind=material_kind, content_kind=content_kind,
                lead_only=lead, path=_article_path(article, revision), text=content[offset:stop],
                offset=offset, chars_total=len(content), next_offset=stop if stop < len(content) else None)
