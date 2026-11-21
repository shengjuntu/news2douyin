"""Bounded retrieval of local, source-attributed leads for an investigation."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from itertools import zip_longest
from math import fsum, hypot, isfinite
import os
import re
import unicodedata

from sqlalchemy import func, or_, union, union_all
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from ..dedup.normalize import STOPWORDS, canonicalize_url
from ..desk.rules import ALIASES
from ..event_embeddings.documents import profile
from ..event_embeddings.runtime import vector_client
from ..event_embeddings.search import find_similar
from ..event_embeddings.models import EventEmbeddingJob, EventEmbeddingRecord
from ..intake.official_models import OfficialEvent
from ..intake.models import IntakeBatch
from ..llm.client.embedding import EmbeddingError, EmbeddingConfigurationError
from ..research.materials import LEAD_KINDS
from ..search.index import index_available
from ..search.service import _match
from ..selection.event_content import candidates as source_candidates, mention_sources
from ..selection.source_content import source_url, usable_title
from ..selection.source_models import SourcePreview
from ..storage.articles import identity_key
from ..storage.models import Article, ArticleEventLink, Event, EventMoment, EventState
from ..storage.sql import json_text
from ..storage.utils import loads
from .chronology import eligible, interval

MAX_QUERIES = 3
MAX_TERMS = 12
MAX_RESULTS = 12
MAX_EVIDENCE = 3
MAX_BODY = 3200
LOCAL_SCAN = 3000


def _queries(values):
    return list(dict.fromkeys(' '.join(value.split())[:200]
                              for value in values if isinstance(value, str) and value.strip()))[:MAX_QUERIES]


def _terms(queries):
    phrases = []
    for query in queries:
        phrases.extend(part.casefold().strip() for part in re.split(r'[,，;；、]+', query))
    words = [word for phrase in phrases for word in re.findall(r'[^\W_]+', phrase)]
    values = [value for value in (*phrases, *words) if len(value) >= (3 if value.isascii() else 2)
              and value not in STOPWORDS and any(char.isalpha() for char in value)]
    variants = [term for value in values for term in ALIASES.get(value, ())]
    return list(dict.fromkeys((*values, *variants)))[:MAX_TERMS]


def _encode(queries):
    try:
        client = vector_client()
        values = client.get_embedding(queries)
    except EmbeddingError:
        return '', []
    return profile(client), values


def _window(column, lower, upper):
    return (column >= lower) & (column < upper)


def _search(model, columns, terms, indexed):
    return or_(*(_match(model, columns, term, indexed, f'causal_term_{index}')
                 for index, term in enumerate(terms)))


def _day(value):
    try:
        return date.fromisoformat(value).isoformat() if len(value) == 10 else ''
    except (ValueError, TypeError):
        return ''


def _publication(value):
    if day := _day(value):
        return day
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        # A date with no time zone can retain day precision only.
        if moment.utcoffset() is None:
            return moment.date().isoformat()
        utc = moment.astimezone(timezone.utc).isoformat(timespec='seconds')
        return utc.replace('+00:00', 'Z')
    except (AttributeError, OverflowError, ValueError):
        return ''


def _quote(body, terms):
    if not body.strip():
        return ''
    spans = [match for term in terms if (match := re.search(re.escape(term), body, re.IGNORECASE))]
    if spans:
        # Slice the original string so the evidence can be located verbatim.
        first = min(spans, key=lambda match: match.start() // 180)
        start = max(0, first.start() - 65)
    else:
        start = 0
    return body[start:start + 360].strip()


def _source(url, title, quote, published, kind):
    if not source_url(url) or '\\' in url or not isinstance(title, str) or not usable_title(title):
        return None
    title = title[:500].strip()
    return dict(url=url, title=title, quote=quote or title, published_at=_publication(published),
                kind=kind if quote else 'local_headline')


def _article_fields():
    return (Article.article_key, Article.provider, Article.url, func.substr(Article.title, 1, 500),
            func.substr(Article.content, 1, MAX_BODY), Article.published_at,
            json_text(Article.raw_json, 'source_kind'), json_text(Article.raw_json, 'material_kind'))


def _article_source(row, terms):
    _, provider, url, title, body, published, source_kind, material_kind = row
    if source_kind == 'event_metadata':
        return None
    if provider == 'newsnow' or source_kind in LEAD_KINDS or material_kind == 'candidate_lead':
        return _source(url, title, '', published, 'local_headline')
    return _source(url, title, _quote(body or '', terms), published, 'local_article')


def _in_window(source, upper):
    published = source['published_at']
    # Day-only dates are resolved by chronology. Their timezone is not a
    # UTC midnight clock and the time within that day may be unknown.
    return not published or len(published) == 10 or published[:19] < upper[:19]


def _candidate(ref, label, sources, upper, *, event_key='', occurred_at='', similarity=0.):
    evidence, seen = [], set()
    for source in sources:
        if source and _in_window(source, upper):
            key = canonicalize_url(source['url'])
            if key not in seen:
                evidence.append(source)
                seen.add(key)
    evidence = evidence[:MAX_EVIDENCE]
    known = [item['published_at'] for item in evidence if item['published_at']]
    return dict(ref=ref, text=label[:850], event_key=event_key, occurred_at=occurred_at,
                published_at=min(known, default=''), sources=evidence, similarity=similarity)


def _curated(session, terms, indexed, lower, upper, exclude, similarities, *, event_keys=None, limit=MAX_RESULTS):
    title = func.coalesce(EventState.title_override, Event.event_title)
    prefix = (select(Event.event_key, func.substr(title, 1, 500), func.substr(Event.summary, 1, 850))
              .outerjoin(EventState, EventState.event_key == Event.event_key)
              .where(or_(EventState.merged_into.is_(None), EventState.merged_into == ''),
                     Event.event_key.not_in(exclude)))
    if event_keys is None:
        prefix = prefix.where(_window(Event.first_seen_at, lower, upper))
    else:
        prefix = prefix.where(Event.event_key.in_(event_keys))
    lexical = {}
    if terms and event_keys is None:
        statement = (prefix.where(_search(Event, (Event.event_title, Event.summary, Event.topic), terms, indexed))
                     .order_by(Event.first_seen_at.desc(), Event.event_key).limit(MAX_RESULTS))
        if not indexed:
            pool = select(Event.id).where(_window(Event.first_seen_at, lower, upper))
            statement = statement.where(Event.id.in_(pool.order_by(Event.first_seen_at.desc()).limit(LOCAL_SCAN)))
        lexical = {key: (title, summary) for key, title, summary in session.exec(statement)}
    keys = list(event_keys) if event_keys is not None else sorted(similarities, key=lambda key: -similarities[key])
    matches = {key: (title, summary) for key, title, summary in session.exec(
        prefix.where(Event.event_key.in_(keys))
    )} if keys else {}
    keys = list(dict.fromkeys((*keys, *lexical)))[:limit]
    matches.update(lexical)
    keys = [key for key in keys if key in matches]
    if not keys:
        return []

    # A small indexed link page per event avoids materializing a large workspace.
    links = []
    for key in keys:
        page = (select(ArticleEventLink.event_key, ArticleEventLink.article_key)
                .where(ArticleEventLink.event_key == key).order_by(ArticleEventLink.article_key).limit(8).subquery())
        links.append(select(page))
    joined = union_all(*links).subquery() if len(links) > 1 else links[0].subquery()
    sources = defaultdict(list)
    for event_key, *row in session.exec(select(joined.c.event_key, *_article_fields()).select_from(joined)
                                        .join(Article, Article.article_key == joined.c.article_key)
                                        .order_by(joined.c.event_key, Article.is_duplicate, Article.id)):
        sources[event_key].append(_article_source(row, terms))

    # Only a single exact, evidence-backed occurrence date can date the whole
    # event conservatively. Progress entries with reported dates stay separate.
    unique_moment = (
        select(func.min(EventMoment.moment_key).label('key'))
        .where(EventMoment.event_key.in_(keys), EventMoment.reviewed.is_(True),
               EventMoment.deleted.is_(False), EventMoment.time_kind == 'occurred')
        .group_by(EventMoment.event_key).having(func.count() == 1)
    )
    moments = {key: (start, end, certainty, raw) for key, start, end, certainty, raw in session.exec(
        select(EventMoment.event_key, EventMoment.date_start, EventMoment.date_end,
               EventMoment.certainty, EventMoment.sources_json).where(EventMoment.moment_key.in_(unique_moment))
    )}
    result = []
    for key in keys:
        occurred = ''
        pinned = []
        if key in moments:
            start, end, certainty, raw = moments[key]
            if certainty == 'exact' and end in ('', start):
                pins = loads(raw, [])
                for item in pins[:MAX_EVIDENCE] if isinstance(pins, list) else ():
                    if isinstance(item, dict) and isinstance(item.get('excerpt'), str) and item['excerpt'].strip():
                        pinned.append(_source(item.get('url'), item.get('title'), item['excerpt'][:360],
                                              item.get('published_at'), 'reviewed_moment'))
                occurred = _day(start) if any(pinned) else ''
        name, summary = matches[key]
        candidate = _candidate(key, '\n'.join(filter(None, (name, summary))), (*pinned, *sources[key]),
                               upper, event_key=key, occurred_at=occurred, similarity=similarities.get(key, 0.))
        if candidate['sources']:
            result.append(candidate)
    return result


# The program-controlled tree has a wider recall budget than the legacy
# interactive tool. All new behavior is opt-in through retrieve_for_tree.
TREE_RESULT_LIMIT = 32
TREE_POOL_LIMIT = 144
TREE_SLICES = 4
TREE_PER_SLICE = 24
TREE_EMBED_BATCH = 16
HISTORICAL_PUBLISHER_KIND = 'publisher_history'


@dataclass(frozen=True)
class TreePool:
    queries: list[str]
    terms: list[str]
    event_text: str
    lower: str
    upper: str
    excluded: set[str]
    candidates: list[dict]
    local: dict


@dataclass(frozen=True)
class TreeRanking:
    rows: list[dict]
    local: dict
    mode: str = ''
    history_eligible_count: int = 0
    history_ranked_count: int = 0


def _tree_terms(queries, event_text):
    """Broaden Chinese phrases without requiring a new tokenizer dependency."""
    base = _terms(queries)
    chunks = []
    for value in queries:
        for run in re.findall(r'[\u3400-\u9fff]{4,}', value):
            chunks.extend(run[i:i+4] for i in range(0, len(run)-3, 2))
    # Parent terms aid recall, but never displace the proposed mechanisms.
    return list(dict.fromkeys([*base, *chunks, *_terms([event_text[:500]])]))[:32]


def _tree_point(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _tree_bins(lower, upper):
    start,stop = _tree_point(lower),_tree_point(upper)
    if start >= stop:
        return []
    points = [start+(stop-start)*i/TREE_SLICES for i in range(TREE_SLICES+1)]
    return [(a.replace(tzinfo=None).isoformat(timespec='seconds'),
             b.replace(tzinfo=None).isoformat(timespec='seconds')) for a,b in zip(points, points[1:])]


def _local_coverage(session, lower, upper):
    """Probe for dated records independently of this step's search terms."""
    floor, ceiling = _tree_point(lower), _tree_point(upper)

    def has_dated_row(column, *conditions):
        days = (column >= lower[:10]) & (column <= upper)
        rows = session.exec(select(column).where(days, *conditions).order_by(column).limit(8)
                             .execution_options(n2d_query_name='causal.tree.local_dated_coverage')).all()
        if any(stamp[0] < ceiling and stamp[1] >= floor for value in rows if (stamp := interval(value))):
            return True
        return None if len(rows) == 8 else False

    dated_articles = has_dated_row(Article.published_at, Article.url != '')
    dated_moments = has_dated_row(EventMoment.date_start, EventMoment.reviewed.is_(True),
                                  EventMoment.deleted.is_(False), EventMoment.time_kind == 'occurred')
    last_day = (ceiling - timedelta(microseconds=1)).date().isoformat()
    official = session.exec(select(OfficialEvent.event_id).where(OfficialEvent.event_date >= lower[:10],
        OfficialEvent.event_date <= last_day).limit(1)
        .execution_options(n2d_query_name='causal.tree.local_official_coverage')).first() is not None
    archive = session.exec(select(IntakeBatch.key).where(IntakeBatch.state == 'archived',
        _window(IntakeBatch.stamp, floor.strftime('%Y%m%d%H%M%S'), ceiling.strftime('%Y%m%d%H%M%S'))).limit(1)
        .execution_options(n2d_query_name='causal.tree.local_archive_coverage')).first() is not None
    uncertain = archive or dated_articles is None or dated_moments is None
    return dict(status='covered' if dated_articles or dated_moments or official else 'unknown' if uncertain else 'gap',
                dated_articles_present=dated_articles, reviewed_occurrence_present=dated_moments,
                official_events_present=official, archive_receipt_present=archive)


def _tree_pool(session, terms, indexed, lower, upper, exclude, *, recall_articles=True):
    """Stratify by source/occurrence clocks, not merely newest ingestion IDs.

    Each slice reads at most 24 event keys, 24 article keys and 12 official
    leads. SQL LIMITs bound returned payloads; they are not a promise that a
    backend can evaluate every full-text predicate without an index scan.
    """
    event_keys, article_keys, official, official_ids = [], [], [], set()
    excluded_articles = select(ArticleEventLink.article_key).where(ArticleEventLink.event_key.in_(exclude))
    for first,last in _tree_bins(lower, upper):
        published = or_(_window(Article.published_at, first, last), Article.published_at == first[:10])
        unknown = (Article.published_at == '') & _window(Article.fetched_at, first, last)
        source_time = (select(ArticleEventLink.event_key).select_from(Article)
                       .join(ArticleEventLink, ArticleEventLink.article_key == Article.article_key).where(published))
        occurred = select(EventMoment.event_key).where(
            EventMoment.reviewed.is_(True), EventMoment.deleted.is_(False), EventMoment.time_kind == 'occurred',
            EventMoment.date_start >= first[:10], EventMoment.date_start <= last[:10])
        undated = (_window(Event.first_seen_at, first, last) & ~select(ArticleEventLink.event_key).join(
            Article, Article.article_key == ArticleEventLink.article_key).where(
                ArticleEventLink.event_key == Event.event_key, Article.published_at != '').correlate(Event).exists())
        # Range scans of publication/first-seen indexes can reject a historical
        # gap without probing article links for every event in the live corpus.
        temporal = union(source_time, occurred, select(Event.event_key).where(undated)).subquery()
        event_pool = select(temporal.c.event_key)
        if not indexed:
            event_pool = event_pool.order_by(temporal.c.event_key).limit(LOCAL_SCAN)
        statement = (select(Event.event_key).outerjoin(EventState, EventState.event_key == Event.event_key)
            .where(or_(EventState.merged_into.is_(None), EventState.merged_into == ''), Event.event_key.not_in(exclude),
                Event.event_key.in_(event_pool), _search(Event, (Event.event_title, Event.summary, Event.topic), terms, indexed))
            .order_by(Event.event_key).limit(TREE_PER_SLICE)
            .execution_options(n2d_query_name='causal.tree.event_recall'))
        event_keys.extend(session.exec(statement))
        if recall_articles:
            statement = (select(Article.article_key).where(or_(published, unknown), Article.is_duplicate.is_(False),
                Article.article_key.not_in(excluded_articles),
                _search(Article, (Article.title, Article.content), terms, indexed))
                .order_by(Article.published_at, Article.article_key).limit(TREE_PER_SLICE)
                .execution_options(n2d_query_name='causal.tree.article_recall'))
            if not indexed:
                pool = (select(Article.id).where(or_(published, unknown))
                    .order_by(Article.published_at, Article.article_key).limit(LOCAL_SCAN).correlate(None))
                statement = statement.where(Article.id.in_(pool))
            article_keys.extend(session.exec(statement))
        official.extend(_official(session, terms, first, last, recalled=official_ids))
    event_keys = list(dict.fromkeys(event_keys))
    curated = _curated(session, terms, indexed, lower, upper, sorted(exclude), {},
        event_keys=event_keys, limit=TREE_PER_SLICE*TREE_SLICES) if event_keys else []
    keys = list(dict.fromkeys(article_keys))
    recall_count = len(event_keys) + len(keys) + len(official_ids)
    articles = []
    if keys:
        for row in session.exec(select(*_article_fields()).where(Article.article_key.in_(keys))
                .execution_options(n2d_query_name='causal.tree.article_material')):
            source = _article_source(row, terms)
            if source:
                articles.append(_candidate('article:'+row[0], source['title']+'\n'+source['quote'], [source], upper))
    # Prefer one curated interpretation for a report. Still keep distinct
    # curated events even when they share reporting, as in the legacy path.
    urls = {canonicalize_url(item['sources'][0]['url']) for item in curated}
    found, refs, identical = [], set(exclude), set()
    for group in zip_longest(curated, articles, official):
        for item in group:
            if not item or item['ref'] in refs or not item['sources']:
                continue
            uri = canonicalize_url(item['sources'][0]['url'])
            fingerprint = (unicodedata.normalize('NFKC',' '.join(item['text'].split())).casefold(),uri)
            if fingerprint in identical:
                continue
            if not item['event_key'] and uri in urls:
                continue
            refs.add(item['ref']);urls.add(uri);identical.add(fingerprint);found.append(item)
            if len(found) >= TREE_POOL_LIMIT:
                return found, recall_count
    return found, recall_count


def _tree_unit(value):
    if not isinstance(value, list) or not value or any(type(x) not in (int,float) for x in value):
        raise EmbeddingError('本地候选向量格式无效')
    try:
        size = hypot(*value)
    except (ValueError, OverflowError):
        raise EmbeddingError('本地候选向量不是有效有限数值') from None
    if not size or not isfinite(size):
        raise EmbeddingError('本地候选向量不是有效有限数值')
    return [x/size for x in value]


def _tree_lexical(candidate, terms):
    text = (' '.join([candidate['text'], *(source['quote'] for source in candidate['sources'])])).casefold()
    return sum(term.casefold() in text for term in terms)/max(1,len(terms))


def _before_floor(stamp, floor):
    return stamp and stamp[0] < floor and stamp[1] <= floor


def _tree_eligible(candidate, floor, ceiling):
    if _before_floor(interval(candidate['occurred_at']), floor):
        return None
    sources = [source for source in candidate['sources']
               if not _before_floor(interval(source['published_at']), floor)]
    return eligible({**candidate, 'sources': sources}, None, ceiling)


def _dated_evidence(source, floor, ceiling):
    stamp = interval(source['published_at'])
    return bool(source['kind'] != 'local_headline' and source['quote'].strip() and stamp
                and floor <= stamp[0] < ceiling and stamp[1] <= ceiling)


def collect_tree_pool(engine, queries: list[str], *, lower: str, upper: str,
                      exclude: set[str], event_text: str = '') -> TreePool:
    """Collect a bounded local pool and retain diagnostics even for empty steps."""
    queries = _queries(queries)
    terms = _tree_terms(queries, event_text)
    local = dict(status='unknown', recall_count=0, pool_count=0, eligible_count=0,
                 dated_evidence_count=0, vector_ranked_count=0, keyword_indexed=False)
    candidates = []
    if queries and terms and _tree_bins(lower, upper):
        with Session(engine) as session:
            indexed = index_available(session)
            local.update(_local_coverage(session, lower, upper))
            recall_articles = True
            if session.bind.dialect.name == 'postgresql' and local['status'] == 'gap':
                # PostgreSQL has a publication index, while the undated
                # fallback clock need only be checked once for an empty era.
                dated = session.exec(select(Article.id).where(_window(Article.published_at, lower[:10], upper))
                    .limit(1).execution_options(n2d_query_name='causal.tree.historical_article_probe')).first()
                recall_articles = dated is not None or session.exec(select(Article.id).where(
                    Article.published_at == '', _window(Article.fetched_at, lower, upper)).limit(1)
                    .execution_options(n2d_query_name='causal.tree.historical_article_probe')).first() is not None
            pool, count = _tree_pool(session, terms, indexed, lower, upper, exclude, recall_articles=recall_articles)
        floor, ceiling = _tree_point(lower), _tree_point(upper)
        # Ingestion clocks are recall hints only. Evidence uses original dates.
        candidates = [value for item in pool if (value := _tree_eligible(item, floor, ceiling))]
        local.update(recall_count=count, pool_count=len(pool), eligible_count=len(candidates),
                     keyword_indexed=indexed, dated_evidence_count=sum(any(
                         _dated_evidence(source, floor, ceiling) for source in item['sources']) for item in candidates))
    return TreePool(queries, terms, event_text, lower, upper, set(exclude), candidates, local)


def _history_candidate(item, floor, ceiling):
    if not isinstance(item, dict) or not isinstance(item.get('ref'), str) or not item['ref']:
        return None
    if not isinstance(item.get('text'), str) or not item['text'].strip() or not isinstance(item.get('sources'), list):
        return None
    evidence = []
    for value in item['sources'][:MAX_EVIDENCE]:
        if not isinstance(value, dict) or value.get('kind') != HISTORICAL_PUBLISHER_KIND:
            continue
        quote = value.get('quote')
        if not isinstance(quote, str) or not quote.strip():
            continue
        source = _source(value.get('url'), value.get('title'), quote[:1200], value.get('published_at'),
                         HISTORICAL_PUBLISHER_KIND)
        if source and _dated_evidence(source, floor, ceiling):
            event_id = value.get('gdelt_event_id', '')
            if isinstance(event_id, str) and re.fullmatch(r'[0-9]{1,20}', event_id):
                source['gdelt_event_id'] = event_id
            evidence.append(source)
    if not evidence:
        return None
    candidate = _candidate(item['ref'], item['text'], evidence, ceiling.isoformat())
    # GDELT dates/codes are observations. Keep them as provenance only.
    for key in ('gdelt_event_id', 'gdelt_mention_url'):
        if isinstance(item.get(key), (str, int)):
            candidate[key] = item[key]
    return candidate


def _tree_merge(pool, historical):
    if not historical:
        return pool.candidates, set()
    floor, ceiling = _tree_point(pool.lower), _tree_point(pool.upper)
    known = {canonicalize_url(source['url']) for item in pool.candidates for source in item['sources']
             if _dated_evidence(source, floor, ceiling)}
    locals_by_ref = {item['ref'] for item in pool.candidates}
    historical_refs, supplements, extra_urls = set(), [], set()
    for item in historical:
        candidate = _history_candidate(item, floor, ceiling)
        if candidate is None or candidate['ref'] in (pool.excluded | locals_by_ref | historical_refs):
            continue
        uri = canonicalize_url(candidate['sources'][0]['url'])
        if uri in known or uri in extra_urls:
            continue
        supplements.append(candidate)
        historical_refs.add(candidate['ref'])
        extra_urls.add(uri)
        if len(supplements) >= TREE_POOL_LIMIT:
            break
    # A verified historical excerpt supersedes an undated copy of the same URL.
    local = [item for item in pool.candidates if not all(canonicalize_url(source['url']) in extra_urls
                                                        for source in item['sources'])]
    return (supplements + local)[:TREE_POOL_LIMIT], historical_refs


def rank_tree_pool(engine, pool: TreePool, *, historical=(), checkpoint=None) -> TreeRanking:
    """Rank local and publisher-verified historical evidence with the same model."""
    queries, terms, event_text = pool.queries, pool.terms, pool.event_text
    candidates, historical_refs = _tree_merge(pool, historical)
    local = dict(pool.local)
    if not candidates:
        return TreeRanking([], local)
    client, fallback = None, ''
    try:
        client = vector_client()
    except EmbeddingConfigurationError:
        from ..event_embeddings.queue import enabled
        if not os.getenv('EMBEDDING_BASE_URL','').strip() or not enabled():
            fallback = 'unconfigured' if not os.getenv('EMBEDDING_BASE_URL','').strip() else 'disabled'
        else:
            raise
    vectors, computed, query_vectors = {}, set(), []
    if client is not None:
        inputs = [*queries, *([event_text[:4000]] if event_text.strip() else [])]
        if checkpoint is not None:
            checkpoint()
        query_vectors = [_tree_unit(v) for v in client.get_embedding(inputs)]
        if len(query_vectors) != len(inputs) or len({len(v) for v in query_vectors}) != 1:
            raise EmbeddingError('归因查询返回向量数量或维度不符')
        dimensions = len(query_vectors[0])
        keys = [item['event_key'] for item in candidates if item['event_key']]
        if keys:
            with Session(engine) as session:
                rows = session.exec(select(EventEmbeddingRecord.event_key, EventEmbeddingRecord.embedding)
                    .join(EventEmbeddingJob, EventEmbeddingJob.event_key == EventEmbeddingRecord.event_key)
                    .where(EventEmbeddingRecord.event_key.in_(keys), EventEmbeddingRecord.profile == profile(client),
                        EventEmbeddingJob.state == 'ready').execution_options(n2d_query_name='causal.tree.stored_vectors')).all()
            for key,raw in rows:
                try:
                    value = _tree_unit(loads(raw, []))
                    if len(value) == dimensions:vectors[key] = value
                except EmbeddingError:
                    continue
        missing = [item for item in candidates if item['event_key'] not in vectors]
        for start in range(0,len(missing),TREE_EMBED_BATCH):
            part = missing[start:start+TREE_EMBED_BATCH]
            texts = [('\n'.join([item['text'], *(source['quote'] for source in item['sources'])]))[:1800] for item in part]
            if checkpoint is not None:
                checkpoint()
            result = client.get_embedding(texts)
            if len(result) != len(part):
                raise EmbeddingError('本地候选返回向量数量不符')
            for item,value in zip(part,result):
                unit = _tree_unit(value)
                if len(unit) != dimensions:raise EmbeddingError('本地候选返回向量维度不符')
                vectors[item['ref']] = unit;computed.add(item['ref'])
    scored, historical_ranked = [], 0
    mode = 'lexical_embedding' if client is not None else 'lexical'
    for item in candidates:
        lexical = _tree_lexical(item, terms)
        vector = vectors.get(item['ref'], vectors.get(item['event_key']))
        semantic = None
        if vector is not None:
            scores = [max(-1.,min(1.,fsum(a*b for a,b in zip(vector,q)))) for q in query_vectors]
            hypotheses = max(scores[:len(queries)])
            semantic = .8*hypotheses+.2*scores[-1] if len(scores)>len(queries) else hypotheses
            if item['ref'] in historical_refs:
                historical_ranked += 1
            else:
                local['vector_ranked_count'] += 1
        rank_score = .8*semantic+.2*lexical if semantic is not None else lexical
        item = {**item,'similarity':semantic if semantic is not None else 0.,'retrieval':dict(
            mode=mode, embedding_state=fallback or 'ready',
            pool_count=local['pool_count']+len(historical_refs),eligible_count=len(candidates),pool_limit=TREE_POOL_LIMIT,
            vector_score=semantic,
            vector_ranked_count=len(candidates) if client is not None else 0,
            hypothesis_queries=len(queries),parent_vector_used=bool(client is not None and event_text.strip()),
            lexical_score=lexical,rank_score=rank_score,
            candidate_vector='computed' if item['ref'] in computed else 'stored' if vector is not None else 'none',
            time_basis='source_publication_or_reviewed_occurrence',score_kind='ranking_not_probability')}
        item['retrieval']['keyword_indexed'] = local['keyword_indexed']
        scored.append(item)
    scored.sort(key=lambda item:(-item['retrieval']['rank_score'],item['ref']))
    return TreeRanking(scored[:TREE_RESULT_LIMIT], local, mode, len(historical_refs), historical_ranked)


def retrieve_for_tree(engine, queries: list[str], *, lower: str, upper: str,
                      exclude: set[str], event_text: str = '') -> list[dict]:
    """Compatibility entry point for a standalone local-only tree lookup."""
    pool = collect_tree_pool(engine, queries, lower=lower, upper=upper, exclude=exclude, event_text=event_text)
    return rank_tree_pool(engine, pool).rows


def _articles(session, terms, indexed, lower, upper, exclude):
    publish = or_(_window(Article.published_at, lower, upper), Article.published_at == lower[:10])
    unknown = (Article.published_at == '') & _window(Article.fetched_at, lower, upper)
    excluded = select(ArticleEventLink.article_key).where(ArticleEventLink.event_key.in_(exclude))
    statement = (select(*_article_fields()).where(or_(publish, unknown), Article.is_duplicate.is_(False),
                 Article.article_key.not_in(excluded), _search(Article, (Article.title, Article.content), terms, indexed))
                 .order_by(Article.id.desc()).limit(MAX_RESULTS))
    if not indexed:
        pool = select(Article.id).where(or_(publish, unknown)).order_by(Article.id.desc()).limit(LOCAL_SCAN)
        statement = statement.where(Article.id.in_(pool))
    return [_candidate('article:' + row[0], source['title'] + '\n' + source['quote'], [source], upper)
            for row in session.exec(statement)
            if (source := _article_source(row, terms)) and _in_window(source, upper)]


def _official(session, terms, lower, upper, *, recalled=None):
    pool = (select(OfficialEvent).where(OfficialEvent.event_date >= lower[:10], OfficialEvent.event_date <= upper[:10])
            .order_by(OfficialEvent.event_date.desc(), OfficialEvent.event_id.desc()).limit(LOCAL_SCAN).subquery())
    event = aliased(OfficialEvent, pool)
    matches = or_(*(func.lower(column).contains(term.casefold(), autoescape=True) for term in terms
                    for column in (event.actor1_name, event.actor2_name, event.action_place)))
    events = session.exec(select(event).where(matches)
                          .order_by(event.event_date.desc(), event.event_id.desc()).limit(MAX_RESULTS)).all()
    if recalled is not None:
        recalled.update(event.event_id for event in events)
    leads = source_candidates(events, mention_sources(session, [row.event_id for row in events]), source_url)
    urls = {item['url'] for mentions in leads.values() for item in mentions}
    if not urls:
        return []
    identity = {url: identity_key({'url': url}) for url in urls}
    cached = {row.key: row for row in session.exec(select(SourcePreview).where(
        SourcePreview.key.in_(identity.values()), SourcePreview.origin == 'source_page'
    ))}
    by_canonical = {}
    for record in session.exec(select(*_article_fields()).where(
        Article.canonical_url.in_({canonicalize_url(url) for url in urls})
    ).order_by(Article.is_duplicate, Article.id.desc()).limit(MAX_RESULTS * 4)):
        if source_url(record[2]):
            by_canonical.setdefault(canonicalize_url(record[2]), record)
    result = []
    for row in events:
        evidence = []
        for mention in leads[row.event_id]:
            url = mention['url']
            record = by_canonical.get(canonicalize_url(url))
            if record is not None:
                evidence.append(_article_source(record, terms))
            if preview := cached.get(identity[url]):
                evidence.append(_source(url, preview.title, _quote(preview.excerpt, terms),
                                        preview.published_at, 'publisher_preview'))
        candidate = _candidate('gdelt:' + row.event_id,
            f'GDELT 编码线索（日期待核对 {row.event_date}）\n' +
            '\n'.join(item['title'] for item in evidence if item), evidence, upper)
        if candidate['sources']:
            result.append(candidate)
    return result


def retrieve(engine, queries: list[str], *, lower: str, upper: str, exclude: set[str]) -> list[dict]:
    """Combine vector, lexical and locally saved GDELT source leads.

    Time bounds cap recall. A collection/index time and a raw GDELT code date
    are never copied into an event's occurrence or a publisher's date.
    """
    queries = _queries(queries)
    if not queries or lower >= upper:
        return []
    embedding_profile, vectors = _encode(queries)
    terms = _terms(queries)
    excluded = sorted(exclude)
    with Session(engine) as session:
        similarities = {}
        for vector in vectors:
            for item in find_similar(session, profile=embedding_profile, vector=vector,
                                     lower=lower, upper=upper, limit=MAX_RESULTS):
                key, score = item['event_key'], item['similarity']
                if key not in exclude:
                    similarities[key] = max(score, similarities.get(key, -1.))
        indexed = index_available(session) if terms else False
        curated = _curated(session, terms, indexed, lower, upper, excluded, similarities)
        articles = _articles(session, terms, indexed, lower, upper, excluded) if terms else []
        official = _official(session, terms, lower, upper) if terms else []
    # Prefer a curated interpretation to another lead from the same report.
    # Shared coverage alone never merges two independently curated events.
    urls = {canonicalize_url(item['sources'][0]['url']) for item in curated}
    groups = [curated]
    for candidates in (articles, official):
        unique = []
        for item in candidates:
            primary = canonicalize_url(item['sources'][0]['url'])
            if item['ref'] not in exclude and primary not in urls:
                urls.add(primary)
                unique.append(item)
        groups.append(unique)
    result, seen = [], set(exclude)
    for group in zip_longest(*groups):
        for item in group:
            if item and item['ref'] not in seen:
                result.append(item)
                seen.add(item['ref'])
            if len(result) == MAX_RESULTS:
                return result
    return result
