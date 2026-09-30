from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from difflib import SequenceMatcher
import re
from uuid import uuid4

from sqlmodel import Session, select

from ..dedup.normalize import normalize_title, tokenize, jaccard
from ..storage.models import Article, ArticleEventLink, Event, EventState, utc_now_iso
from ..storage.utils import dumps

WINDOW_DAYS = 3
CANDIDATE_LIMIT = 250
GENERIC = {'market', 'stocks', 'shares', 'rise', 'fall', 'news', 'update', 'report',
           'reports', 'company', 'new', 'today', 'announces', 'announced'}


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError, AttributeError, TypeError):
        return None


@dataclass
class Assignment:
    event_key: str
    reason: str
    score: float = 0
    matched_article_key: str | None = None


def pair_score(item: dict, row: Article) -> float:
    """Conservative lexical evidence, not a trained semantic model."""
    a, b = normalize_title(item.get('title', '')), normalize_title(row.title)
    if not a or not b:
        return 0
    # Different quantities/dates may be follow-ups. Keep them separate for review.
    numbers = lambda text: set(re.findall(r'\d+(?:\.\d+)?|[零一二两三四五六七八九十百千万亿]+', text))
    if numbers(a) != numbers(b):
        return 0
    # Do not merge affirmative and negated versions of a claim.
    negative = lambda text: bool(re.search(r'\b(?:not|no|denies|denied|cancels|cancelled)\b|否认|未获|取消|不予', text))
    if negative(a) != negative(b):
        return 0
    # A shared topic must not erase a different named subject. This is purposely
    # conservative: lower-case paraphrases and translated names can remain split.
    if re.search(r'[a-zA-Z]', a) and a.split()[0] != b.split()[0]:
        return 0
    if re.match(r'[\u4e00-\u9fff]', a) and a[:2] != b[:2]:
        return 0
    ta, tb = set(tokenize(a)) - GENERIC, set(tokenize(b)) - GENERIC
    if len(ta & tb) < 3:
        return 0
    title = jaccard(ta, tb)
    body = jaccard(set(tokenize(item.get('content', '')[:3000])),
                   set(tokenize(row.content[:3000])))
    ratio = SequenceMatcher(None, a[:300], b[:300]).ratio()
    cjk_match = bool(re.search(r'[\u4e00-\u9fff]', a)) and title >= .5 and body >= .15 and ratio >= .82
    if a == b or (title >= .55 and body >= .18) or (ratio >= .82 and title >= .45 and body >= .3) or cjk_match:
        return round(.75 * title + .25 * body, 4)
    return 0


def choose_event(session: Session, item: dict, decision) -> Assignment:
    # Copies follow the persisted original event, irrespective of publication date.
    parent = decision.duplicate_of_article_key
    seen = set()
    while parent and parent not in seen:
        seen.add(parent)
        link = session.exec(select(ArticleEventLink).join(Event, Event.event_key == ArticleEventLink.event_key)
                            .where(ArticleEventLink.article_key == parent).order_by(ArticleEventLink.id)).first()
        if link:
            return Assignment(link.event_key, 'republication', decision.score, parent)
        article = session.exec(select(Article).where(Article.article_key == parent)).first()
        parent = article.duplicate_of_article_key if article else None
    published = timestamp(item.get('published_at'))
    if published:
        # Compare publication times, never ingestion time: historical backfills work.
        start = (published - timedelta(days=WINDOW_DAYS)).isoformat(timespec='seconds').replace('+00:00', 'Z')
        end = (published + timedelta(days=WINDOW_DAYS)).isoformat(timespec='seconds').replace('+00:00', 'Z')
        candidates = session.exec(select(Article, ArticleEventLink.event_key)
            .join(ArticleEventLink, Article.article_key == ArticleEventLink.article_key)
            .join(Event, Event.event_key == ArticleEventLink.event_key)
            .where(Article.country == item.get('country', 'us'), Article.language == item.get('language', 'en'), Article.is_duplicate == False,
                   Article.published_at >= start, Article.published_at <= end)
            .order_by(Article.published_at.desc(), Article.id.desc()).limit(CANDIDATE_LIMIT)).all()
        best = None
        for row, event_key in candidates:
            score = pair_score(item, row)
            if score and (best is None or score > best.score):
                best = Assignment(event_key, 'independent_report_rule_v1', score, row.article_key)
        if best:
            return best
    return Assignment('evt_' + uuid4().hex[:20], 'new_event')


def refresh_event(session: Session, event_key: str, *, touch_version=True) -> None:
    session.flush()
    event = session.exec(select(Event).where(Event.event_key == event_key)).one()
    rows = session.exec(select(Article).join(ArticleEventLink, Article.article_key == ArticleEventLink.article_key)
                        .where(ArticleEventLink.event_key == event_key)).unique().all()
    event.article_count = len(rows)
    times = [t for row in rows if (t := timestamp(row.published_at))]
    if times:
        # Never regress event history when a source updates its publication time.
        old = [t for value in (event.first_seen_at, event.last_seen_at) if (t := timestamp(value))]
        event.first_seen_at = min(times + old).isoformat(timespec='seconds').replace('+00:00', 'Z')
        event.last_seen_at = max(times + old).isoformat(timespec='seconds').replace('+00:00', 'Z')
    if rows:
        primary = sorted(rows, key=lambda row: (row.is_duplicate, row.id))[0]
        event.event_title, event.summary = primary.title, primary.content[:400]
        event.importance = max(row.market_relevance_score for row in rows)
        event.countries_json = dumps(sorted({row.country for row in rows if row.country}))
    state = session.get(EventState, event_key)
    if state:
        if state.title_override is not None:
            event.event_title = state.title_override
        if state.summary_override is not None:
            event.summary = state.summary_override
    if touch_version:
        state = state or EventState(event_key=event_key)
        state.version += 1
        state.updated_at = utc_now_iso()
        session.add(state)
    session.add(event)


def evidence_counts(session: Session, event_key: str) -> dict:
    stmt = select(Article).join(ArticleEventLink, Article.article_key == ArticleEventLink.article_key).where(ArticleEventLink.event_key == event_key)
    rows = session.exec(stmt).unique().all()
    independent = [row for row in rows if not row.is_duplicate]
    return {'article_count': len(rows), 'independent_article_count': len(independent),
            'source_domain_count': len({row.source_domain for row in independent if row.source_domain}),
            'duplicate_article_count': len(rows) - len(independent)}
