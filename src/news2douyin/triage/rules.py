from __future__ import annotations

from difflib import SequenceMatcher
from urllib.parse import urlparse

from loguru import logger


def _sim(a: str, b: str) -> float:
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


def apply_rules(raw_items: list[dict], rules_cfg: dict) -> list[dict]:
    """Apply lightweight rule-based filtering and de-duplication."""
    if not rules_cfg.get("enable", True):
        logger.info(f"[TRIAGE] rules disabled, pass-through n={len(raw_items)}")
        return raw_items

    n0 = len(raw_items)

    drop_if_missing_url = bool(rules_cfg.get("drop_if_missing_url", True))
    drop_if_no_content = bool(rules_cfg.get("drop_if_no_content", True))

    dedup_cfg = rules_cfg.get("dedup") or {}
    by_url = bool(dedup_cfg.get("by_url", True))
    by_title = bool(dedup_cfg.get("by_title_similarity", True))
    thr = float(dedup_cfg.get("title_similarity_threshold", 0.92))

    blacklist_domains = set((rules_cfg.get("blacklist_domains") or []))
    whitelist_domains = set((rules_cfg.get("whitelist_domains") or []))

    seen_urls: set[str] = set()
    seen_titles: list[str] = []
    out: list[dict] = []

    dropped = {
        "missing_url": 0,
        "no_content": 0,
        "blacklist_domain": 0,
        "not_in_whitelist": 0,
        "dup_url": 0,
        "dup_title": 0,
    }

    for it in raw_items:
        url = (it.get("url") or "").strip()
        title = (it.get("title") or "").strip()
        content = (it.get("content") or "").strip()
        dom = (it.get("source") or {}).get("domain") or _domain(url)

        if drop_if_missing_url and not url:
            dropped["missing_url"] += 1
            continue
        if drop_if_no_content and not content:
            dropped["no_content"] += 1
            continue
        if dom and dom in blacklist_domains:
            dropped["blacklist_domain"] += 1
            continue
        if whitelist_domains and dom not in whitelist_domains:
            dropped["not_in_whitelist"] += 1
            continue

        if by_url and url and url in seen_urls:
            dropped["dup_url"] += 1
            continue
        if by_title and title:
            if any(_sim(title, t) >= thr for t in seen_titles):
                dropped["dup_title"] += 1
                continue

        out.append(it)
        if url:
            seen_urls.add(url)
        if title:
            seen_titles.append(title)

    logger.info(
        f"[TRIAGE] rules done {n0}->{len(out)} dropped={n0-len(out)} " 
        f"(missing_url={dropped['missing_url']}, no_content={dropped['no_content']}, "
        f"blacklist_domain={dropped['blacklist_domain']}, not_in_whitelist={dropped['not_in_whitelist']}, "
        f"dup_url={dropped['dup_url']}, dup_title={dropped['dup_title']})"
    )
    return out
