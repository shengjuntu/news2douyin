from __future__ import annotations

from typing import Any


def domain_matches(domain: str, allowed: set[str]) -> bool:
    # Match the domain itself or any subdomain (www.reuters.com vs reuters.com).
    return any(domain == d or domain.endswith('.' + d) for d in allowed)


def filter_items(items: list[dict[str, Any]], profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Rule-based filter: source whitelist/blacklist, keywords, categories."""
    include = [k.lower() for k in profile.get('keywords_include', [])]
    exclude = [k.lower() for k in profile.get('keywords_exclude', [])]
    whitelist = {d.lower() for d in profile.get('source_whitelist', [])}
    blacklist = {d.lower() for d in profile.get('source_blacklist', [])}
    categories = [c.lower() for c in profile.get('categories', [])]
    out = []
    for item in items:
        text = f"{item.get('title','')}\n{item.get('content','')}".lower()
        domain = (((item.get('source') or {}).get('domain')) or '').lower()
        if whitelist and domain and not domain_matches(domain, whitelist):
            continue
        if blacklist and domain and domain_matches(domain, blacklist):
            continue
        if include and not any(k in text for k in include):
            if categories and not any(c in text for c in categories):
                continue
        if exclude and any(k in text for k in exclude):
            continue
        out.append(item)
    return out
