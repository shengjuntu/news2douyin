"""Build one bounded, sourced property graph from GDELT 2.0 evidence."""

from collections import defaultdict
from datetime import date, timedelta
import json
from urllib.parse import quote, urlsplit

from sqlalchemy import func, literal_column, union_all
from sqlmodel import select

from ..intake.official_models import OfficialEvent, OfficialMention
from ..search.dates import calendar_day
from ..selection.service import CAMEO_LABELS
from ..selection.source_content import source_url
from ..storage.models import Article, ArticleEventLink, EventState
from .focus_models import FocusLead
from .focus_read import _membership
from .graph_explorer import _node
from .models import RadarEntry
from .official_graph import _gkg, _key

EVENTS = 240
MEMBERS = 480
SOURCES = 1500
LEADS = 1200
DOCUMENTS = 8
NOTES_LIMIT = 65_536

_BASIS = {
    'provenance': '焦点材料保存的官方编号',
    'focus_lead': '当前焦点的官方事件线索',
    'shared_document': '焦点报道提及，事件归属待核',
}


def bounds(days, until):
    end = date.fromisoformat(calendar_day(until, 'Asia/Shanghai'))
    start = end - timedelta(days=min(days - 1, (end - date.min).days))
    return start.isoformat(), end.isoformat()


def _event_id(value):
    if isinstance(value, str) and value.isascii() and value.isdigit() and 1 <= len(value) <= 30:
        return value
    return ''


def _notes(value):
    if not value or len(value) > NOTES_LIMIT:
        return {}
    try:
        result = json.loads(value)
    except (ValueError, RecursionError):
        return {}
    return result if isinstance(result, dict) else {}


def has_linked_reports(session, watch):
    members = _membership(watch.key).subquery()
    return session.exec(select(ArticleEventLink.id)
                        .where(ArticleEventLink.event_key.in_(select(members.c.key))).limit(1)).first() is not None


def _members(session, watch, start, end):
    # Read neither radar payloads nor article bodies. Event time is checked on
    # the official records; this bound applies to the local membership sample.
    members = _membership(watch.key).subquery()
    rows = session.exec(select(RadarEntry.key)
        .where(RadarEntry.key.in_(select(members.c.key)))
        .order_by(((RadarEntry.seen >= start) & (RadarEntry.seen <= end + 'T23:59:59Z')).desc(),
                  RadarEntry.seen.desc(), RadarEntry.key).limit(MEMBERS + 1)).all()
    keys = rows[:MEMBERS]
    if not keys:
        return {}, set(), False
    records = session.exec(select(ArticleEventLink.event_key, Article.article_key, Article.url)
        .join(Article, Article.article_key == ArticleEventLink.article_key)
        .where(ArticleEventLink.event_key.in_(keys))
        .order_by(ArticleEventLink.event_key, ArticleEventLink.id)
        .limit(SOURCES + 1)).all()
    event_notes = {event: _notes(value) for event, value in session.exec(
        select(EventState.event_key, func.substr(EventState.source_notes_json, 1, NOTES_LIMIT + 1))
        .where(EventState.event_key.in_(keys)))}
    origins, urls = {}, set()
    for event, article, url in records[:SOURCES]:
        if len(url or '') <= 2048 and source_url(url):
            urls.add(url)
        note = event_notes.get(event, {}).get(article)
        ref = note.get('source_ref') if isinstance(note, dict) else None
        if isinstance(ref, dict) and ref.get('provider') == 'gdelt':
            ident = _event_id(ref.get('event_id'))
            if ident:
                origins[ident] = 'provenance'
    return origins, urls, len(rows) > MEMBERS or len(records) > SOURCES


def _roots(session, watch, start, end):
    origins, urls, capped = _members(session, watch, start, end)
    dates = (OfficialEvent.event_date >= start, OfficialEvent.event_date <= end)
    leads = session.exec(select(FocusLead.candidate, FocusLead.decision)
        .join(OfficialEvent, OfficialEvent.event_id == func.substr(FocusLead.candidate, 7)).where(
        *dates,
        FocusLead.watch == watch.key, FocusLead.scope_version == watch.version,
        FocusLead.candidate.startswith('gdelt:'),
    ).order_by(FocusLead.observed_at.desc(), FocusLead.key).limit(LEADS + 1)).all()
    excluded = set()
    for key, decision in leads[:LEADS]:
        ident = _event_id(key[6:])
        if not ident:
            continue
        if decision == 'exclude':
            excluded.add(ident)
        else:
            origins.setdefault(ident, 'focus_lead')
    official = []
    if origins:
        official = session.exec(select(OfficialEvent).where(OfficialEvent.event_id.in_(sorted(origins)), *dates)
            .order_by(OfficialEvent.event_date.desc(), OfficialEvent.last_seen.desc(), OfficialEvent.event_id)
            .limit(EVENTS + 1)).all()
    # Reverse lookup is exact on the preserved document identifier. The short
    # indexed prefix is just a seek key and never substitutes for equality.
    shared = []
    if urls and len(official) < EVENTS:
        prefix = func.substr(OfficialMention.doc_identifier, literal_column('1'), literal_column('240'))
        refs = select(OfficialMention.event_id).where(
            prefix.in_(sorted({url[:240] for url in urls})),
            OfficialMention.doc_identifier.in_(sorted(urls)),
        ).distinct().subquery()
        query = select(OfficialEvent).join(refs, refs.c.event_id == OfficialEvent.event_id).where(*dates)
        if origins or excluded:
            query = query.where(OfficialEvent.event_id.not_in(sorted(set(origins) | excluded)))
        shared = session.exec(query.order_by(OfficialEvent.event_date.desc(), OfficialEvent.last_seen.desc(),
            OfficialEvent.event_id).limit(EVENTS + 1 - len(official))).all()
        origins.update((item.event_id, 'shared_document') for item in shared)
    roots = (official + shared)[:EVENTS]
    return roots, origins, capped or len(leads) > LEADS or len(official) + len(shared) > EVENTS


def _documents(session, events):
    """One indexed top-k seek per event in one SQL round trip per batch."""
    result = defaultdict(list)
    for i in range(0, len(events), 24):
        selects = []
        for event in events[i:i + 24]:
            sub = (select(OfficialMention.key).where(OfficialMention.event_id == event.event_id)
                .order_by(OfficialMention.last_seen.desc(), OfficialMention.key.desc())
                .limit(DOCUMENTS + 1).subquery())
            selects.append(select(sub.c.key))
        keys = union_all(*selects).subquery()
        rows = session.exec(select(OfficialMention).join(keys, keys.c.key == OfficialMention.key)
            .order_by(OfficialMention.event_id, OfficialMention.last_seen.desc(), OfficialMention.key.desc())).all()
        for row in rows:
            result[row.event_id].append(row)
    return result


def build(session, watch, days, until):
    start, end = bounds(days, until)
    roots, origins, capped = _roots(session, watch, start, end)
    documents = _documents(session, roots)
    metadata = {}
    unique = {m.doc_identifier: m for rows in documents.values() for m in rows[:DOCUMENTS]}
    identifiers = list(unique.values())
    for i in range(0, len(identifiers), 128):
        metadata.update(_gkg(session, identifiers[i:i + 128]))
    nodes, edges = {}, {}

    def node(kind, key, title, url='', **properties):
        item = _node(kind, key, title, url, **properties)
        nodes.setdefault(item['id'], item)
        return item['id']

    def edge(source, target, kind, **properties):
        key = _key(source, kind, target)
        edges.setdefault(key, dict(id=key, source=source, target=target, kind=kind, directed=True,
                                   details={k: v for k, v in properties.items() if v is not None and v != ''}))

    for event in roots:
        code = CAMEO_LABELS.get(event.event_root_code, event.event_code or '行动')
        title = ' · '.join(part for part in (event.actor1_name, code, event.actor2_name) if part)
        event_node = node('SourceEvent', 'gdelt/' + event.event_id, title,
            '/radar?source=gdelt&event=' + quote(event.event_id, safe=''),
            external_id=event.event_id, event_date=event.event_date, code=event.event_code,
            country=event.action_country_iso2, quad_class=event.quad_class, goldstein=event.goldstein,
            basis=_BASIS[origins[event.event_id]], observed=event.added_at,
            feed=event.feed, batch=event.stamp, row=event.row_index)
        for role, name, country in (('CODED_ACTOR1', event.actor1_name, event.actor1_country),
                                     ('CODED_ACTOR2', event.actor2_name, event.actor2_country)):
            if name or country:
                actor = node('GdeltActor', _key(name, country), name or country, country=country)
                edge(event_node, actor, role)
        if event.action_place or event.action_country_iso2:
            place = node('GdeltPlace', _key(event.action_place, event.action_country_iso2,
                event.latitude, event.longitude), event.action_place or event.action_country_iso2,
                country=event.action_country_iso2, latitude=event.latitude, longitude=event.longitude)
            edge(event_node, place, 'CODED_ACTION_AT')
        rows = documents[event.event_id]
        capped |= len(rows) > DOCUMENTS
        for mention in rows[:DOCUMENTS]:
            identifier = mention.doc_identifier
            record, feed, batch, row, more = metadata.get(identifier, ((None,) * 5, []))[0]
            url = source_url(identifier) or ''
            title = (mention.source_name or mention.domain or (urlsplit(url).hostname if url else None)
                     or identifier[:180])
            doc = node('GdeltDocument', _key(identifier), title, url, identifier=identifier[:1000],
                       domain=mention.domain, gkg_record=record, feed=feed, batch=batch, row=row)
            edge(event_node, doc, 'MENTIONED_IN', mention_key=mention.key, feed=mention.last_feed,
                 batch=mention.last_stamp, row=mention.last_row_index, seen=mention.last_seen,
                 sentence=mention.sentence_id, confidence=mention.confidence,
                 first_seen=mention.first_seen, first_feed=mention.first_feed,
                 first_batch=mention.first_stamp, first_row=mention.first_row_index)
            capped |= bool(more)
            for subject_key, kind, name in metadata.get(identifier, (None, []))[1]:
                entity = node('Person' if kind == 'person' else 'Organization', subject_key, name,
                              identity='GKG 名称，身份待核')
                edge(doc, entity, 'GKG_MENTIONS', gkg_record=record, feed=feed, batch=batch, row=row)
    counts = dict(events=len(roots), reports=len(unique), gkg_reports=len(metadata),
                  people=sum(n['kind'] == 'Person' for n in nodes.values()),
                  organizations=sum(n['kind'] == 'Organization' for n in nodes.values()))
    return dict(nodes=list(nodes.values()), edges=list(edges.values()), counts=counts, truncated=capped,
                date_from=start, date_to=end)
