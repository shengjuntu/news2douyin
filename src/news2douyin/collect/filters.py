from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from ..enrich.service import classify_categories


def domain_matches(domain: str, allowed: set[str]) -> bool:
    # Match the domain itself or any subdomain (www.reuters.com vs reuters.com).
    return any(domain == d or domain.endswith('.' + d) for d in allowed)


def hard_filter_reason(item, profile):
    text = f"{item.get('title','')}\n{item.get('content','')}".lower()
    domain = (((item.get('source') or {}).get('domain')) or urlparse(item.get('url') or '').hostname or '').lower().rstrip('.')
    whitelist = {d.lower() for d in profile.get('source_whitelist', [])}
    blacklist = {d.lower() for d in profile.get('source_blacklist', [])}
    if whitelist and not domain_matches(domain, whitelist):
        return 'source_not_allowed'
    if blacklist and domain and domain_matches(domain, blacklist):
        return 'source_blocked'
    if any(k.lower() in text for k in profile.get('keywords_exclude', [])):
        return 'excluded_keyword'
    return ''


def hard_filter_items(items: list[dict[str, Any]], profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Non-negotiable source/exclusion constraints, shared by all filter modes."""
    return [item for item in items if not hard_filter_reason(item, profile)]


def filter_items(items: list[dict[str, Any]], profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Hard constraints followed by keyword-or-category relevance matching."""
    include = [k.lower() for k in profile.get('keywords_include', [])]
    categories = [c.lower() for c in profile.get('categories', [])]
    out = []
    for item in hard_filter_items(items, profile):
        text = f"{item.get('title', '')}\n{item.get('content', '')}".lower()
        if include or categories:
            if not any(k in text for k in include) and not classify_categories(text, categories):
                continue
        out.append(item)
    return out
