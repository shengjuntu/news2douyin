from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import os
import re
from typing import Any

from sqlmodel import Session, select

from .normalize import normalize_article, jaccard, short_hash, tokenize
from .llm_arbiter import llm_same_event
from ..storage.models import Article

CANDIDATE_LIMIT = 1000
NEAR_DUP_THRESHOLD = 0.90
CONTENT_MATCH_TITLE_GATE = 0.40
MAX_CHAIN_DEPTH = 10
DIGIT_DIFF_PENALTY = 0.85
_DIGIT_RE = re.compile(r'[0-9零一二两三四五六七八九十百千万亿]')
GRAY_ZONE_LOW = 0.60
DEDUP_LLM_ENABLED = os.getenv('DEDUP_LLM_ENABLED', '1').strip().lower() not in {'0', 'false', 'no', 'off'}


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


def _title_kind_conflict(a_title: str, b_title: str) -> bool:
    a = (a_title or '').strip()
    b = (b_title or '').strip()
    if not a or not b or a == b:
        return False
    sm = SequenceMatcher(None, a, b)
    diff_a = ''.join(a[i1:i2] for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag in ('replace', 'insert'))
    diff_b = ''.join(b[j1:j2] for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag in ('replace', 'delete'))
    cjk_short = re.compile(r'[\u4e00-\u9fff]{2,6}$')
    if not diff_a or not diff_b or not cjk_short.fullmatch(diff_a) or not cjk_short.fullmatch(diff_b):
        return False
    return not (set(diff_a) & set(diff_b))


def _has_digit_difference(a_title: str, b_title: str) -> bool:
    sm = SequenceMatcher(None, (a_title or '')[:200], (b_title or '')[:200])
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'replace' and (_DIGIT_RE.search(a_title[i1:i2]) or _DIGIT_RE.search(b_title[j1:j2])):
            return True
    return False


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
    norm = normalize_article(item.get('title',''), item.get('content',''), item.get('url',''))
    country = (item.get('country') or 'us').lower()
    candidates = _candidate_rows(session, country, before_id=before_id)

    for row in candidates:
        if norm.canonical_url and norm.canonical_url == row.canonical_url:
            return DedupDecision(norm, True, row.article_key, _resolve_root_group_id(session, row), 'same_url', 1.0, row.title_signature or row.content_signature)
        if norm.title_hash and norm.title_hash == row.title_hash:
            return DedupDecision(norm, True, row.article_key, _resolve_root_group_id(session, row), 'same_title_hash', 0.98, row.title_signature or row.content_signature)

    if norm.content_hash:
        for row in candidates:
            if row.content_hash and row.content_hash == norm.content_hash and _title_ratio(norm.normalized_title, row.normalized_title) >= CONTENT_MATCH_TITLE_GATE:
                return DedupDecision(norm, True, row.article_key, _resolve_root_group_id(session, row), 'same_content_hash', 0.98, row.title_signature or row.content_signature)

    best_row = None
    best_score = 0.0
    norm_title_tokens = set(norm.title_tokens)
    norm_content_tokens = set(norm.content_tokens[:80])
    for row in candidates:
        row_title_tokens = set(tokenize(row.normalized_title or ''))
        row_content_tokens = set(tokenize(row.normalized_content or '')[:80])
        title_ratio = _title_ratio(norm.normalized_title, row.normalized_title)
        jt = jaccard(norm_title_tokens, row_title_tokens)
        jc = jaccard(norm_content_tokens, row_content_tokens)
        score = max(title_ratio, jt * 0.6 + jc * 0.4)
        if title_ratio >= 0.80 and score >= NEAR_DUP_THRESHOLD and _has_digit_difference(norm.normalized_title, row.normalized_title):
            score *= DIGIT_DIFF_PENALTY
        if score > best_score:
            best_score = score
            best_row = row

    if best_row and best_score >= NEAR_DUP_THRESHOLD:
        return DedupDecision(norm, True, best_row.article_key, _resolve_root_group_id(session, best_row), 'near_duplicate', float(round(best_score, 3)), best_row.title_signature or best_row.content_signature)

    llm_enabled = os.getenv('DEDUP_LLM_ENABLED', str(int(DEDUP_LLM_ENABLED))).strip().lower() not in {'0', 'false', 'no', 'off'}
    if llm_enabled and best_row is not None and GRAY_ZONE_LOW <= best_score < NEAR_DUP_THRESHOLD:
        verdict = llm_same_event(norm.normalized_title, norm.normalized_content, best_row.normalized_title, best_row.normalized_content)
        if verdict is True and not _title_kind_conflict(norm.normalized_title, best_row.normalized_title):
            return DedupDecision(norm, True, best_row.article_key, _resolve_root_group_id(session, best_row), 'llm_same_event', float(round(best_score, 3)), best_row.title_signature or best_row.content_signature)

    event_seed = norm.content_signature or norm.title_signature or short_hash(norm.normalized_title, length=18)
    gid = short_hash(norm.canonical_url or norm.title_hash or event_seed, length=18)
    return DedupDecision(norm, False, None, gid, 'unique', 0.0, event_seed)
