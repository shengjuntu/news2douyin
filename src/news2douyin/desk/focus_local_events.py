"""Contribute admitted events and collection leads to a focus property graph."""

from collections import defaultdict
from datetime import date
from urllib.parse import quote

from sqlalchemy import func
from sqlmodel import select

from ..candidates.models import SourceReport
from ..candidates.service import REPORT_COLUMNS, _report_candidate
from ..digest.calendar import day_bounds_utc
from ..selection.source_content import source_url
from ..storage.models import Article, ArticleEventLink
from ..storage.sql import time_value
from .focus_leads import _Matcher, _recalled
from .focus_models import FocusDefinition, FocusLead
from .focus_read import _membership, entry_day
from .models import RadarEntry
from .people_evidence import evidence_from_sources
from .people_view import _materials

LOCAL_LIMIT = 240
LEAD_LIMIT = 120
HISTORICAL_REPORT_LIMIT = 5000
ARTICLE_LINKS = 8


def _events(session, watch, start, end):
    membership = _membership(watch).subquery()
    day = entry_day(session)
    return session.exec(select(RadarEntry.key, func.substr(RadarEntry.title, 1, 500).label('title'),
        RadarEntry.seen, day.label('day')).where(RadarEntry.key.in_(select(membership.c.key)),
        day >= start, day <= end).order_by(time_value(RadarEntry.seen).desc(), RadarEntry.key)
        .limit(LOCAL_LIMIT + 1)).all()


def _reports(session, keys):
    # Deduplicate repeated link rows before bounding each event. Do not load
    # article bodies or version snapshots while building the display graph.
    pairs = (select(ArticleEventLink.event_key, ArticleEventLink.article_key)
        .join(Article, Article.article_key == ArticleEventLink.article_key)
        .where(ArticleEventLink.event_key.in_(keys)).distinct().subquery())
    order = func.row_number().over(partition_by=pairs.c.event_key, order_by=pairs.c.article_key)
    ranked = select(pairs, order.label('position')).subquery()
    return session.exec(select(ranked.c.event_key, ranked.c.position, Article.article_key, Article.provider,
        func.substr(Article.title, 1, 500).label('title'), Article.url, Article.source_domain,
        Article.published_at, Article.language).join(Article, Article.article_key == ranked.c.article_key)
        .where(ranked.c.position <= ARTICLE_LINKS + 1)
        .order_by(ranked.c.event_key, ranked.c.position)).all()


def _mentions(session, watch, start, end, articles, node, edge):
    if not articles:
        return False
    sources, checked, coverage = _materials(session, watch, start, end)
    usable = [source for source in sources if (source['event_key'], source['article_key']) in articles]
    evidence = evidence_from_sources(usable, checked, require_person=False)
    entities = {item['key']: item for item in evidence['entities']}
    for action in evidence['actions']:
        article = 'Article:' + action['article_key']
        cite = action['source_path'] + '#m-' + quote(action['mention_id'], safe='') if action['mention_id'] else action['source_path']
        for arg in action['arguments']:
            entity = entities[arg['entity_key']]
            target = node(entity['kind'], entity['key'], entity['name'], identity=entity['identity_label'])
            edge(article, target, 'EXTRACTED_MENTION', revision=action['revision'],
                 quote=action['quote'], evidence_path=cite, review=action['review'], status=action['status'])
    return evidence['limits']['truncated'] or any(coverage[key] for key in
        ('events_truncated', 'source_caps', 'mentions_truncated', 'oversized_sources', 'oversized_mentions'))


def add_local_events(session, watch, start, end, node, edge, official_origins, official_ids):
    rows = _events(session, watch.key, start, end)
    keys = [row.key for row in rows[:LOCAL_LIMIT]]
    local_by_url = defaultdict(set)
    articles = set()
    capped = len(rows) > LOCAL_LIMIT
    for row in rows[:LOCAL_LIMIT]:
        event = node('Event', row.key, row.title, '/events/' + quote(row.key, safe=''),
                     seen=row.seen, material_date=row.day, basis='已归入当前焦点')
        for ident in sorted(official_origins.get(row.key, set()) & official_ids):
            edge(event, 'SourceEvent:gdelt/' + ident, 'HAS_OFFICIAL_SOURCE', basis='焦点材料保存的官方编号')
    for item in _reports(session, keys) if keys else ():
        if item.position > ARTICLE_LINKS:
            capped = True
            continue
        url = source_url(item.url)
        article = node('Article', item.article_key, item.title or item.source_domain, '/articles/' +
                       quote(item.article_key, safe=''), provider=item.provider or 'unknown',
                       source_url=url, domain=item.source_domain, published_at=item.published_at,
                       language=item.language)
        edge('Event:' + item.event_key, article, 'CITES')
        articles.add((item.event_key, item.article_key))
        if url:
            local_by_url[url].add(article)
    capped |= _mentions(session, watch.key, start, end, articles, node, edge)
    return local_by_url, capped


def _catalogue_matches(session, watch, low, high):
    """Match older connector observations with the same rules as live Focus intake."""
    definition = session.get(FocusDefinition, watch.key)
    matcher = _Matcher([(watch, definition)])
    query = (select(*REPORT_COLUMNS).where(SourceReport.observed_at >= low,
        SourceReport.observed_at < high, SourceReport.provider.not_in(('gdelt', 'gkg', 'newsnow', 'mock')))
        .order_by(SourceReport.observed_at.desc(), SourceReport.key.desc()).limit(HISTORICAL_REPORT_LIMIT + 1))
    remaining = HISTORICAL_REPORT_LIMIT
    for batch in session.exec(query.execution_options(yield_per=128)).partitions(128):
        candidates = [lead for row in batch[:remaining] for lead in _recalled(matcher, _report_candidate(row))]
        remaining -= len(batch)
        known = set(session.exec(select(FocusLead.key).where(FocusLead.key.in_(
            [lead.key for lead in candidates])))) if candidates else set()
        # A stored lead, including an exclusion, remains authoritative. This
        # also fences repeated batches of the exact same title and excerpt.
        yield [lead for lead in candidates if lead.key not in known], remaining < 0


def add_report_leads(session, watch, start, end, linked_urls, node):
    low = day_bounds_utc(date.fromisoformat(start))[0].isoformat(timespec='seconds').replace('+00:00', 'Z')
    high = day_bounds_utc(date.fromisoformat(end))[1].isoformat(timespec='seconds').replace('+00:00', 'Z')
    query = select(FocusLead.key, func.substr(FocusLead.title, 1, 500).label('title'), FocusLead.url,
                   FocusLead.provider, FocusLead.language, FocusLead.observed_at, FocusLead.candidate).where(
        FocusLead.watch == watch.key, FocusLead.scope_version == watch.version, FocusLead.decision != 'exclude',
        FocusLead.observed_at >= low, FocusLead.observed_at < high, ~FocusLead.candidate.startswith('gdelt:'))
    saved = session.exec(query.order_by(FocusLead.observed_at.desc(), FocusLead.key).limit(LEAD_LIMIT * 4 + 1)).all()
    seen = set()

    def add(rows, basis):
        for item in rows:
            url = source_url(item.url)
            if not url or url in linked_urls or url in seen:
                continue
            if len(seen) >= LEAD_LIMIT:
                return False
            seen.add(url)
            node('ArticleLead', item.key, item.title, url, provider=item.provider or 'unknown',
                 language=item.language, seen=item.observed_at, basis=basis, candidate=item.candidate)
        return True

    if not add(saved[:LEAD_LIMIT * 4], '当前焦点匹配的报道线索'):
        return True
    capped = len(saved) > LEAD_LIMIT * 4
    if len(seen) >= LEAD_LIMIT:
        return True
    for candidates, limit_hit in _catalogue_matches(session, watch, low, high):
        if not add(candidates, '历史采集候选按当前焦点匹配，待核'):
            return True
        capped |= limit_hit
    return capped
