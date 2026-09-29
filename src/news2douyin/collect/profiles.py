from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_profile_file(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding='utf-8')) or {}
    return normalize_profile_dict(data)


def normalize_profile_dict(data: dict[str, Any]) -> dict[str, Any]:
    profile = dict(data)
    profile.setdefault('provider', 'worldnewsapi')
    profile.setdefault('country', 'us')
    profile.setdefault('language', 'en')
    profile.setdefault('categories', [])
    profile.setdefault('keywords_include', [])
    profile.setdefault('keywords_exclude', [])
    profile.setdefault('source_whitelist', [])
    profile.setdefault('source_blacklist', [])
    profile.setdefault('max_items', 100)
    profile.setdefault('market_scope', profile.get('country', 'global'))
    profile.setdefault('market_tags', [])
    return profile
