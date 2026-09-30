from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
from typing import Any

from sqlmodel import Session, select

from .normalize import normalize_article, short_hash
from ..storage.models import Article

CANDIDATE_LIMIT = 1000
CONTENT_MATCH_TITLE_GATE = 0.40
MAX_CHAIN_DEPTH = 10
# Kept for older integrations; semantic LLM verdicts no longer classify copies.
DEDUP_LLM_ENABLED = False


@dataclass
class DedupDecision:
    normalized: Any
    is_duplicate: bool
    duplicate_of_article_key: str | None
    dedup_group_id: str
    reason: str
    score: float
    event_seed: str


def _candidate_rows(session: Session, country: str, limit: int = CANDIDATE_LIMIT, before_id: int | None = None) -> list[Article]:
    stmt = (
        select(Article)
        .where(Article.country == country)
        .order_by(Article.id.desc())
        .limit(limit)
    )
    if before_id is not None:
        stmt = stmt.where(Article.id < before_id)
    return list(session.exec(stmt))


def _title_ratio(a_title: str, b_title: str) -> float:
    return SequenceMatcher(None, (a_title or '')[:200], (b_title or '')[:200]).ratio()


def _resolve_root_group_id(session: Session, row: Article) -> str:
    seen: set[str] = set()
    cur = row
    for _ in range(MAX_CHAIN_DEPTH):
        if not cur.is_duplicate or not cur.duplicate_of_article_key or cur.duplicate_of_article_key in seen:
            break
        seen.add(cur.duplicate_of_article_key)
        parent = session.exec(select(Article).where(Article.article_key == cur.duplicate_of_article_key)).first()
        if parent is None:
            break
        cur = parent
    return cur.dedup_group_id or short_hash(cur.article_key, length=18)


def decide_duplicate(session: Session, item: dict[str, Any], before_id: int | None = None) -> DedupDecision:
    """Detect copied text, not merely reports of the same event.

    Exact hashes search the whole database; bounded near-text comparisons follow.
    Same URL is resolved by ArticleIdentity before this function during collection.
    """
    norm = normalize_article(item.get('title', ''), item.get('content', ''), item.get('url', ''))
    country = (item.get('country') or 'us').lower()
    exact = select(Article).where(Article.country == country)
    if before_id is not None:
        exact = exact.where(Article.id < before_id)
    if norm.content_hash:
        matches = session.exec(exact.where(Article.content_hash == norm.content_hash)
                               .order_by(Article.id).limit(20)).all()
        for row in matches:
            if _title_ratio(norm.normalized_title, row.normalized_title) >= CONTENT_MATCH_TITLE_GATE:
                return DedupDecision(norm, True, row.article_key, _resolve_root_group_id(session, row),
                                     'same_content_hash', 1.0, row.title_signature)
    # Legacy short articles had no content_hash. Exact title lookup is indexed,
    # but a title alone is never sufficient to classify a copied report.
    if norm.normalized_title and len(norm.normalized_content) >= 40:
        matches = session.exec(exact.where(Article.title_hash == norm.title_hash)
                               .order_by(Article.id).limit(100)).all()
        for row in matches:
            if row.normalized_content == norm.normalized_content:
                return DedupDecision(norm, True, row.article_key, _resolve_root_group_id(session, row),
                                     'same_text', 1.0, row.title_signature)
    best, best_score = None, 0.0
    for row in _candidate_rows(session, country, before_id=before_id):
        if len(norm.normalized_content) < 80 or len(row.normalized_content) < 80:
            continue
        if max(len(norm.normalized_content), len(row.normalized_content)) > 8000:
            continue  # Do not call a truncated prefix a complete copy.
        # Different quantities are not silently collapsed as republications.
        numbers = lambda text: re.findall(r'\d+(?:\.\d+)?', text)
        if numbers(norm.normalized_title + norm.normalized_content) != numbers(row.normalized_title + row.normalized_content):
            continue
        if _title_ratio(norm.normalized_title, row.normalized_title) < CONTENT_MATCH_TITLE_GATE:
            continue
        matcher = SequenceMatcher(None, norm.normalized_content, row.normalized_content)
        if matcher.quick_ratio() < .94:
            continue
        score = matcher.ratio()
        if score >= .94 and score > best_score:
            best, best_score = row, score
    if best:
        return DedupDecision(norm, True, best.article_key, _resolve_root_group_id(session, best),
                             'near_copied_text', round(best_score, 3), best.title_signature)
    # The legacy same-event LLM verdict is deliberately not a duplicate verdict.
    event_seed = norm.content_signature or norm.title_signature or short_hash(norm.normalized_title, length=18)
    gid = short_hash(norm.canonical_url or norm.title_hash or event_seed, length=18)
    return DedupDecision(norm, False, None, gid, 'unique', 0.0, event_seed)
