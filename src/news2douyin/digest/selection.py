"""Public v1 contract for selecting a daily edition from its full candidate stream."""
from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
from collections.abc import Collection, Iterable
from copy import deepcopy
from dataclasses import dataclass, fields, replace
from datetime import date, datetime
from importlib import metadata
from itertools import chain
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from ..dedup.normalize import canonicalize_url
from ..selection.source_content import source_url
from .candidate_pool import MAX_SOURCES, Candidate, _utc_time
from .source_breadth import MAX_DOMAIN_SAMPLE, HostSample, PublisherUnits
from .strategies import DEFAULT_STRATEGY, STRATEGIES

API_VERSION = 1
ENTRY_POINT_GROUP = 'news2douyin.selection'
CONFIG_ENV = 'NEWS2DOUYIN_SELECTION_CONFIG'


@dataclass(frozen=True, slots=True)
class SelectionContext:
    day: date
    cutoff: datetime  # Exclusive upper bound used by the day's source adapters.
    limit: int
    prior_keys: frozenset[str]


@dataclass(frozen=True, slots=True)
class SelectionPick:
    candidate: Candidate
    key: str | None = None  # Optional stable story/cluster identifier.
    score: float = 0.0
    reasons: tuple[str, ...] = ()
    related: tuple[Candidate, ...] = ()


class DailySelectionStrategy(Protocol):
    api_version: int
    version: str

    def select(self, candidates: Iterable[Candidate], context: SelectionContext) -> Iterable[SelectionPick]:
        """Consume the single-pass stream and emit picks in final editorial order."""
        ...


def _plugins():
    return metadata.entry_points(group=ENTRY_POINT_GROUP)


def list_selection_strategies() -> list[str]:
    """Discover installable strategies without importing any plugin code."""
    builtins = {s.id for s in STRATEGIES.all()}
    return sorted(builtins.union(ep.name for ep in _plugins()))


# A receipt travels with an observation even when a plugin spills it to disk.
# This keeps source checks independent of day size and of the plugin's grouping.
_EVIDENCE_FIELDS = tuple(f.name for f in fields(Candidate) if f.name != '_selection_receipt')


def _receipt(candidate: Candidate, run_key: bytes) -> str:
    values = [getattr(candidate, name) for name in _EVIDENCE_FIELDS]
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return hashlib.blake2b(payload, key=run_key, digest_size=16).hexdigest()


def _card(pick: SelectionPick, run_key: bytes) -> dict:
    if not isinstance(pick, SelectionPick) or not isinstance(pick.candidate, Candidate):
        raise ValueError('expected SelectionPick with a representative Candidate')
    row = pick.candidate
    key = row.key if pick.key is None else pick.key
    if not isinstance(key, str) or not 1 <= len(key) <= 160 or key.strip() != key or not key.isprintable():
        raise ValueError('selection key must be 1..160 printable characters without surrounding spaces')
    if type(pick.score) not in (int, float) or not math.isfinite(pick.score):
        raise ValueError('selection score must be finite')
    if not isinstance(pick.reasons, (tuple, list)) or not all(isinstance(r, str) for r in pick.reasons):
        raise ValueError('selection reasons must be a sequence of strings')
    hosts = HostSample()
    publishers = PublisherUnits()
    documents = set()
    sources = []
    seen_urls = set()
    latest = ''
    earliest_gkg = None
    for candidate in chain((row,), pick.related):
        if (not isinstance(candidate, Candidate) or not candidate.key or not candidate.title.strip()
                or not candidate.source_kind or not source_url(candidate.source_url)):
            raise ValueError('selected evidence requires an identified source and readable title')
        if candidate._selection_receipt != _receipt(candidate, run_key):
            raise ValueError('selected evidence did not match an observation from this input')
        timestamp = _utc_time(candidate.observed_at)
        if not timestamp:
            raise ValueError('selected evidence has an invalid observation time')
        latest = max(latest, timestamp)
        if candidate.source_kind == 'gkg' and (earliest_gkg is None or timestamp < earliest_gkg):
            earliest_gkg = timestamp
        address = canonicalize_url(candidate.source_url)
        if len(documents) <= MAX_DOMAIN_SAMPLE:
            documents.add(address)
        for domain in chain(candidate.domains, (urlsplit(candidate.source_url).hostname,)):
            hosts.add(domain)
        for unit in candidate.publisher_units:
            publishers.add(*unit)
        if len(sources) < MAX_SOURCES and address not in seen_urls:
            seen_urls.add(address)
            sample = dict(url=candidate.source_url, title=candidate.title, source_kind=candidate.source_kind,
                          source_key=candidate.key, evidence_key=candidate.evidence_key)
            if candidate.language:
                sample['language'] = candidate.language
            if candidate.source_ref:
                sample['source_ref'] = candidate.source_ref
            if candidate.action_evidence:
                sample['action_evidence'] = candidate.action_evidence
            if candidate.reconstruction:
                sample.update(reconstruction=candidate.reconstruction, excerpt=candidate.excerpt)
            sources.append(sample)
    publishers.hosts(hosts.raw)
    if hosts.overflow:
        for unit in hosts.overflow.units():
            publishers.add(*unit)
    domains = sorted(hosts.raw)[:MAX_DOMAIN_SAMPLE]
    card = dict(key=key, selection_provider='plugin', title=row.title, excerpt=row.excerpt, observed_at=latest,
                source_observed_at=row.observed_at, source_kind=row.source_kind, source_key=row.key,
                source_url=row.source_url, country=row.country, channel=row.channel, actors=list(row.actors),
                watches=list(row.watches), focus_hints=list(row.focus_hints), attention=row.attention,
                impact=row.impact, is_primary=row.is_primary, sources=sources, domains=domains,
                domain_count=len(domains), domains_capped=len(hosts.raw) > MAX_DOMAIN_SAMPLE,
                observations=1 + len(pick.related), publisher_breadth=publishers.snapshot(),
                candidate_documents=len(documents), candidate_documents_capped=len(documents) > MAX_DOMAIN_SAMPLE,
                score=pick.score, reasons=list(pick.reasons))
    if row.source_kind == 'gkg':
        card['earliest_gkg_receipt'] = earliest_gkg
    for field in ('source_clock', 'action_evidence', 'official_coverage', 'reconstruction', 'language', 'source_ref'):
        if getattr(row, field):
            card[field] = getattr(row, field)
    return deepcopy(card)


@dataclass(frozen=True)
class SelectionPlan:
    strategy: str
    version: str
    limit: int
    _options_json: str
    needs_coarse: bool
    _plugin: DailySelectionStrategy | None = None

    @property
    def options(self) -> dict:
        return json.loads(self._options_json)

    def select(self, candidates: Iterable[Candidate], *, day: date, cutoff: datetime,
               prior_keys: Collection[str] = ()) -> dict:
        if not isinstance(day, date) or isinstance(day, datetime):
            raise ValueError('selection day must be a date')
        if not isinstance(cutoff, datetime) or cutoff.utcoffset() is None:
            raise ValueError('selection cutoff must be a timezone-aware datetime')
        context = SelectionContext(day, cutoff, self.limit, frozenset(prior_keys))
        if self._plugin is None:
            with STRATEGIES.prepare(candidates, self.strategy) as pool:
                result = STRATEGIES.select(pool, self.strategy, limit=self.limit, prior_keys=context.prior_keys)
        else:
            run_key = secrets.token_bytes(16)
            source = iter(candidates)
            scanned = 0

            def counted():
                nonlocal scanned
                for row in source:
                    scanned += 1
                    yield replace(row, _selection_receipt=_receipt(row, run_key))

            stream = counted()
            items = []
            keys = set()
            for pick in self._plugin.select(stream, context):
                if len(items) >= self.limit:
                    raise ValueError('plugin exceeded the requested TopN')
                item = _card(pick, run_key)
                if item['key'] in keys:
                    raise ValueError('plugin returned duplicate selection keys')
                keys.add(item['key'])
                items.append(item)
            # Finish source accounting even if an implementation reads only a prefix.
            consumed = scanned
            for _ in source:
                scanned += 1
            result = dict(items=items, stats=dict(scanned=scanned, strategy_consumed=consumed,
                                                 selected=len(items), mode=self.strategy))
        result['stats']['selection'] = dict(api_version=API_VERSION, strategy=self.strategy, version=self.version,
                                            limit=self.limit, options=self.options)
        return result


def create_selection(strategy: str = DEFAULT_STRATEGY, limit: int = 100, options: dict | None = None) -> SelectionPlan:
    if type(limit) is not int or limit < 1:
        raise ValueError('selection.limit must be a positive integer')
    if not isinstance(strategy, str) or not strategy.strip():
        raise ValueError('selection.strategy must be a nonempty name')
    options = {} if options is None else options
    if not isinstance(options, dict):
        raise ValueError('selection.options must be a table')
    try:
        encoded = json.dumps(options, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError('selection.options must be JSON-compatible') from exc
    matching = [ep for ep in _plugins() if ep.name == strategy]
    builtin = next((s for s in STRATEGIES.all() if s.id == strategy), None)
    if len(matching) + int(builtin is not None) > 1:
        raise ValueError(f'ambiguous selection strategy {strategy!r}')
    if builtin:
        if options:
            raise ValueError(f'{strategy!r} does not accept selection.options')
        return SelectionPlan(strategy, builtin.version, limit, encoded, builtin.requires_coarse)
    if not matching:
        raise ValueError(f'unknown selection strategy {strategy!r}')
    plugin = matching[0].load()(json.loads(encoded))
    if type(getattr(plugin, 'api_version', None)) is not int or plugin.api_version != API_VERSION:
        raise ValueError(f'{strategy!r} must implement selection API v{API_VERSION}')
    if not isinstance(getattr(plugin, 'version', None), str) or not plugin.version.strip():
        raise ValueError(f'{strategy!r} must declare its implementation version')
    if not callable(getattr(plugin, 'select', None)):
        raise ValueError(f'{strategy!r} must implement select')
    coarse = getattr(plugin, 'needs_coarse', False)
    if type(coarse) is not bool:
        raise ValueError(f'{strategy!r} needs_coarse must be boolean')
    return SelectionPlan(strategy, plugin.version, limit, encoded, coarse, plugin)


def load_selection(path: str | Path | None = None) -> SelectionPlan:
    config_path = path if path is not None else os.environ.get(CONFIG_ENV)
    if not config_path:
        return create_selection()
    with Path(config_path).open('rb') as handle:
        config = tomllib.load(handle)
    if set(config) != {'selection'} or not isinstance(config['selection'], dict):
        raise ValueError('expected a [selection] table in selection config')
    settings = config['selection']
    unknown = settings.keys() - {'strategy', 'limit', 'options'}
    if unknown:
        raise ValueError(f'unknown selection settings: {sorted(unknown)}')
    return create_selection(**settings)


def select_top_n(candidates: Iterable[Candidate], *, day: date, cutoff: datetime, n: int = 100,
                 strategy: str = DEFAULT_STRATEGY, options: dict | None = None,
                 prior_keys: Collection[str] = ()) -> dict:
    """Select at most N from the supplied day's unranked candidates."""
    return create_selection(strategy, n, options).select(candidates, day=day, cutoff=cutoff, prior_keys=prior_keys)
