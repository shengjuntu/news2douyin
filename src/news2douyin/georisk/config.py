"""Inputs which determine membership in the daily signal."""
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha256
import os
from pathlib import Path
import re

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from ..storage.utils import dumps

GKG_FEEDS = ('gkg_english', 'gkg_translation')
DEFAULT_SCOPES = {
    'global': '',
    'europe': ('AL AD AT BE BA BG BY CH CY CZ DE DK EE ES FI FR GB GR HR HU IE IS IT LI LT LU '
               'LV MC MD ME MK MT NL NO PL PT RO RS RU SE SI SK SM TR UA VA XK'),
    'middle-east': 'AE BH EG IL IQ IR JO KW LB OM PS QA SA SY TR YE',
    'east-asia': 'CN HK JP KP KR MO MN TW',
}
DEFAULT_CATEGORIES = {
    'conflict': ['ARMEDCONFLICT', 'MILITARY', 'TERROR'],
    'sanctions': ['ECON_TRADE_DISPUTE', 'EPU_CATS_TRADE_POLICY'],
}
DEFAULT_TITLE_TERMS = {
    'sanctions': ['sanction', 'embargo', 'export control', '制裁', '出口管制', 'санкц',
                  'эмбарго', 'sanción', 'sancion', 'sanzion', 'sanktion', 'عقوبات'],
}
SCOPE_LABELS = {'global': '全球', 'europe': '欧洲', 'middle-east': '中东', 'east-asia': '东亚'}
CATEGORY_LABELS = {'conflict': '冲突与军事', 'sanctions': '制裁与贸易限制'}
IDENTIFIER = re.compile(r'[a-z][a-z0-9-]{0,31}')


@dataclass(frozen=True)
class Settings:
    feeds: tuple[str, ...]
    scopes: dict[str, frozenset[str]]
    categories: dict[str, frozenset[str]]
    title_terms: dict[str, tuple[str, ...]]
    key: str


def enabled():
    return os.getenv('NEWS2DOUYIN_GEORISK_ENABLED', '').strip().lower() in ('1', 'true', 'on', 'yes')


def replay_days():
    try:
        value = int(os.getenv('NEWS2DOUYIN_GEORISK_REPLAY_DAYS', '60'))
    except ValueError:
        raise ValueError('地缘风险回看天数必须为 1–180') from None
    if not 1 <= value <= 180:
        raise ValueError('地缘风险回看天数必须为 1–180')
    return value


def _strings(values, maximum, pattern):
    if (not isinstance(values, list) or len(values) > maximum
            or any(not isinstance(value, str) or not pattern.fullmatch(value) for value in values)):
        raise ValueError('地缘风险配置中的列表或值无效')


def _mapping(value):
    if (not isinstance(value, dict) or not value or len(value) > 12
            or any(not IDENTIFIER.fullmatch(key) for key in value)):
        raise ValueError('地缘风险范围或分类无效')
    return value


@lru_cache(maxsize=4)
def _load(path):
    data = {}
    if path:
        try:
            with Path(path).open('rb') as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError()
            data = tomllib.loads(raw.decode('utf-8'))
        except (OSError, ValueError):
            raise ValueError('无法读取地缘风险 TOML 配置') from None
    if set(data) - {'feeds', 'scopes', 'categories', 'title_terms'}:
        raise ValueError('地缘风险配置含未知字段')
    feeds = data.get('feeds', list(GKG_FEEDS))
    if not isinstance(feeds, list) or not feeds or any(feed not in GKG_FEEDS for feed in feeds):
        raise ValueError('地缘风险来源须为已接收的 GKG 批次流')
    feeds = tuple(sorted(set(feeds)))
    scopes = _mapping(data.get('scopes', {key: countries.split() for key, countries in DEFAULT_SCOPES.items()}))
    categories = _mapping(data.get('categories', DEFAULT_CATEGORIES))
    title_terms = data.get('title_terms', {key: words for key, words in DEFAULT_TITLE_TERMS.items() if key in categories})
    if not isinstance(title_terms, dict) or set(title_terms) - categories.keys():
        raise ValueError('标题线索须对应已配置的地缘风险分类')
    if 'global' not in scopes or scopes['global'] != []:
        raise ValueError('地缘风险范围须包含 global = []')
    for key, values in scopes.items():
        _strings(values, 250, re.compile(r'[A-Za-z]{2}'))
        if key != 'global' and not values:
            raise ValueError('地区范围须有国家或地区代码')
    for values in categories.values():
        _strings(values, 40, re.compile(r'[A-Z0-9_]{1,100}'))
        if not values:
            raise ValueError('分类须有 GKG 主题代码')
    for values in title_terms.values():
        _strings(values, 40, re.compile(r'[^\x00-\x1f]{1,60}'))
        if any(not value.strip() for value in values):
            raise ValueError('标题线索不能为空')
    scopes = {key: frozenset(value.lower() for value in values) for key, values in scopes.items()}
    categories = {key: frozenset(values) for key, values in categories.items()}
    title_terms = {key: tuple(sorted({value.strip().casefold() for value in values}))
                   for key, values in title_terms.items() if values}
    spec = [1, feeds, {key: sorted(values) for key, values in scopes.items()},
            {key: sorted(values) for key, values in categories.items()}, title_terms]
    return Settings(feeds, scopes, categories, title_terms, sha256(dumps(spec).encode()).hexdigest())


def settings():
    return _load(os.getenv('NEWS2DOUYIN_GEORISK_CONFIG', '').strip())
