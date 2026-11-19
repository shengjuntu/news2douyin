"""Prepare normalized observations, grouped cards and legacy indexes on temporary disk.

One input pass supplies all strategies evaluating this day. Database handles and
materialized indexes only exist inside the grouped_candidates context.
"""
from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from collections.abc import Collection, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from itertools import groupby, islice
from urllib.parse import urlsplit

from ..dedup.normalize import canonicalize_url
from ..selection.source_content import source_url
from .calendar import BEIJING
from .signals import action_passage, headline_signals
from .source_breadth import MAX_DOMAIN_SAMPLE, HostSample, PublisherUnits
from .headline_identity import progress_keys


@dataclass(frozen=True, slots=True)
class Candidate:
    key: str
    title: str
    observed_at: str
    source_url: str
    source_kind: str
    country: str = ''
    channel: str = ''
    actors: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    watches: tuple[str, ...] = ()
    attention: float | None = None
    impact: float = 0.0
    evidence_key: str = ''
    excerpt: str = ''
    is_primary: bool = False
    focus_hints: tuple[str, ...] = ()
    official_coverage: dict | None = None
    action_evidence: dict | None = None
    reconstruction: dict | None = None
    legacy_eligible: bool = True
    legacy_excerpt: str | None = None
    publisher_units: tuple[tuple[str, str, str], ...] = ()
    source_clock: dict | None = None
    coarse: dict | None = None
    language: str = ''
    source_ref: dict | None = None
    _selection_receipt: str = field(default='', kw_only=True, repr=False, compare=False)


MAX_SOURCES = 8


def _unit(value: float | None) -> float:
    return max(0.0, min(1.0, value)) if value is not None and math.isfinite(value) else 0.0


def _utc_time(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec='microseconds')
    except (ValueError, OverflowError):
        return ''


def _bounded_values(values: Iterable[str], limit: int) -> list[str]:
    return sorted({value.strip() for value in values if value and value.strip()})[:limit]


def _observation(row: Candidate, *, shortlist_trial=False):
    title = ' '.join(row.title.split())
    url = source_url(row.source_url)
    previous_key, key = progress_keys(title, url)
    observed = _utc_time(row.observed_at)
    if not key or not url or not observed or not row.key or not row.source_kind:
        return None
    hostname = urlsplit(url).hostname or ''
    publisher_units = row.publisher_units
    if len(row.domains) <= MAX_DOMAIN_SAMPLE:
        domains = {domain.strip().casefold().removeprefix('www.') for domain in row.domains if domain.strip()}
        domains.add(hostname.casefold().removeprefix('www.'))
    else:
        # Account for every host before truncating the raw legacy sample.
        sample = HostSample()
        for host in row.domains:
            sample.add(host)
        sample.add(hostname)
        domains = sample.raw
        if sample.overflow is not None:
            publisher_units = (*publisher_units, *sample.overflow.units())
    headline_impact, original_document = headline_signals(title, url)
    record = (row.key, title[:500], observed, url, row.source_kind,
              row.country, row.channel, row.actors[:12], sorted(domains)[:MAX_DOMAIN_SAMPLE+1],
              row.watches[:64], row.attention, max(_unit(row.impact), headline_impact), row.evidence_key,
              row.excerpt[:1000], bool((row.is_primary or original_document) and not row.reconstruction),
              row.focus_hints[:16], row.official_coverage, row.action_evidence, row.reconstruction, row.legacy_eligible, row.legacy_excerpt)
    coarse = row.coarse if shortlist_trial else None
    if publisher_units or row.source_clock or coarse or row.language or row.source_ref:
        record += (publisher_units,)
    if row.source_clock or coarse or row.language or row.source_ref:
        record += (row.source_clock,)
    if coarse or row.language or row.source_ref:
        record += (coarse,)
    if row.language or row.source_ref:
        record += (row.language, row.source_ref)
    return key, previous_key, json.dumps(record, ensure_ascii=False, separators=(',', ':')), int(row.legacy_eligible)


def _group_record(key: str, observations: Iterator, policy: str, *, with_coarse=False):
    legacy = policy == 'v031'
    current = policy == 'current'
    merged_headlines = policy in ('current', 'v033')
    publishers = PublisherUnits() if current else None
    # Sample representatives by deterministic priority, independent of arrival
    # order. Bound per-group state even for a wire headline on thousands of URLs.
    evidence = {}
    # Count distinct candidate document links independently of the eight displayed samples.
    # A popular syndicated story can span thousands of URLs. One extra distinct URL
    # proves the lower bound without accumulating a growing per-story set.
    document_urls = set()
    domains = set()
    capped_domains = False
    watches = set()
    hints = set()
    actors = set()
    impact = 0.0
    attention = None
    primary = False
    latest = ''
    earliest_gkg = ''
    observation_count = 0
    best = None
    best_order = None
    last_variant = None
    variant_count = 0
    for _, variant, encoded in observations:
        if variant != last_variant:
            variant_count += 1
            last_variant = variant
        row = Candidate(*json.loads(encoded))
        if legacy and row.legacy_excerpt is not None:
            row = replace(row, excerpt=row.legacy_excerpt, is_primary=headline_signals(row.title, row.source_url)[1])
        observation_count += 1
        latest = max(latest, row.observed_at)
        if current and row.source_kind == 'gkg':
            earliest_gkg = min(earliest_gkg or row.observed_at, row.observed_at)
        impact = max(impact, row.impact)
        if row.attention is not None and math.isfinite(row.attention):
            attention = max(attention or 0.0, _unit(row.attention))
        primary |= row.is_primary
        if publishers is not None:
            publishers.hosts(row.domains)
            for unit in row.publisher_units:
                publishers.add(*unit)
        domains.update(row.domains)
        if len(domains) > MAX_DOMAIN_SAMPLE:
            domains = set(sorted(domains)[:MAX_DOMAIN_SAMPLE])
            capped_domains = True
        watches.update(row.watches)
        hints.update(row.focus_hints)
        if len(hints) > 16:
            hints = set(_bounded_values(hints, 16))
        actors.update(row.actors)
        if len(watches) > 64:
            watches = set(_bounded_values(watches, 64))
        if len(actors) > 12:
            actors = set(_bounded_values(actors, 12))
        # For otherwise equivalent reports prefer a route which can be selected
        # in the desk. Every displayed title/excerpt/link is a single observation.
        order = (row.is_primary, bool(row.excerpt), not bool(row.reconstruction) or legacy,
                 row.source_kind in ('local', 'gdelt', 'newsnow'),
                 row.observed_at, row.key, row.source_url, row.title, row.excerpt,
                 row.country, row.channel, row.evidence_key)
        if best_order is None or order > best_order:
            best_order, best = order, row
        url_key = canonicalize_url(row.source_url)
        if current and len(document_urls) <= MAX_DOMAIN_SAMPLE:
            document_urls.add(url_key)
        previous = evidence.get(url_key)
        if previous is None or order > previous[0]:
            sample = dict(url=row.source_url, title=row.title, source_kind=row.source_kind,
                          source_key=row.key, evidence_key=row.evidence_key)
            if row.language:
                sample['language'] = row.language
            if row.source_ref:
                sample['source_ref'] = row.source_ref
            if row.action_evidence and not legacy:
                sample['action_evidence'] = row.action_evidence
            if row.reconstruction and not legacy:
                sample['reconstruction'] = row.reconstruction
                sample['excerpt'] = row.excerpt
            evidence[url_key] = (order, sample)
        if len(evidence) > MAX_SOURCES:
            del evidence[min(evidence, key=lambda address: (evidence[address][0], address))]

    source_count = len(domains)
    watch_names = _bounded_values(watches, 64)
    actor_names = _bounded_values(actors, 12)
    hint_names = _bounded_values(hints.difference(watches), 16)
    record = dict(key=key, title=best.title, excerpt=best.excerpt, observed_at=latest,
                  source_kind=best.source_kind, source_key=best.key, source_url=best.source_url,
                  country=best.country, channel=best.channel, watches=watch_names, actors=actor_names,
                  focus_hints=hint_names, domains=sorted(domains), domain_count=source_count, domains_capped=capped_domains,
                  sources=[v[1] for v in sorted(evidence.values(), key=lambda v: v[0], reverse=True)],
                  observations=observation_count, impact=impact, attention=attention, is_primary=primary)
    if best.language:
        record['language'] = best.language
    if best.source_ref:
        record['source_ref'] = best.source_ref
    if merged_headlines and variant_count > 1:
        record['headline_variants'] = variant_count
    if current:
        record['candidate_documents'] = len(document_urls)
        record['candidate_documents_capped'] = len(document_urls) > MAX_DOMAIN_SAMPLE
        record['source_observed_at'] = best.observed_at
        if best.source_kind == 'gkg':
            record['earliest_gkg_receipt'] = earliest_gkg
        if best.source_clock:
            record['source_clock'] = best.source_clock
    if publishers is not None:
        record['publisher_breadth'] = publishers.snapshot()
        source_count = record['publisher_breadth']['units']
    breadth = math.log2(1 + min(source_count, 32)) / math.log2(33)
    editorial = 40*impact + 22*breadth + 16*primary + 7*bool(best.excerpt)
    focus_bonus = 15 if watch_names else 4 if hint_names else 0
    balanced = editorial + focus_bonus + 8*(attention or 0.0)
    # Counts describe a specific official code, never an addition across all
    # coded actions with similar headlines. Only evidence from the displayed
    # document is used to describe or score its action wording.
    if not legacy:
        if best.official_coverage:
            coverage = best.official_coverage
            record['official_coverage'] = coverage
        action = best.action_evidence
        if best.source_kind == 'gkg' and not action:
            # GKG carries a page title. Examine only the group's representative
            # once, using the local receipt day, with no inferred official event.
            period = (best.source_clock or {}).get('repair_day')
            if not period:
                period = datetime.fromisoformat(best.observed_at).astimezone(BEIJING).date().isoformat()
            action = action_passage(best.title, '', best.source_url, period)
            if action:
                for sample in record['sources']:
                    if sample['source_key'] == best.key and sample['url'] == best.source_url:
                        sample['action_evidence'] = action
                        break
        if action:
            record['action_evidence'] = action
            balanced += {'dated_action_report': 12, 'stage_report': 4}.get(action['kind'], 0)
        if (coverage := best.official_coverage) and coverage.get('history_complete'):
            # On an already covered code, additional publishers alone do not
            # add novelty credit. Keep the underlying breadth signal and counts.
            old_and_growing = (coverage.get('day') and coverage.get('documents', 0)
                               > coverage.get('new_documents', 0) > 0)
            if merged_headlines and old_and_growing and not action:
                record['coverage_growth_only'] = True
            elif current:
                # Historical totals don't identify newly arriving publisher
                # groups. Count the presence of new documents as one recency
                # signal, regardless of their number of domains.
                balanced += int(coverage.get('new_documents', 0) > 0)
            else:
                balanced += min(4.0, math.log2(1 + max(0, coverage['new_domains'])))
        if best.reconstruction:
            record['reconstruction'] = best.reconstruction
    # One primary coverage dimension per group. The prefix keeps unrelated
    # names of watches, actors and categories from colliding. Unknown stories
    # without any such metadata have no reason to compete as a single subject.
    if watch_names or hint_names:
        lane = 'watch:' + (watch_names or hint_names)[0].casefold()
    elif actor_names:
        lane = 'actor:' + actor_names[0].casefold()
    elif best.country or best.channel:
        lane = f'area:{best.country.casefold()}/{best.channel.casefold()}'
    else:
        lane = key
    result = (key, lane, latest, source_count, round(editorial, 6), round(balanced, 6),
              json.dumps(record, ensure_ascii=False, separators=(',', ':')))
    if with_coarse:
        # A group's representative and its coarse features must be from the same report.
        return result, best.coarse
    return result


class GroupedDay:
    """A prepared candidate pool shared by the strategies comparing one day."""

    def __init__(self, db, stats, baseline_stats, shortlist_trial=False):
        self.db = db
        self.stats = stats
        self._baseline_stats = baseline_stats
        self._ready_tables = {'progress'}
        self.has_coarse_features = shortlist_trial

    @property
    def has_legacy_source(self):
        return self._baseline_stats is not None

    def _table_for(self, policy):
        table = _TABLES[policy]
        if policy == 'v031' and not self.has_legacy_source:
            raise ValueError('Enable keep_baseline to compare the v031 source universe')
        if table not in self._ready_tables:
            _populate_progress(self.db, policy)
            self._ready_tables.add(table)
        return table

    def group_index(self, policy):
        """Get the internal SQL table and population counts for a grouping policy."""
        table = self._table_for(policy)
        stats = self._baseline_stats if policy == 'v031' else self.stats
        if policy not in ('current', 'v033'):
            stats = dict(stats, groups=self.db.execute(f'SELECT count(*) FROM {table}').fetchone()[0])
        return table, stats

    def iter_cards(self):
        for (record,) in self.db.execute('SELECT card FROM progress ORDER BY key'):
            yield json.loads(record)

    def cards_for(self, keys, *, policy='current'):
        """Read selected cards by key from a prepared grouping policy."""
        table = self._table_for(policy)
        keys = list(dict.fromkeys(keys))
        cards = {}
        for start in range(0, len(keys), 500):
            batch = keys[start:start + 500]
            marks = ','.join('?' for _ in batch)
            rows = self.db.execute(f'SELECT key,card FROM {table} WHERE key IN ({marks})', batch)
            cards.update((key, json.loads(card)) for key, card in rows)
        return cards

    def current_cards(self, keys: Collection[str]) -> dict[str, dict]:
        """Read current-policy evidence for a bounded set of known headline keys."""
        return self.cards_for(keys)

    def current_key_map(self, keys: Collection[str]) -> dict[str, str]:
        keys = list(dict.fromkeys(keys))
        mapping = {key: key for key in keys}
        for first in range(0, len(keys), 500):
            batch = keys[first:first + 500]
            marks = ','.join('?' for _ in batch)
            mapping.update(self.db.execute(
                f'SELECT original, current FROM aliases WHERE original IN ({marks})', batch))
        return mapping

    def current_keys(self, keys: Collection[str]) -> set[str]:
        return set(self.current_key_map(keys).values())

    def select(self, *, limit=100, prior_keys: Collection[str] = (), mode='balanced'):
        """Compatibility entry point for the existing named rankings."""
        from .ranked_selection import MODES, check_legacy_limit
        from .strategies import STRATEGIES

        check_legacy_limit(limit)
        if mode not in MODES:
            raise ValueError(f'unknown digest mode {mode!r}')
        return STRATEGIES.select(self, mode, limit=limit, prior_keys=prior_keys)

    def select_shortlist_trial(self, *, prior_keys=(), limit=100, variant='combined', on_merge=None):
        """Compatibility entry point for the three shortlist experiment arms."""
        from .ranked_selection import check_legacy_limit
        from .shortlist_trial import VARIANTS
        from .strategies import STRATEGIES

        check_legacy_limit(limit)
        if variant not in VARIANTS:
            raise ValueError(f'unknown shortlist trial variant {variant!r}')
        return STRATEGIES.select(self, 'shortlist_' + variant, prior_keys=prior_keys,
                                 limit=limit, on_merge=on_merge)


_TABLES = {'current': 'progress', 'v033': 'progress_v033', 'v032': 'progress_v032', 'v031': 'progress_v031'}


def _populate_progress(db, policy, *, shortlist_trial=False):
    table = _TABLES[policy]
    db.execute(f'CREATE TABLE {table} (key TEXT PRIMARY KEY, lane TEXT, observed TEXT, breadth INTEGER, '
               'editorial REAL, balanced REAL, card TEXT)')
    where = 'WHERE legacy = 1' if policy == 'v031' else ''
    column = 'current' if policy in ('current', 'v033') else 'original'
    # The second sort key counts distinct original headline groups in bounded
    # memory, even for material syndicated across thousands of domains.
    rows = db.execute(f'SELECT {column}, original, payload FROM observations {where} ORDER BY 1, 2')
    groups = (_group_record(key, group, policy, with_coarse=shortlist_trial)
              for key, group in groupby(rows, key=lambda row: row[0]))
    insert = f'INSERT INTO {table} VALUES (?,?,?,?,?,?,?)'
    if not shortlist_trial:
        db.executemany(insert, groups)
        return
    from .shortlist_trial import create_features, features_row
    create_features(db)
    while batch := list(islice(groups, 256)):
        db.executemany(insert, (record for record, _ in batch))
        db.executemany('INSERT INTO shortlist_features VALUES (?,?,?,?,?,?)',
                       (features_row(record[0], coarse) for record, coarse in batch if coarse))


@contextmanager
def grouped_candidates(candidates: Iterable[Candidate], *, keep_baseline=True, shortlist_trial=False):
    # Empty path requests a private throw-away SQLite file; there are no locks
    # on the operational store and closing the connection removes the file.
    db = sqlite3.connect('')
    try:
        db.executescript("""
            PRAGMA temp_store=FILE;
            PRAGMA cache_size=-8192;
            PRAGMA journal_mode=OFF;
            CREATE TABLE observations (current TEXT, original TEXT, payload TEXT, legacy INTEGER);
            CREATE TABLE aliases (original TEXT PRIMARY KEY, current TEXT);
            CREATE TABLE prior (key TEXT PRIMARY KEY);
        """)
        stats = dict(scanned=0, invalid=0, groups=0)
        per_kind = Counter()
        introduced = Counter()
        nonlegacy_invalid = 0

        def observations():
            nonlocal nonlegacy_invalid
            for row in candidates:
                stats['scanned'] += 1
                per_kind[row.source_kind] += 1
                record = _observation(row, shortlist_trial=shortlist_trial)
                if keep_baseline and not row.legacy_eligible:
                    introduced[row.source_kind] += 1
                if record is None:
                    stats['invalid'] += 1
                    nonlegacy_invalid += int(not row.legacy_eligible)
                else:
                    yield record

        db.executemany('INSERT INTO observations VALUES (?,?,?,?)', observations())
        db.execute('INSERT OR IGNORE INTO aliases SELECT original,current FROM observations WHERE original != current')
        # A third-party mirror may reprint exactly the same headline including
        # the station credit, but have no trustworthy article slug of its own.
        # Its existing v032 group shares any attested alias for that title.
        if db.execute('SELECT 1 FROM aliases LIMIT 1').fetchone():
            db.execute('UPDATE observations SET current = (SELECT current FROM aliases '
                       'WHERE aliases.original = observations.original) '
                       'WHERE current = original AND original IN (SELECT original FROM aliases)')
        _populate_progress(db, 'current', shortlist_trial=shortlist_trial)
        # Breadth and editorial remain callable even when the v031 comparison
        # is disabled. Retain the temporary observations until context exit
        # so those original-group indexes can still be built lazily on demand.
        stats['groups'] = db.execute('SELECT count(*) FROM progress').fetchone()[0]
        stats['scanned_by_source'] = dict(sorted(per_kind.items()))
        baseline_stats = dict(scanned=stats['scanned'] - sum(introduced.values()), invalid=stats['invalid'] - nonlegacy_invalid,
                              scanned_by_source=dict(sorted((per_kind - introduced).items()))) if keep_baseline else None
        yield GroupedDay(db, stats, baseline_stats, shortlist_trial=shortlist_trial)
    finally:
        db.close()
