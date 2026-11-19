"""Read source observations through stable references without duplicating upstream archives."""
from __future__ import annotations

from datetime import date, datetime, timezone
from hashlib import sha256
import json
import re
from typing import Iterable
from urllib.parse import urlsplit

from sqlalchemy import or_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from ..digest.calendar import day_bounds_utc
from ..digest.candidate_pool import Candidate
from ..dedup.normalize import canonicalize_url
from ..selection.source_content import source_url, text, usable_title
from ..storage.articles import identity_key
from ..storage.models import utc_now_iso
from ..translations.language import normalize_language
from .models import SourceReport

CANDIDATE_LAYER_VERSION = 1
REPORT_KIND = 'collected'
MAX_REPORT_TEXT = 50_000
REPORT_COLUMNS = (SourceReport.key, SourceReport.provider, SourceReport.url, SourceReport.title,
                  SourceReport.excerpt, SourceReport.language, SourceReport.published_at,
                  SourceReport.observed_at, SourceReport.provenance_json)
_KEY = re.compile(r'[a-zA-Z0-9_:+\-]{1,160}\Z')


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value):
    return sha256(_json(value).encode()).hexdigest()


def _instant(value):
    if not isinstance(value, str) or not value:
        return ''
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return _stamp(moment)
    except (ValueError, OverflowError):
        return ''


def _stamp(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('候选时间须包含时区')
    return value.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _readable_url(value):
    return source_url(value) if isinstance(value, str) and '\\' not in value else ''


def _plain(value, limit):
    return value.replace('\x00', '')[:limit] if isinstance(value, str) else ''


def _content_kind(item, content):
    if not content.strip():
        return 'metadata'
    metadata = item.get('research_meta') or {}
    declared = item.get('content_kind') or (metadata.get('content_kind') if isinstance(metadata, dict) else '')
    return declared if isinstance(declared, str) and declared in {'text', 'summary', 'excerpt', 'reconstruction'} else 'excerpt'


def record_reports(session: Session, items: Iterable[dict], *, provider: str, batch: str,
                   source_ref: dict | None = None, observed_at: str = '') -> list[str]:
    """Index readable observations before relevance filtering, in the caller's transaction.

    The batch and its ordinal identify an immutable delivered item. Replaying the
    same collection stage fills missing records without changing existing ones.
    Malformed records remain in the provider's raw archive.
    """
    if not isinstance(provider, str) or not provider or not isinstance(batch, str) or not batch:
        raise ValueError('候选记录需要信源和批次编号')
    accepted = []
    recorded_at = _instant(observed_at) or utc_now_iso()
    insert = pg_insert if session.get_bind().dialect.name == 'postgresql' else sqlite_insert
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        url = _readable_url(item.get('url'))
        title = text(_plain(item.get('title'), 1000), 500)
        if not url or not usable_title(title):
            continue
        key = 'report:' + _digest([provider, batch, index])
        observed_content = _plain(item.get('content'), MAX_REPORT_TEXT + 1)
        content = observed_content[:MAX_REPORT_TEXT]
        kind = _content_kind(item, content)
        if kind == 'text' and len(observed_content) > MAX_REPORT_TEXT:
            kind = 'excerpt'
        declared = _plain(item.get('source_language', item.get('language')), 200)
        language = normalize_language(item.get('language'), provider=provider) or normalize_language(declared, provider=provider)
        country = _plain(item.get('country'), 24)
        reference = dict(source_ref or {})
        row_ref = item.get('source_ref')
        if isinstance(row_ref, dict):
            reference.update(row_ref)
        research_meta = item.get('research_meta') or {}
        if isinstance(research_meta, dict) and research_meta.get('upstream_id') is not None:
            reference['upstream_id'] = research_meta['upstream_id']
        meta = item.get('meta') or {}
        if isinstance(meta, dict) and meta.get('api_cluster_id'):
            reference['upstream_cluster_id'] = meta['api_cluster_id']
        reference.update(provider=provider, batch=batch, row_index=index, source_language=declared,
                         media_country=country, content_kind=kind, material_sha256=_digest(content))
        source_observed = _instant(item.get('fetched_at'))
        if source_observed:
            reference['source_observed_at'] = source_observed
        accepted.append(dict(key=key, provider=provider, batch=batch, row_index=index, url=url, title=title,
                             excerpt=text(content, 520), content=content, content_kind=kind, language=language,
                             country=country, observed_at=recorded_at, published_at=_instant(item.get('published_at')),
                             provenance_json=_json(reference)))
    if accepted:
        for offset in range(0, len(accepted), 100):
            session.execute(insert(SourceReport).on_conflict_do_nothing(index_elements=['key']),
                            accepted[offset:offset+100])
    return [row['key'] for row in accepted]


def _report_candidate(row: SourceReport | tuple) -> Candidate:
    key, provider, url, title, excerpt, language, published, observed, provenance = (
        tuple(getattr(row, column.key) for column in REPORT_COLUMNS) if isinstance(row, SourceReport) else row)
    return Candidate(key=key, title=title, observed_at=observed, source_url=url, source_kind=REPORT_KIND,
                     domains=(urlsplit(url).hostname or '',), excerpt=excerpt, language=language,
                     evidence_key=identity_key({'url': url}), legacy_eligible=False, source_ref=json.loads(provenance),
                     source_clock={'clock_kind': 'collected', 'published_at': published})


def list_reports(session: Session, *, query: str = '', language: str = '', provider: str = '',
                 before: tuple[str, str] | None = None, limit: int = 30) -> list[Candidate]:
    """Browse the indexed prefilter observations without loading document bodies."""
    if type(limit) is not int or not 1 <= limit <= 200:
        raise ValueError('候选页大小无效')
    report = SourceReport
    statement = select(*REPORT_COLUMNS)
    if query.strip():
        term = query.strip()[:200]
        statement = statement.where(or_(report.title.icontains(term, autoescape=True),
                                         report.excerpt.icontains(term, autoescape=True)))
    if language:
        code = normalize_language(language, provider=provider)
        if not code:
            raise ValueError('候选语种无效')
        statement = statement.where(report.language == code)
    if provider:
        statement = statement.where(report.provider == provider)
    if before:
        observed, key = before
        statement = statement.where(or_(report.observed_at < observed,
                                         (report.observed_at == observed) & (report.key < key)))
    rows = session.exec(statement.order_by(report.observed_at.desc(), report.key.desc()).limit(limit))
    return [_report_candidate(row) for row in rows]


def _report_rows(session, start, end, checkpoint):
    statement = (select(*REPORT_COLUMNS)
                 .where(SourceReport.observed_at >= start, SourceReport.observed_at < end)
                 .order_by(SourceReport.observed_at, SourceReport.key))
    for batch in session.exec(statement.execution_options(yield_per=256)).partitions(256):
        if checkpoint is not None:
            checkpoint()
        for row in batch:
            yield _report_candidate(row)


def _begin_snapshot(session):
    connection = session.connection()
    if connection.dialect.name == 'postgresql':
        connection.exec_driver_sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
    else:
        connection.exec_driver_sql('BEGIN')


def _sql_day(session, day, *, cutoff, since, stats, checkpoint, include_recovered, include_reports, receipts):
    from ..digest.sources import iter_candidates

    options = {'since': since} if since is not None else {}
    if not include_recovered:
        options['include_recovered'] = False
    if receipts:
        options['receipts'] = True
    yield from iter_candidates(session, day, cutoff=cutoff, stats=stats, checkpoint=checkpoint, **options)
    if include_reports:
        start, end = day_bounds_utc(day)
        stats['candidate_layer'] = CANDIDATE_LAYER_VERSION
        stats['collected_emitted'] = 0
        for candidate in _report_rows(session, _stamp(max(start, since) if since else start),
                                      _stamp(min(end, cutoff)), checkpoint):
            stats['collected_emitted'] += 1
            yield candidate


def iter_day(db: Session | Engine, storage_root, day: date, *, cutoff=None, since=None, stats=None,
             checkpoint=None, include_recovered=True, include_reports=True, restored_stamps=(),
             coarse=False, read_only=False, receipts=False):
    """Stream the same day-window sources for Daily, Focus and historical replay.

    Supplying an Engine opens an independent SQL snapshot, released before
    scanning DuckDB. Older issue replays bypass the new collected source index.
    Focus uses receipts to see repaired material at its real local arrival.
    """
    counts = {} if stats is None else stats
    end = cutoff if cutoff is not None else datetime.now(timezone.utc)
    _stamp(end)
    if since is not None:
        _stamp(since)
    options = dict(cutoff=end, since=since, stats=counts, checkpoint=checkpoint,
                   include_recovered=include_recovered, include_reports=include_reports, receipts=receipts)
    if isinstance(db, Session):
        yield from _sql_day(db, day, **options)
    else:
        with Session(db) as session:
            _begin_snapshot(session)
            yield from _sql_day(session, day, **options)
    if storage_root is not None:
        from ..digest.archive import iter_gkg
        window = {'since': since} if since is not None else {}
        if restored_stamps:
            window['restored_stamps'] = restored_stamps
        if coarse:
            window['coarse'] = True
        if read_only:
            window['read_only'] = True
        if receipts:
            window['receipts'] = True
        yield from iter_gkg(storage_root, day, cutoff=end, stats=counts, checkpoint=checkpoint, **window)


def candidate_token(candidate: Candidate) -> str:
    """Bind an editor's action to the exact material displayed by the server."""
    return _digest(dict(key=candidate.key, title=candidate.title, url=candidate.source_url,
                        observed_at=candidate.observed_at, language=candidate.language,
                        excerpt=candidate.excerpt, provenance=candidate.source_ref))


def _material(candidate, *, content='', content_kind='metadata', provider='', country='', published_at='',
              article_key=''):
    return dict(candidate=candidate, key=candidate.key, url=candidate.source_url, title=candidate.title,
                content=content, language=candidate.language, provider=provider or candidate.source_kind,
                source_kind=candidate.source_kind, country=country, published_at=published_at,
                observed_at=candidate.observed_at, source_ref=candidate.source_ref or {},
                content_kind=content_kind, article_key=article_key)


def _point_candidate(key, *, title, url, observed, kind, reference, language='', content=''):
    if not title or not observed:
        raise KeyError('未找到可用的来源候选')
    return Candidate(key=key, title=text(title, 500), observed_at=observed, source_url=_readable_url(url),
                     source_kind=kind, excerpt=text(content, 520), language=language, source_ref=reference,
                     evidence_key=identity_key({'url': url}) if _readable_url(url) else '')


def _stored_material(session, key, cutoff):
    row = session.get(SourceReport, key)
    if row is None or (cutoff and row.observed_at >= cutoff):
        raise KeyError('未找到可用的来源候选')
    candidate = _report_candidate(row)
    article_key = candidate.source_ref.get('article_key', '') if row.batch.startswith('connector/') else ''
    return _material(candidate, provider=row.provider, country=row.country, article_key=article_key,
                     published_at=row.published_at, content=row.content, content_kind=row.content_kind)


def _article_material(session, key, kind, ident, cutoff):
    from ..storage.models import Article, ArticleEventLink, ArticleVersion
    from ..events.workbench import canonical_event

    version = select(ArticleVersion)
    if kind == 'local':
        ident = canonical_event(session, ident)
        version = version.join(ArticleEventLink, ArticleEventLink.article_key == ArticleVersion.article_key)
        version = version.where(ArticleEventLink.event_key == ident)
    else:
        version = version.where(ArticleVersion.article_key == ident)
    if cutoff:
        version = version.where(ArticleVersion.observed_at < cutoff)
    row = session.exec(version.order_by(ArticleVersion.observed_at.desc(), ArticleVersion.id.desc()).limit(1)).first()
    if row is not None:
        document = json.loads(row.payload_json)
        article_key = row.article_key
        observed = row.observed_at
        reference = {'article_key': article_key, 'revision': row.revision, 'material_sha256': row.content_hash}
    elif kind == 'article' and not cutoff:
        article = session.exec(select(Article).where(Article.article_key == ident)).first()
        if article is None:
            raise KeyError('未找到可用的来源候选')
        document = article.model_dump()
        article_key = article.article_key
        observed = _instant(article.fetched_at or article.created_at)
        reference = {'article_key': article_key, 'material_sha256': _digest([article.title, article.content, article.url])}
    else:
        raise KeyError('未找到可用的来源候选')
    provider = document.get('provider') or kind
    declared = document.get('language', '')
    reference.update(provider=provider, source_language=declared)
    if kind == 'local':
        reference['local_event'] = ident
    content = document.get('content') or ''
    raw = document.get('raw') or {}
    declared_kind = _content_kind(raw, content) if isinstance(raw, dict) else 'excerpt'
    content_kind = ('metadata' if not content else
                    declared_kind if provider == 'worldnewsapi' else
                    'excerpt' if provider in ('newsnow', 'gdelt', 'gkg') else 'text')
    candidate = _point_candidate(key, kind=kind, title=document.get('title'), url=document.get('url') or '',
                                 observed=observed, content=content, reference=reference,
                                 language=normalize_language(declared, provider=provider))
    return _material(candidate, content=content, content_kind=content_kind, provider=provider,
                     country=document.get('country', ''), published_at=document.get('published_at', ''),
                     article_key=article_key)


def _gkg_material(storage_root, key, cutoff):
    from ..digest.archive import read_gkg_reference

    row = read_gkg_reference(storage_root, key, cutoff=cutoff)
    candidate = _point_candidate(key, kind='gkg', title=row['title'], url=row['url'],
                                 observed=row['observed_at'], reference=row['source_ref'],
                                 language=normalize_language(row.get('source_language', ''), provider='gkg'))
    return _material(candidate, provider='gkg')


def _gdelt_material(session, key, event_id, cutoff):
    from ..intake.official_models import OfficialEvent
    from ..selection.service import detail

    if not event_id.isascii() or not event_id.isdigit() or len(event_id) > 30:
        raise KeyError('未找到可用的来源候选')
    event = session.get(OfficialEvent, event_id)
    if event is None or (cutoff and (event.ingested_at >= cutoff or event.last_seen >= cutoff)):
        raise KeyError('未找到可用的来源候选')
    card = detail(session, event_id)['event']
    preview = card.get('report_preview') or {}
    reconstruction = card.get('reconstruction') or {}
    content = preview.get('excerpt') or reconstruction.get('excerpt') or ''
    declared = preview.get('language') or reconstruction.get('language') or ''
    reference = dict(card['source_ref'], event_id=event_id, provider='gdelt', source_language=declared)
    url = preview.get('source_url') or reconstruction.get('url') or card.get('source_url') or ''
    candidate = _point_candidate(key, kind='gdelt', title=preview.get('title') or card['coded_title'],
                                 url=url, observed=event.last_seen or event.ingested_at, content=content,
                                 reference=reference, language=normalize_language(declared, provider='gdelt'))
    return _material(candidate, content=content, provider='gdelt', country=card.get('action_country', ''),
                     content_kind='excerpt' if preview.get('excerpt') else
                     'reconstruction' if reconstruction.get('excerpt') else 'metadata',
                     published_at=preview.get('published_at', ''))


def _newsnow_material(session, key, topic_key, cutoff):
    from ..attention.models import AttentionItem, AttentionTopic

    topic = session.get(AttentionTopic, topic_key)
    if topic is None or (cutoff and topic.created >= cutoff):
        raise KeyError('未找到可用的来源候选')
    query = select(AttentionItem).where(AttentionItem.topic == topic_key)
    if cutoff:
        query = query.where(AttentionItem.last_seen < cutoff)
    rows = session.exec(query.order_by(AttentionItem.last_seen.desc(), AttentionItem.key.desc()).limit(30))
    for row in rows:
        if not usable_title(row.title) or not _readable_url(row.url):
            continue
        declared = getattr(row, 'language', '')
        reference = dict(json.loads(row.snapshot_ref_json), provider='newsnow', attention_topic=topic_key,
                         item_key=row.key, source_language=declared)
        candidate = _point_candidate(key, kind='newsnow', title=row.title, url=row.url,
                                     observed=row.last_seen, content=row.excerpt, reference=reference,
                                     language=normalize_language(declared, provider='newsnow'))
        return _material(candidate, content=row.excerpt, content_kind='summary' if row.excerpt else 'metadata')
    raise KeyError('未找到可用的来源候选')


def resolve_material(session: Session, storage_root, key: str, *, cutoff: datetime | None = None) -> dict:
    """Load only the source named by this key. Missing/malformed keys cannot create material."""
    if not isinstance(key, str) or not _KEY.fullmatch(key) or ':' not in key:
        raise KeyError('未找到可用的来源候选')
    kind, ident = key.split(':', 1)
    end = _stamp(cutoff) if cutoff else ''
    if kind == 'report':
        return _stored_material(session, key, end)
    if kind == 'gkg' and storage_root is not None:
        return _gkg_material(storage_root, key, end)
    if kind in ('local', 'article'):
        return _article_material(session, key, kind, ident, end)
    if kind == 'gdelt':
        return _gdelt_material(session, key, ident, end)
    if kind == 'newsnow':
        return _newsnow_material(session, key, ident, end)
    raise KeyError('未找到可用的来源候选')


def resolve_candidate(session: Session, storage_root, key: str, *, cutoff: datetime | None = None) -> Candidate:
    return resolve_material(session, storage_root, key, cutoff=cutoff)['candidate']
