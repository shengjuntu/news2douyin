"""Bounded retrieval of local, source-attributed leads for an investigation."""
from collections import defaultdict
from datetime import date, datetime, timezone
from itertools import zip_longest
import re

from sqlalchemy import func, or_, union_all
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from ..dedup.normalize import STOPWORDS, canonicalize_url
from ..desk.rules import ALIASES
from ..event_embeddings.documents import profile
from ..event_embeddings.runtime import vector_client
from ..event_embeddings.search import find_similar
from ..intake.official_models import OfficialEvent
from ..llm.client.embedding import EmbeddingError
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


def _curated(session, terms, indexed, lower, upper, exclude, similarities):
    title = func.coalesce(EventState.title_override, Event.event_title)
    prefix = (select(Event.event_key, func.substr(title, 1, 500), func.substr(Event.summary, 1, 850))
              .outerjoin(EventState, EventState.event_key == Event.event_key)
              .where(or_(EventState.merged_into.is_(None), EventState.merged_into == ''),
                     Event.event_key.not_in(exclude), _window(Event.first_seen_at, lower, upper)))
    lexical = {}
    if terms:
        statement = (prefix.where(_search(Event, (Event.event_title, Event.summary, Event.topic), terms, indexed))
                     .order_by(Event.first_seen_at.desc(), Event.event_key).limit(MAX_RESULTS))
        if not indexed:
            pool = select(Event.id).where(_window(Event.first_seen_at, lower, upper))
            statement = statement.where(Event.id.in_(pool.order_by(Event.first_seen_at.desc()).limit(LOCAL_SCAN)))
        lexical = {key: (title, summary) for key, title, summary in session.exec(statement)}
    keys = sorted(similarities, key=lambda key: -similarities[key])
    matches = {key: (title, summary) for key, title, summary in session.exec(
        prefix.where(Event.event_key.in_(keys))
    )} if keys else {}
    keys = list(dict.fromkeys((*keys, *lexical)))[:MAX_RESULTS]
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


def _official(session, terms, lower, upper):
    pool = (select(OfficialEvent).where(OfficialEvent.event_date >= lower[:10], OfficialEvent.event_date <= upper[:10])
            .order_by(OfficialEvent.event_date.desc(), OfficialEvent.event_id.desc()).limit(LOCAL_SCAN).subquery())
    event = aliased(OfficialEvent, pool)
    matches = or_(*(func.lower(column).contains(term.casefold(), autoescape=True) for term in terms
                    for column in (event.actor1_name, event.actor2_name, event.action_place)))
    events = session.exec(select(event).where(matches)
                          .order_by(event.event_date.desc(), event.event_id.desc()).limit(MAX_RESULTS)).all()
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
