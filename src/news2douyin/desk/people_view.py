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

from sqlalchemy import and_, false, func, or_, tuple_
from sqlalchemy.orm import aliased
from sqlmodel import select

from ..extraction.models import MentionReview, TextMention
from ..extraction.schema import LABELS
from ..search.dates import calendar_day
from ..selection.source_content import source_url
from ..storage.models import Article, ArticleEventLink, ArticleVersion, EventState
from ..storage.queries import all_rows
from ..storage.sql import time_value
from .focus_lifecycle import require_active
from .focus_read import _membership, entry_day
from .models import RadarEntry
from .people_evidence import evidence_from_sources, time_order
from .people_coverage import coverage_values, extraction_summary, version_coverage_columns


EVENT_LIMIT = 120
SOURCES_PER_EVENT = 6
SOURCE_JSON_LIMIT = 196_608
ASSIGNMENT_NOTES_LIMIT = 65_536
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
    except (TypeError, ValueError, RecursionError):
        return None


def _window(days, until):
    if type(days) is not int or days not in DAY_OPTIONS:
        raise ValueError('人物材料窗口须为30、90或365天')
    end = date.fromisoformat(calendar_day(until, 'Etc/GMT-8'))
    beginning = end - timedelta(days=min(days - 1, (end - date.min).days))
    return beginning.isoformat(), end.isoformat()


def _editorial_notes(session, events, stats):
    """Only legacy projections need the current event's editorial version pins."""
    if not events:
        return {}
    rows = all_rows(session, select(EventState.event_key,
        func.substr(EventState.source_notes_json, 1, ASSIGNMENT_NOTES_LIMIT + 1).label('notes'))
        .where(EventState.event_key.in_(events)).limit(len(events)), name='focus_people.editorial_pins')
    notes = {}
    for row in rows:
        oversized = len(row.notes or '') > ASSIGNMENT_NOTES_LIMIT
        stats['source_caps'] |= oversized
        # Unknown/malformed notes must not be treated as proof that no pin exists.
        notes[row.event_key] = None if oversized else _json(row.notes)
    return notes


def _material_sources(session, focus, beginning, end):
    day = entry_day(session, 'observed')
    members = _membership(focus).subquery()
    # Give linked current extraction evidence priority over empty recent
    # events. This is one indexed SQL query; material payloads remain bounded.
    linked = ArticleEventLink
    latest = (select(ArticleVersion.id).where(ArticleVersion.article_key == linked.article_key)
              .order_by(ArticleVersion.revision.desc()).limit(1).correlate(linked).scalar_subquery())
    extracted = (select(linked.id).join(TextMention, TextMention.version_id == latest)
                 .outerjoin(MentionReview, MentionReview.id == TextMention.id)
                 .where(linked.event_key == RadarEntry.key,
                        or_(MentionReview.id.is_(None), MentionReview.decision != 'rejected'))
                 .correlate(RadarEntry).exists())
    recent = (select(RadarEntry.key.label('event_key'), RadarEntry.seen.label('seen'),
                     extracted.label('has_actions'))
        .where(RadarEntry.key.in_(select(members.c.key)), day >= beginning, day <= end)
        .order_by(extracted.desc(), time_value(RadarEntry.seen).desc(), RadarEntry.key)
        .limit(EVENT_LIMIT + 1).subquery())
    # Sort only keys/clocks before fetching bounded JSON snippets. Offset
    # timestamps compare by their instant, not their string spelling.
    query = (select(RadarEntry.key,
        func.substr(RadarEntry.title, 1, 500).label('title'), RadarEntry.seen,
        func.substr(RadarEntry.sources, 1, SOURCE_JSON_LIMIT + 1).label('sources'))
        .join(recent, recent.c.event_key == RadarEntry.key)
        .order_by(recent.c.has_actions.desc(), time_value(recent.c.seen).desc(), RadarEntry.key))
    rows = all_rows(session, query, name='focus_people.admitted_material')
    stats = dict(sampled_events=min(len(rows), EVENT_LIMIT), event_limit=EVENT_LIMIT,
                 events_truncated=len(rows) > EVENT_LIMIT, source_limit=SOURCES_PER_EVENT,
                 oversized_sources=0, invalid_sources=0, stale_sources=0, source_caps=False,
                 sampled_sources=0, legacy_sources=0, recovered_legacy_sources=0,
                 assignment_recovered_sources=0, ambiguous_legacy_sources=0,
                 missing_source_versions=0, checked_sources=0, removed_source_links=0, mention_limit=MENTION_LIMIT,
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
            stats['sampled_sources'] += 1
            if not isinstance(source, dict):
                stats['invalid_sources'] += 1
                continue
            article = source.get('key', source.get('article_key', source.get('article', '')))
            legacy = 'revision' not in source
            if legacy:
                stats['legacy_sources'] += 1
            revision = source.get('revision')
            version_id = source.get('version_id')
            content_hash = source.get('content_hash')
            minimum_id = 1 if legacy else 0
            if (not isinstance(article, str) or not article.strip() or len(article) > 160
                    or any(source[name] != article for name in ('key', 'article_key', 'article') if name in source)
                    or not legacy and (type(revision) is not int or revision < 1)
                    or version_id is not None and (type(version_id) is not int or version_id < minimum_id)
                    or content_hash is not None and not isinstance(content_hash, str)):
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
    hash_only = {source['article_key'] for source in sources if 'revision' not in source
                 and not source.get('version_id') and source.get('content_hash')}
    repeated_hash = false()
    if hash_only:
        older = aliased(ArticleVersion)
        repeated_hash = and_(ArticleVersion.article_key.in_(hash_only),
            select(older.id).where(older.article_key == ArticleVersion.article_key,
                older.content_hash == ArticleVersion.content_hash, older.revision < ArticleVersion.revision)
                .correlate(ArticleVersion).exists())
    legacy_events = {source['event_key'] for source in sources if 'revision' not in source}
    editorial_notes = _editorial_notes(session, legacy_events, stats)
    current_metadata = (func.substr(Article.title, 1, 2000).label('current_title'),
                        func.substr(Article.url, 1, 4096).label('current_url'),
                        func.substr(Article.published_at, 1, 64).label('current_publication')) if legacy_events else ()
    versions = all_rows(session, select(ArticleEventLink.event_key, ArticleEventLink.article_key,
        ArticleVersion.revision, ArticleVersion.id, ArticleVersion.content_hash, ArticleVersion.origin,
        repeated_hash.label('repeated_hash'), *current_metadata, *version_coverage_columns(ArticleVersion.id))
        .select_from(ArticleEventLink)
        .outerjoin(Article, Article.article_key == ArticleEventLink.article_key)
        .outerjoin(latest, latest.c.article == Article.article_key)
        .outerjoin(ArticleVersion, and_(ArticleVersion.article_key == latest.c.article,
                                       ArticleVersion.revision == latest.c.revision))
        .where(tuple_(ArticleEventLink.event_key, ArticleEventLink.article_key).in_(pairs))
        .distinct().limit(len(pairs) + 1), name='focus_people.current_source_versions')
    live_pairs = {(row.event_key, row.article_key) for row in versions}
    by_article = {row.article_key: row for row in versions if row.id is not None}
    checked = {key: dict(revision=row.revision, version_id=row.id, **coverage_values(row))
               for key, row in by_article.items()}
    valid = []
    for source in sources:
        if (source['event_key'], source['article_key']) not in live_pairs:
            stats['removed_source_links'] += 1
            continue
        row = by_article.get(source['article_key'])
        if row is None:
            stats['missing_source_versions'] += 1
            continue
        # Only a missing field marks an old-format source. Explicit stale pins
        # are never advanced. Link, ID and any stored content hash must agree.
        version_id = source.get('version_id')
        content_hash = source.get('content_hash')
        if (source.get('revision', row.revision) != row.revision
                or version_id and version_id != row.id or content_hash and content_hash != row.content_hash):
            stats['stale_sources'] += 1
            continue
        if 'revision' not in source:
            # A migration baseline is a snapshot taken later, not evidence that
            # the source originally referenced this text. Repeated content hashes
            # (e.g. A -> B -> A) likewise cannot identify a revision by themselves.
            first_original = row.revision == 1 and row.origin not in ('legacy_baseline', 'intake_history')
            own_pin = bool(version_id or content_hash and not row.repeated_hash)
            notes = editorial_notes.get(source['event_key'], {})
            if not isinstance(notes, dict):
                stats['ambiguous_legacy_sources'] += 1
                continue
            article_key = source['article_key']
            if article_key in notes:
                note = notes[article_key]
                if (not isinstance(note, dict) or type(note.get('revision')) is not int or note['revision'] < 1
                        or 'content_hash' in note and not isinstance(note['content_hash'], str)):
                    stats['ambiguous_legacy_sources'] += 1
                    continue
                if note['revision'] != row.revision or note.get('content_hash') and note['content_hash'] != row.content_hash:
                    stats['stale_sources'] += 1
                    continue
                if not own_pin:
                    # This current editorial assignment may post-date the radar
                    # snapshot. Display the linked article's current metadata.
                    source.update(title=row.current_title, url=row.current_url, when='',
                                  time_kind='', published_at=row.current_publication,
                                  revision_basis='current_event_assignment')
                    stats['assignment_recovered_sources'] += 1
            elif not own_pin and not first_original:
                stats['ambiguous_legacy_sources'] += 1
                continue
            source['revision'] = row.revision
            stats['recovered_legacy_sources'] += 1
        source['version_id'] = row.id
        valid.append(source)
    stats['checked_sources'] = len(valid)
    return valid, checked, stats


def _materials(session, focus, beginning, end):
    valid, checked, stats = _material_sources(session, focus, beginning, end)
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
    summary = extraction_summary(sources, checked, projected)
    summary['examples_without_person'] = [
        dict(title=example['title'] or example['article_key'], source_path=_source_path(example))
        for example in projected['examples_without_person']]
    coverage['extraction'] = summary
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
    stats = coverage['extraction']
    if not stats['versions']:
        message = '本次检查未取得可核定版本的来源，暂无可追溯的人物动作。'
    elif stats['statements_without_person']:
        message = f"已检查的陈述中有 {stats['statements_without_person']} 条未提供可核实的人物，暂无可展示的人物动作。"
    elif stats['rejected_only_versions']:
        message = '已核对的版本中有陈述被全部驳回，暂无其他可展示的人物动作。'
    elif stats['completed_empty']:
        message = f"其中 {stats['completed_empty']} 个版本已完成抽取，未产出陈述，当前没有可展示的人物动作。"
    elif stats['failed'] or stats['retrying']:
        message = '部分来源的陈述抽取失败或等待重试，当前未取得可展示的人物动作。'
    elif stats['queued'] or stats['running'] or stats['unqueued']:
        message = '部分来源尚待抽取或正在抽取，当前未取得可展示的人物动作。'
    elif stats['metadata_only']:
        message = '已核对的来源中有仅含元数据的线索，当前未取得可展示的人物动作。'
    else:
        message = '已检查的当前版本材料中未找到可追溯的人物动作。'
    if coverage.get('truncated'):
        return message + '所选窗口仍有材料未检查。'
    return message


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
        latest = max((row['time'] for row in values.values()), key=time_order, default='')
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
        rows = sorted(activities[selected_key].values(), key=lambda row: (time_order(row['time']), row['key'], row['role']), reverse=True)
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
                if time_order(row['time']) > time_order(current_row['time']):
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
