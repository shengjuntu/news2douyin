from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

PROFILE_FIELDS = {'name', 'provider', 'country', 'language', 'categories',
                  'keywords_include', 'keywords_exclude', 'source_whitelist',
                  'source_blacklist', 'max_items', 'market_scope', 'market_tags'}


def load_profile_file(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding='utf-8')) or {}
    return normalize_profile_dict(data)


def normalize_profile_dict(data: dict[str, Any]) -> dict[str, Any]:
    # REST exposes provider-specific options under `extra`; YAML historically
    # puts them at the top level. Accept both, without shadowing core fields.
    extra = data.get('extra') or {}
    if not isinstance(extra, dict):
        raise ValueError('profile.extra must be an object')
    profile = {k: v for k, v in extra.items() if k not in PROFILE_FIELDS and k != 'extra'}
    profile.update({k: v for k, v in data.items() if k != 'extra'})
    profile.setdefault('provider', 'worldnewsapi')
    gdelt = profile['provider'] in ('gdelt','gkg')
    profile.setdefault('country', '' if gdelt else 'us')
    profile.setdefault('language', '' if gdelt else 'en')
    if gdelt:
        profile.setdefault('collection_mode', 'search')
        profile.setdefault('filter_mode', 'rules')
    profile.setdefault('categories', [])
    profile.setdefault('keywords_include', [])
    profile.setdefault('keywords_exclude', [])
    profile.setdefault('source_whitelist', [])
    profile.setdefault('source_blacklist', [])
    profile.setdefault('max_items', 100)
    profile.setdefault('market_scope', profile.get('country') or 'global')
    profile.setdefault('market_tags', [])
    return profile
