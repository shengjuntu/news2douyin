"""Bounded, read-only people/activity view over current admitted focus material.

The material window uses RadarEntry.seen in Beijing calendar days. It is not an
as-of reconstruction or an assignment of event-level dates to every person.
Identity and literal relationship projection are shared with people_evidence.
"""
from collections import defaultdict
from datetime import date, timedelta
import json
import re
from urllib.parse import quote

from sqlalchemy import and_, func, or_, tuple_
from sqlmodel import select

from ..extraction.models import MentionReview, TextMention
from ..extraction.schema import LABELS
from ..search.dates import calendar_day
from ..selection.source_content import source_url
from ..storage.models import ArticleEventLink, ArticleVersion
from ..storage.queries import all_rows
from ..storage.sql import time_value
from .focus_lifecycle import require_active
from .focus_read import _membership, entry_day
from .models import RadarEntry
from .people_evidence import evidence_from_sources


EVENT_LIMIT = 120
SOURCES_PER_EVENT = 6
SOURCE_JSON_LIMIT = 196_608
MENTION_LIMIT = 800
MENTION_JSON_LIMIT = 24_576
ACTIONS_PER_SOURCE = 24
PEOPLE_LIMIT = 60
ACTIVITY_LIMIT = 40
ORGANIZATION_LIMIT = 40
DAY_OPTIONS = (30, 90, 365)


def _json(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


def _window(days, until):
    if type(days) is not int or days not in DAY_OPTIONS:
        raise ValueError('人物材料窗口须为30、90或365天')
    end = date.fromisoformat(calendar_day(until, 'Etc/GMT-8'))
    beginning = end - timedelta(days=min(days - 1, (end - date.min).days))
    return beginning.isoformat(), end.isoformat()


def _materials(session, focus, beginning, end):
    day = entry_day(session, 'observed')
    members = _membership(focus).subquery()
    recent = (select(RadarEntry.key.label('event_key'), RadarEntry.seen.label('seen'))
        .where(RadarEntry.key.in_(select(members.c.key)), day >= beginning, day <= end)
        .order_by(time_value(RadarEntry.seen).desc(), RadarEntry.key)
        .limit(EVENT_LIMIT + 1).subquery())
    # Sort only keys/clocks before fetching bounded JSON snippets. Offset
    # timestamps compare by their instant, not their string spelling.
    query = (select(RadarEntry.key,
        func.substr(RadarEntry.title, 1, 500).label('title'), RadarEntry.seen,
        func.substr(RadarEntry.sources, 1, SOURCE_JSON_LIMIT + 1).label('sources'))
        .join(recent, recent.c.event_key == RadarEntry.key)
        .order_by(time_value(recent.c.seen).desc(), RadarEntry.key))
    rows = all_rows(session, query, name='focus_people.admitted_material')
    stats = dict(sampled_events=min(len(rows), EVENT_LIMIT), event_limit=EVENT_LIMIT,
                 events_truncated=len(rows) > EVENT_LIMIT, source_limit=SOURCES_PER_EVENT,
                 oversized_sources=0, invalid_sources=0, stale_sources=0, source_caps=False,
                 checked_sources=0, removed_source_links=0, mention_limit=MENTION_LIMIT,
                 mentions_truncated=False, oversized_mentions=0)
    sources = []
    for row in rows[:EVENT_LIMIT]:
        if len(row.sources or '') > SOURCE_JSON_LIMIT:
            stats['oversized_sources'] += 1
            continue
        values = _json(row.sources)
        if not isinstance(values, list):
            stats['invalid_sources'] += 1
            continue
        stats['source_caps'] |= len(values) > SOURCES_PER_EVENT
        for source in values[:SOURCES_PER_EVENT]:
            if not isinstance(source, dict):
                stats['invalid_sources'] += 1
                continue
            article = source.get('key', source.get('article_key', source.get('article', '')))
            revision = source.get('revision')
            if (not isinstance(article, str) or not article or len(article) > 160
                    or type(revision) is not int or revision < 1):
                stats['invalid_sources'] += 1
                continue
            sources.append({**source, 'article_key': article, 'key': article,
                            'event_key': row.key, 'event_title': row.title,
                            'material_at': row.seen})
    keys = sorted({source['article_key'] for source in sources})
    stats['source_candidates'] = len(sources)
    if not keys:
        return [], {}, stats
    # Restrict the latest-revision aggregate to this already bounded set. Never
    # scan/group all ArticleVersion rows or load its large payload_json column.
    latest = (select(ArticleVersion.article_key.label('article'),
                     func.max(ArticleVersion.revision).label('revision'))
              .where(ArticleVersion.article_key.in_(keys))
              .group_by(ArticleVersion.article_key).subquery())
    pairs = sorted({(source['event_key'], source['article_key']) for source in sources})
    versions = all_rows(session, select(ArticleEventLink.event_key, ArticleVersion.article_key,
        ArticleVersion.revision, ArticleVersion.id).select_from(ArticleEventLink)
        .join(ArticleVersion, ArticleVersion.article_key == ArticleEventLink.article_key)
        .join(latest, and_(ArticleVersion.article_key == latest.c.article,
                          ArticleVersion.revision == latest.c.revision))
        .where(tuple_(ArticleEventLink.event_key, ArticleEventLink.article_key).in_(pairs))
        .distinct().limit(len(pairs) + 1),
        name='focus_people.current_source_versions')
    checked = {row.article_key: dict(revision=row.revision, version_id=row.id) for row in versions}
    live_pairs = {(row.event_key, row.article_key) for row in versions}
    valid = []
    for source in sources:
        if (source['event_key'], source['article_key']) not in live_pairs:
            stats['removed_source_links'] += 1
            continue
        version = checked.get(source['article_key'])
        if (not version or version['revision'] != source['revision']
                or source.get('version_id') not in (None, 0, version['version_id'])):
            stats['stale_sources'] += 1
            continue
        source['version_id'] = version['version_id']
        valid.append(source)
    stats['checked_sources'] = len(valid)
    ids = sorted({source['version_id'] for source in valid})
    if not ids:
        return [], checked, stats
    rows = all_rows(session, select(TextMention.id, TextMention.version_id,
        TextMention.article, TextMention.revision,
        func.substr(TextMention.data, 1, MENTION_JSON_LIMIT + 1).label('data'),
        MentionReview.decision).outerjoin(MentionReview, MentionReview.id == TextMention.id)
        .where(TextMention.version_id.in_(ids),
               or_(MentionReview.id.is_(None), MentionReview.decision != 'rejected'))
        .order_by(TextMention.version_id, TextMention.id).limit(MENTION_LIMIT + 1),
        name='focus_people.current_action_evidence')
    stats['mentions_truncated'] = len(rows) > MENTION_LIMIT
    actions = defaultdict(list)
    for row in rows[:MENTION_LIMIT]:
        if len(row.data or '') > MENTION_JSON_LIMIT:
            stats['oversized_mentions'] += 1
            continue
        action = _json(row.data)
        version = checked.get(row.article)
        if (not isinstance(action, dict) or not version or version['revision'] != row.revision
                or version['version_id'] != row.version_id):
            continue
        action.update(id=row.id, article=row.article, revision=row.revision,
                      decision=row.decision or 'pending', review=row.decision or 'pending')
        actions[row.version_id].append(action)
    for source in valid:
        values = actions[source['version_id']]
        stats['source_caps'] |= len(values) > ACTIONS_PER_SOURCE
        source['actions'] = values[:ACTIONS_PER_SOURCE]
        source['source_path'] = '/articles/'+quote(source['article_key'], safe='')+'?revision='+str(source['revision'])
        # Only an explicitly labelled publisher clock can be a publication
        # fallback. GKG discovery/ingestion clocks remain material metadata.
        if source.get('time_kind') != '发布' and not source.get('published_at'):
            source.pop('when', None)
    return valid, checked, stats


def _source_path(item):
    article = item.get('article_key', item.get('article', ''))
    revision = item.get('revision')
    if not isinstance(article, str) or not article or type(revision) is not int or revision < 1:
        return ''
    path = '/articles/'+quote(article, safe='')+'?revision='+str(revision)
    mention = item.get('mention_id')
    return path+'#m-'+quote(mention, safe='') if isinstance(mention, str) and mention else path


def _activity(action, argument):
    role = argument.get('role', '')
    return dict(key=action['key'], event_key=action.get('event_key', ''),
        event_keys=list(action.get('event_keys', [])), mention_id=action.get('mention_id', ''),
        evidence_key=action.get('evidence_key', action['key']),
        title=action.get('event_title', ''), time=action.get('time', ''),
        time_kind=action.get('time_kind', ''), role=role, role_label=LABELS.get(role, role),
        kind=action.get('kind', ''), kind_label=LABELS.get(action.get('kind', ''), action.get('kind', '')),
        status=action.get('status', ''), status_label=LABELS.get(action.get('status', ''), action.get('status', '')),
        participation=bool(argument.get('participation')), review=action.get('review', 'pending'),
        source_url=source_url(action.get('source_url', '')) or '', source_title=action.get('source_title', ''),
        article_key=action.get('article_key', ''), revision=action.get('revision'),
        source_path=_source_path(action), evidence=action.get('evidence') or action.get('quote', ''),
        event_path='/events/'+quote(action.get('event_key', ''), safe=''))


def read_focus_evidence(session, watch, days, until):
    """Load the same current, bounded material for the graph and activity timeline."""
    beginning, end = _window(days, until)
    key = watch if isinstance(watch, str) else watch.key
    with session.no_autoflush:
        current = require_active(session, key)
        sources, checked, coverage = _materials(session, current.key, beginning, end)
    projected = evidence_from_sources(sources, checked)
    limits = projected['limits']
    coverage.update(date_from=beginning, date_to=end, time_basis='material_observed',
                    time_basis_label='北京时间材料日期', evidence_limits=limits,
                    truncated=bool(coverage['events_truncated'] or coverage['source_caps']
                                   or coverage['mentions_truncated'] or coverage['oversized_sources']
                                   or coverage['oversized_mentions'] or limits['truncated']))
    return projected, coverage


def empty_reason(coverage):
    if not coverage['sampled_events']:
        return '这个材料窗口还没有已收录事件。'
    return '已收录材料尚无当前版本的可追溯人物动作；不会从名单或共现补造关系。'


def page(session, watch, *, person='', days=30, until=''):
    """Read current focus membership and pinned source identities; never mutate."""
    if (not isinstance(person, str) or len(person) > 160 or re.search(r'[\x00-\x1f\x7f]', person)):
        raise ValueError('人物编号无效')
    projected, coverage = read_focus_evidence(session, watch, days, until)
    entities = {entity['key']: entity for entity in projected['entities']}
    actions = {action['key']: action for action in projected['actions']}
    activities = defaultdict(dict)
    for action in actions.values():
        for argument in action.get('arguments', []):
            ident = argument.get('entity_key')
            if ident in entities and entities[ident].get('kind') == 'Person':
                activities[ident].setdefault((action['key'], argument.get('role', '')),
                                             _activity(action, argument))
    items = []
    for ident, values in activities.items():
        entity = entities[ident]
        latest = max((row['time'] for row in values.values()), default='')
        items.append(dict(key=ident, name=entity['name'], activity_count=len({row['key'] for row in values.values()}),
                          latest_at=latest, provisional=bool(entity.get('provisional', True)),
                          identity_label=entity.get('identity_label', '名称归组，身份待核')))
    items.sort(key=lambda item: (-item['activity_count'], item['name'].casefold(), item['key']))
    all_items = items
    people_truncated = len(items) > PEOPLE_LIMIT
    items = items[:PEOPLE_LIMIT]
    requested = person.strip()
    selected_key = requested or (items[0]['key'] if items else '')
    selected = None
    activity_truncated = organization_truncated = False
    selected_item = next((item for item in all_items if item['key'] == selected_key), None)
    if selected_item and requested and not any(item['key'] == requested for item in items):
        items = [*items[:PEOPLE_LIMIT - 1], selected_item]
    if selected_item:
        selected = dict(selected_item)
        rows = sorted(activities[selected_key].values(), key=lambda row: (row['time'], row['key'], row['role']), reverse=True)
        activity_truncated = len(rows) > ACTIVITY_LIMIT
        selected['activities'] = rows[:ACTIVITY_LIMIT]
        groups = {}
        for relation in projected['relations']:
            if relation.get('person_key') != selected_key:
                continue
            organization = entities.get(relation.get('organization_key'))
            if not organization or organization.get('kind') != 'Organization':
                continue
            ident = (organization['key'], relation.get('relation', ''), relation.get('status', ''), relation.get('role', ''))
            action = actions.get(relation.get('action_key'), {})
            fields = {**action, **relation}
            row = dict(key=organization['key'], name=organization['name'], relation=relation.get('relation', ''),
                provisional=bool(organization.get('provisional', True)),
                identity_label=organization.get('identity_label', '名称归组，身份待核'),
                relation_label=relation.get('label', ''), role=relation.get('role', ''),
                role_label=LABELS.get(relation.get('role', ''), relation.get('role', '')),
                time=relation.get('time', ''), time_kind=relation.get('time_kind', ''),
                event_key=relation.get('event_key', ''), event_title=relation.get('event_title', ''),
                event_path='/events/'+quote(relation.get('event_key', ''), safe=''),
                source_url=source_url(relation.get('source_url', '')) or '', source_title=relation.get('source_title', ''),
                source_path=_source_path(fields), revision=relation.get('revision'),
                evidence=relation.get('evidence', ''), status=relation.get('status', ''),
                mention_id=relation.get('mention_id', ''), evidence_key=relation.get('evidence_key', ''),
                event_keys=list(action.get('event_keys', [])),
                status_label=LABELS.get(relation.get('status', ''), relation.get('status', '')),
                review=relation.get('review', 'pending'), formal_affiliation=bool(relation.get('formal_affiliation')),
                relation_basis=relation.get('relation_basis', ''), activity_count=1)
            if ident not in groups:
                groups[ident] = (row, {relation.get('action_key')})
            else:
                current_row, seen = groups[ident]
                seen.add(relation.get('action_key'))
                if row['time'] > current_row['time']:
                    groups[ident] = (row, seen)
        organizations = []
        for row, seen in groups.values():
            row['activity_count'] = len(seen)
            organizations.append(row)
        organizations.sort(key=lambda row: (-row['activity_count'], row['name'].casefold(), row['relation']))
        organization_truncated = len(organizations) > ORGANIZATION_LIMIT
        selected['organizations'] = organizations[:ORGANIZATION_LIMIT]
    truncated = coverage['truncated'] or people_truncated or activity_truncated or organization_truncated
    coverage.update(people_limit=PEOPLE_LIMIT, activities_limit=ACTIVITY_LIMIT,
                    organizations_limit=ORGANIZATION_LIMIT, people_truncated=people_truncated,
                    activities_truncated=activity_truncated, organizations_truncated=organization_truncated,
                    truncated=truncated)
    notice = ('所选人物不在这个材料窗口内，未自动切换人物。' if requested and not selected else
              '仅展示当前焦点已收录材料中的有据人物动作；名称归组不等于身份已核实。')
    return dict(items=items, selected=selected, requested_person=requested,
        selection_missing=bool(requested and not selected), days=days, until=coverage['date_to'],
        window=coverage, truncated=truncated, notice=notice, empty_reason=empty_reason(coverage) if not items else '')
