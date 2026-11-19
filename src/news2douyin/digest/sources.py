"""Read already observed news from SQL. No network, graph writes or per-row queries.

GDELT's last_seen is an upstream observation time. Its event and mention tables
and NewsNow's topic table keep *current* projections. If their newest values
postdate the cutoff we omit them; a historical rebuild is not a reconstruction
of overwritten state. Local material comes from immutable article versions.
Current event links and accepted watch memberships can change after a past day.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from urllib.parse import urlsplit
from .calendar import day_bounds_utc

from sqlalchemy import func, or_
from sqlmodel import Session, select

from ..attention.models import AttentionEventMatch, AttentionItem, AttentionTopic
from ..desk.focus_lifecycle import visible_watch
from ..desk.models import RadarEntry, Watch, WatchEvent
from ..desk.rules import CHANGE, IMPACT
from ..intake.official_models import OfficialEvent, OfficialMention
from ..selection.service import CAMEO_LABELS
from ..selection.source_content import source_url, text, usable_title
from ..selection.source_models import SourcePreview
from ..storage.articles import identity_key
from ..storage.models import Article, ArticleEventLink, ArticleIdentity, ArticleVersion
from ..storage.sql import json_text
from .ranking import Candidate
from .source_breadth import HostSample
from .signals import action_passage

BATCH = 256


def _parts(session, query, checkpoint):
    for batch in session.exec(query.execution_options(yield_per=BATCH)).partitions(BATCH):
        if checkpoint is not None:
            checkpoint()
        yield batch


@dataclass(frozen=True)
class _Report:
    title: str
    url: str
    excerpt: str

    @property
    def domain(self):
        return urlsplit(self.url).hostname or ''


def _report(title, url, excerpt=''):
    valid_url = source_url(url)
    if valid_url and '\\' not in valid_url and isinstance(title, str) and usable_title(title):
        return _Report(text(title, 500), valid_url, text(excerpt, 520))
    return None


def _version_reports(session, id_column, version_ids):
    # In production a version may include a full article plus its connector's
    # raw metadata. Only send the few display fields to the offline ranker.
    doc = ArticleVersion.payload_json
    query = select(id_column, func.substr(json_text(doc, 'title'), 1, 500),
                   func.substr(json_text(doc, 'url'), 1, 16001),
                   func.substr(json_text(doc, 'content'), 1, 520)).where(
                       ArticleVersion.id.in_(version_ids))
    for key, title, url, excerpt in session.exec(query):
        yield key, _report(title, url, excerpt)


def _signals(title):
    return (2 * bool(IMPACT.search(title)) + bool(CHANGE.search(title))) / 3


def _readable_urls(session, values, end):
    """Resolve exactly these document identities, with no preview fetch or job."""
    ids = {url: identity_key({'url': url}) for url in set(values) if source_url(url)}
    if not ids:
        return {}
    keys = set(ids.values())
    by_id = {}
    cache_rows = session.exec(select(SourcePreview.key, SourcePreview.title, SourcePreview.excerpt,
        SourcePreview.source_url).where(SourcePreview.key.in_(keys), SourcePreview.fetched_at != '',
        SourcePreview.fetched_at < end))
    for key, title, excerpt, url in cache_rows:
        if result := _report(title, url, excerpt):
            by_id[key] = result
    # A current local title is usable only if it was already fetched by cutoff.
    rows = session.exec(select(ArticleIdentity.identity_key, Article.article_key, Article.title, Article.url,
        func.substr(Article.content, 1, 520), Article.fetched_at, Article.created_at).join(Article,
        ArticleIdentity.article_key == Article.article_key).where(ArticleIdentity.identity_key.in_(keys))).all()
    local_keys = [row[1] for row in rows]
    # An older fetched_at may be carried through a later backfill. If *any*
    # revision postdates the cutoff, only a dated historical snapshot is safe.
    revised_later = set(session.exec(select(ArticleVersion.article_key).where(
        ArticleVersion.article_key.in_(local_keys), ArticleVersion.observed_at >= end).distinct())) if local_keys else set()
    later = defaultdict(list)
    for key, article, title, url, excerpt, fetched, created in rows:
        if fetched and fetched < end and created and created < end and article not in revised_later:
            if result := _report(title, url, excerpt):
                by_id[key] = result
        else:
            later[article].append(key)
    if later:
        latest = select(func.max(ArticleVersion.id)).where(ArticleVersion.article_key.in_(later),
            ArticleVersion.observed_at != '', ArticleVersion.observed_at < end).group_by(ArticleVersion.article_key)
        for article, result in _version_reports(session, ArticleVersion.article_key, latest):
            if result:
                for key in later[article]:
                    by_id[key] = result
    return {url: by_id[key] for url, key in ids.items() if key in by_id}


def _attn(value):
    return None if value is None else max(0., min(1., value / 100.))


def _official_attention(session, event_ids, start, end):
    # Unconfirmed semantic candidates cannot transfer attention to a coded event.
    # Exact-document matches only signify related attention to the publication.
    rows = session.exec(select(AttentionEventMatch.event_id, func.max(AttentionTopic.attention_score))
        .join(AttentionTopic, AttentionTopic.key == AttentionEventMatch.topic)
        .where(AttentionEventMatch.event_id.in_(event_ids), AttentionEventMatch.status != 'rejected',
               or_(AttentionEventMatch.status == 'confirmed', AttentionEventMatch.method.in_(
                   ('source_document_url', 'mention_document_url'))),
               AttentionEventMatch.generation == AttentionTopic.generation,
               AttentionEventMatch.updated_at < end, AttentionTopic.updated_at < end,
               AttentionTopic.last_seen >= start, AttentionTopic.last_seen < end)
        .group_by(AttentionEventMatch.event_id))
    return {event: _attn(score) for event, score in rows}


def _official(session, start, end, day, stats, checkpoint, include_recovered):
    from ..selection.ngram_models import NgramTarget

    visible_today = (OfficialEvent.ingested_at != '', OfficialEvent.ingested_at < end,
                     OfficialEvent.last_seen >= start, OfficialEvent.last_seen < end)
    yield from _official_batches(session, select(OfficialEvent).where(*visible_today), start, end, day, stats, checkpoint, include_recovered)
    if not include_recovered:
        return

    # Use the immutable recovery even if the compact event was later updated.
    # Read only the stored event roles/code observed by the worker in that case.
    already_considered = select(OfficialEvent.event_id).where(
        OfficialEvent.event_id == NgramTarget.event_id, *visible_today).exists()
    new_evidence = select(NgramTarget.event_id).where(NgramTarget.state == 'ready', NgramTarget.observed_at >= start,
                    NgramTarget.observed_at < end, ~already_considered).distinct()
    from ..selection.official_evidence import coverage_by_event
    for keys in _parts(session, new_evidence, checkpoint):
        coverage = coverage_by_event(session, keys, start, end)
        recoveries = _batch_recoveries(session, keys, start, end, checkpoint)
        titles = _readable_urls(session, (r['url'] for r in recoveries.values()), end)
        stats['official_seen'] += len(recoveries)
        for key, entry in recoveries.items():
            source, reconstruction = _recovered_report(entry, titles.get(entry['url']))
            if not source:
                stats['official_no_readable'] += 1
                continue
            stats['official_reconstructed'] += 1
            stats['official_emitted'] += 1
            yield Candidate(key='gdelt:' + key, title=source.title, excerpt=source.excerpt,
                observed_at=entry['observed_at'], source_url=source.url, source_kind='gdelt',
                channel=CAMEO_LABELS.get(entry.get('cameo_root'), ''),
                actors=tuple(x for x in (entry.get('actor1'), entry.get('actor2')) if x),
                domains=(source.domain,), evidence_key=identity_key({'url': source.url}),
                official_coverage=dict(coverage[key], event_id=key, day=day),
                source_clock=dict(local_received_at=entry['observed_at'], clock_kind='reconstruction'),
                action_evidence=action_passage(source.title, source.excerpt, source.url, day, recovered=reconstruction),
                reconstruction=reconstruction, legacy_eligible=False)


def _batch_recoveries(session, keys, start, end, checkpoint):
    from ..selection.ngram_enrichment import iter_recovered
    result = {}
    for entry in iter_recovered(session, start, end, event_ids=keys, checkpoint=checkpoint):
        previous = result.get(entry['event_id'])
        if previous is None or (entry['observed_at'], entry['url']) > (previous['observed_at'], previous['url']):
            result[entry['event_id']] = entry
    return result


def _recovered_report(entry, report=None):
    # A publisher headline is usable only for this exact document. Otherwise
    # the UI identifies the display text as a reconstructed sentence.
    publisher = report is not None and report.url == entry['url']
    title = report.title if publisher else entry['excerpt']
    origin = 'publisher' if publisher else 'reconstructed_sentence'
    return _report(title, entry['url'], entry['excerpt']), dict(entry, title_origin=origin)


def _official_batches(session, query, start, end, day, stats, checkpoint, include_recovered):
    from ..selection.official_evidence import coverage_by_event

    for events in _parts(session, query, checkpoint):
        stats['official_seen'] += len(events)
        reports = _readable_urls(session, (e.source_url for e in events), end)
        keys = [e.event_id for e in events]
        attention = _official_attention(session, keys, start, end)
        coverage = coverage_by_event(session, keys, start, end)
        recovered = _batch_recoveries(session, keys, start, end, checkpoint) if include_recovered else {}
        domains = defaultdict(HostSample)
        best = {}
        # Each row is a distinct (event,document) in the compact index. Stream all
        # candidates, including cases where only a low-ranked mention has a title.
        mentions = select(OfficialMention.event_id, OfficialMention.doc_identifier,
            OfficialMention.confidence, OfficialMention.last_seen).where(OfficialMention.event_id.in_(keys),
            OfficialMention.last_seen < end, OfficialMention.updated_at != '', OfficialMention.updated_at < end)
        for batch in _parts(session, mentions, checkpoint):
            found = _readable_urls(session, (url for _, url, _, _ in batch if url not in reports), end)
            for key, url, confidence, seen in batch:
                if not source_url(url):
                    continue
                domains[key].add(urlsplit(url).hostname or '')
                related = reports.get(url) or found.get(url)
                if related:
                    rank = (confidence if confidence is not None else -1, seen, url)
                    if key not in best or rank > best[key][0]:
                        best[key] = (rank, related)
        for e in events:
            source = reports.get(e.source_url) or (best[e.event_id][1] if e.event_id in best else None)
            recovery = recovered.get(e.event_id)
            reconstruction = None
            legacy = source is not None
            observed = e.last_seen
            legacy_excerpt = None
            if recovery and source is None:
                source, reconstruction = _recovered_report(recovery)
                observed = recovery['observed_at']
                if source:
                    stats['official_reconstructed'] += 1
            elif recovery and not source.excerpt and source.url == recovery['url']:
                legacy_excerpt = source.excerpt
                source, reconstruction = _recovered_report(recovery, source)
                stats['official_augmented'] += 1
            if not source:
                stats['official_no_readable'] += 1
                continue
            domains[e.event_id].add(source.domain)
            stats['official_emitted'] += 1
            yield Candidate(key='gdelt:' + e.event_id, title=source.title, excerpt=source.excerpt,
                observed_at=observed, source_url=source.url, source_kind='gdelt',
                country=e.action_country_iso2.upper(), channel=CAMEO_LABELS.get(e.event_root_code, ''),
                actors=tuple(x for x in (e.actor1_name, e.actor2_name) if x),
                **domains[e.event_id].fields(), attention=attention.get(e.event_id),
                impact=min(1., abs(e.goldstein or 0.) / 10), evidence_key=identity_key({'url': source.url}),
                official_coverage=dict(coverage[e.event_id], event_id=e.event_id, day=day),
                source_clock=dict(event_day=e.event_date, upstream_first=e.first_seen,
                                  upstream_last=e.last_seen, local_received_at=e.ingested_at, clock_kind='official',
                                  event_ref=[e.feed, e.stamp, e.row_index]),
                action_evidence=action_passage(source.title, source.excerpt, source.url, day, recovered=reconstruction),
                reconstruction=reconstruction, legacy_eligible=legacy, legacy_excerpt=legacy_excerpt)


def _watch_names(session, events, end):
    found = defaultdict(list)
    query = select(WatchEvent.event, Watch.name).join(Watch, Watch.key == WatchEvent.watch).where(
        WatchEvent.event.in_(events), WatchEvent.choice != 'exclude', WatchEvent.first_seen < end,
        Watch.enabled == True, visible_watch()).order_by(WatchEvent.event, Watch.name)
    for event, name in session.exec(query):
        found[event].append(name)
    return found


@dataclass
class _Local:
    key: str
    watches: tuple
    latest: tuple = ()
    earliest: str = ''
    report: _Report | None = None
    domains: HostSample = field(default_factory=HostSample)

    def add(self, report, observed, vid):
        if report is None:
            return
        self.domains.add(report.domain)
        self.earliest = min(self.earliest or observed, observed)
        if (observed, vid) > self.latest:
            self.latest = observed, vid
            self.report = report

    def candidate(self, day):
        report = self.report
        return Candidate(key='local:' + self.key, title=report.title, source_kind='local',
            observed_at=self.latest[0], source_url=report.url, excerpt=report.excerpt,
            source_clock=dict(first_local=self.earliest, clock_kind='version'),
            **self.domains.fields(), watches=self.watches, impact=_signals(report.title),
            evidence_key=identity_key({'url': report.url}),
            action_evidence=action_passage(report.title, report.excerpt, report.url, day))


def _local(session, start, end, day, stats, checkpoint):
    # Sort compact keys only. Read snapshots for one fetch batch at a time and
    # keep just one event's evidence across a partition boundary.
    version_rows = select(ArticleEventLink.event_key, ArticleVersion.id, ArticleVersion.observed_at
        ).select_from(ArticleVersion).join(ArticleEventLink, ArticleEventLink.article_key == ArticleVersion.article_key
        ).join(RadarEntry, RadarEntry.key == ArticleEventLink.event_key).where(
            ArticleVersion.observed_at >= start, ArticleVersion.observed_at < end,
            ArticleVersion.origin != 'intake_history').order_by(ArticleEventLink.event_key)
    current = None

    def finish(entry):
        if entry.report:
            stats['local_emitted'] += 1
            yield entry.candidate(day)
        else:
            stats['local_no_readable'] += 1

    for rows in _parts(session, version_rows, checkpoint):
        watch_names = _watch_names(session, {r[0] for r in rows}, end)
        versions = dict(_version_reports(session, ArticleVersion.id, {r[1] for r in rows}))
        for key, vid, observed in rows:
            if current is None or current.key != key:
                if current:
                    yield from finish(current)
                stats['local_seen'] += 1
                current = _Local(key, tuple(watch_names.get(key, ())))
            current.add(versions[vid], observed, vid)
    if current:
        yield from finish(current)


def _newsnow(session, start, end, stats, checkpoint):
    query = select(AttentionTopic.key, AttentionTopic.last_seen, AttentionTopic.attention_score,
        AttentionTopic.updated_at, AttentionTopic.first_seen).where(AttentionTopic.last_seen >= start, AttentionTopic.last_seen < end,
        AttentionTopic.created < end)
    for topics in _parts(session, query, checkpoint):
        stats['newsnow_seen'] += len(topics)
        keys = [topic[0] for topic in topics]
        items = select(AttentionItem.topic, AttentionItem.title, AttentionItem.url, AttentionItem.excerpt,
            AttentionItem.last_seen, AttentionItem.key).where(AttentionItem.topic.in_(keys),
            AttentionItem.last_seen >= start, AttentionItem.last_seen < end)
        best = {}
        for batch in _parts(session, items, checkpoint):
            for key, title, url, excerpt, seen, item in batch:
                report = _report(title, url, excerpt)
                if report and (key not in best or (seen, item) > best[key][0]):
                    best[key] = ((seen, item), report)
        for key, seen, score, updated, first_seen in topics:
            if key not in best:
                stats['newsnow_no_readable'] += 1
                continue
            report = best[key][1]
            stats['newsnow_emitted'] += 1
            yield Candidate(key='newsnow:' + key, title=report.title, observed_at=seen,
                source_clock=dict(first_local=first_seen, clock_kind='attention'),
                source_url=report.url, source_kind='newsnow', attention=_attn(score) if updated < end else None,
                excerpt=report.excerpt, domains=(report.domain,), evidence_key=identity_key({'url': report.url}))


def iter_candidates(session: Session, day: date, *, stats: dict | None = None, cutoff: datetime | None = None,
                    checkpoint=None, include_recovered=True):
    """Emit one readable source lead per eligible projected event/topic; day in Beijing.

    Optional cutoff is an exclusive instant. The normal path never reads beyond now.
    Stream counters describe distinct projected entities, not raw feed rows.
    """
    stats = {} if stats is None else stats
    for name in ('official', 'local', 'newsnow'):
        for suffix in ('seen', 'no_readable', 'emitted'):
            stats[name + '_' + suffix] = 0
    begin, end_day = day_bounds_utc(day)
    until = cutoff or datetime.now(timezone.utc)
    if until.tzinfo is None:
        raise ValueError('cutoff must have a timezone')
    ceiling = min(end_day, until.astimezone(timezone.utc))
    if ceiling <= begin:
        return
    stamp = lambda d: d.isoformat(timespec='seconds').replace('+00:00', 'Z')
    start, end = stamp(begin), stamp(ceiling)
    stats['official_reconstructed'] = 0
    stats['official_augmented'] = 0
    stats['reconstruction_source_available'] = include_recovered
    yield from _official(session, start, end, day.isoformat(), stats, checkpoint, include_recovered)
    yield from _local(session, start, end, day.isoformat(), stats, checkpoint)
    yield from _newsnow(session, start, end, stats, checkpoint)
