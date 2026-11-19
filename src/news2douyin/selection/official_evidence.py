"""As-of GDELT coded-event/document coverage on the local working index.

Each OfficialMention represents one (GlobalEventID, DocumentIdentifier). The
first and last upstream references can belong to different intake batches.
An old document survives a later update if its first batch is provably visible
by the cutoff; later sentence offsets and confidence are never back-projected.
A domain is a hostname, not a publisher identity or independent verification.
"""
from datetime import date, datetime, timedelta, timezone
import re

from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import aliased
from sqlmodel import Session, select

from ..digest.calendar import BEIJING, day_bounds_utc
from ..intake.models import IntakeBatch
from ..intake.official_models import OfficialEvent, OfficialMention
from ..storage.articles import identity_key
from ..storage.models import ArticleIdentity, ArticleVersion
from ..storage.sql import json_text
from .service import CAMEO_LABELS
from .source_content import source_url

EVENT_BATCH = 256


def _utc(moment):
    return moment.isoformat(timespec='seconds').replace('+00:00', 'Z')


def _visible(event_ids, end, *, detail=False):
    """Name the two *evidenced* upstream endpoints separately.

    The earliest raw pointer may move to an older, late-arriving row during
    backfill. The immutable receipt time prevents it leaking into past days.
    Legacy/imported rows with no batch have only the dated current projection
    to attest the last observation (and the first if both pointers coincide).
    """
    m = OfficialMention
    b = IntakeBatch
    last_batch = aliased(IntakeBatch)
    latest = and_(m.last_seen != '', m.last_seen < end, m.updated_at != '', m.updated_at < end,
                  or_(last_batch.key.is_(None),
                      and_(last_batch.completed_at != '', last_batch.completed_at < end)))
    same = and_(m.first_stamp == m.last_stamp, m.first_feed == m.last_feed,
                m.first_row_index == m.last_row_index)
    first_batch_seen = and_(b.completed_at != '', b.completed_at < end)
    first = and_(m.first_seen != '', m.first_seen < end,
                 or_(first_batch_seen, and_(b.key.is_(None), same, latest)))
    columns = [m.event_id, m.domain, case((first, m.first_seen)).label('first_at'),
               case((latest, m.last_seen)).label('last_at')]
    if detail:
        columns.extend([m.key, m.doc_identifier, m.first_feed, m.first_stamp, m.first_row_index,
                        m.last_feed, m.last_stamp, m.last_row_index, m.confidence,
                        m.sentence_id, m.action_offset, m.actor1_offset, m.actor2_offset])
    return (select(*columns).select_from(m)
            .outerjoin(b, b.key == m.first_feed + '_' + m.first_stamp)
            .outerjoin(last_batch, last_batch.key == m.last_feed + '_' + m.last_stamp)
            .where(m.event_id.in_(event_ids)).subquery())


def coverage_by_event(session: Session, event_ids, start: str, end: str) -> dict:
    """Count verified distinct documents/domains and first sightings in [start,end).

    Timestamps are UTC ISO strings (seconds, Z). The day is based on GDELT
    first mention time, subject to local availability. Unknown first dates do
    not earn novelty credit, even if a later mention is visible. Counts cover
    locally indexed Mentions; the separate Events.SOURCEURL is not a mention.
    """
    if start > end:
        raise ValueError('invalid evidence interval')
    ids = list(dict.fromkeys(event_ids))
    result = {key: dict(documents=0, domains=0, new_documents=0, new_domains=0,
                        history_complete=True) for key in ids}
    for i in range(0, len(ids), EVENT_BATCH):
        rows = _visible(ids[i:i + EVENT_BATCH], end).c
        domains = (select(rows.event_id, rows.domain, func.count().label('docs'),
                          func.count(rows.first_at).label('dated'),
                          func.min(rows.first_at).label('first_at'),
                          func.sum(case((rows.first_at >= start, 1), else_=0)).label('new_docs'))
                   .where(or_(rows.first_at.is_not(None), rows.last_at.is_not(None)))
                   .group_by(rows.event_id, rows.domain).subquery()).c
        new_domain = and_(domains.domain != '', domains.dated == domains.docs,
                          domains.first_at >= start)
        query = (select(domains.event_id, func.sum(domains.docs),
                        func.sum(case((domains.domain != '', 1), else_=0)),
                        func.sum(domains.new_docs), func.sum(case((new_domain, 1), else_=0)),
                        func.sum(domains.dated))
                 .group_by(domains.event_id)
                 .execution_options(n2d_query_name='official.coverage.as_of'))
        for key, docs, outlets, new_docs, new_outlets, dated in session.exec(query):
            result[key] = dict(documents=int(docs), domains=int(outlets),
                               new_documents=int(new_docs), new_domains=int(new_outlets),
                               history_complete=dated == docs)
    return result


def _ref(feed, stamp, row_index):
    return dict(feed=feed, stamp=stamp, row_index=row_index)


def _document_query(ids, end):
    rows = _visible(ids, end, detail=True)
    return select(*rows.c).where(or_(rows.c.first_at.is_not(None), rows.c.last_at.is_not(None)))


def _as_document(row):
    first = bool(row.first_at)
    last = bool(row.last_at)
    return dict(event_id=row.event_id, document=row.doc_identifier, url=source_url(row.doc_identifier),
                domain=row.domain or '', first_seen=row.first_at or '', last_seen=row.last_at or '',
                first_ref=_ref(row.first_feed, row.first_stamp, row.first_row_index) if first else None,
                last_ref=_ref(row.last_feed, row.last_stamp, row.last_row_index) if last else None,
                confidence=row.confidence if last else None,
                sentence_id=row.sentence_id if last else None,
                action_offset=row.action_offset if last else None,
                actor1_offset=row.actor1_offset if last else None,
                actor2_offset=row.actor2_offset if last else None, gkg=None)


def _attach_gkg(session, docs, end):
    """Resolve exact URLs in immutable intake snapshots for this bounded detail page.

    A canonical article id only narrows the search. Match the GKG input URI
    verbatim; a current article title, a near-identical URL, or a GKG person
    appearing elsewhere in the article proves no role in the coded action.
    """
    requested = {doc['document'] for doc in docs if doc['url']}
    if not requested:
        return
    keys = {identity_key({'url': url}) for url in requested}
    raw = json_text(ArticleVersion.payload_json, 'raw')
    uri = json_text(raw, 'uri')
    intake = json_text(raw, 'intake')
    feed = json_text(intake, 'feed')
    # Index lookup per representative URL, never a scan of all GKG or versions.
    articles = select(ArticleIdentity.article_key).where(ArticleIdentity.identity_key.in_(keys))
    versions = (select(func.max(ArticleVersion.id)).where(ArticleVersion.article_key.in_(articles),
                ArticleVersion.origin == 'intake_metadata', ArticleVersion.observed_at != '',
                ArticleVersion.observed_at < end, uri.in_(requested),
                feed.in_(('gkg_english', 'gkg_translation'))).group_by(uri))
    query = (select(uri, ArticleVersion.article_key, ArticleVersion.revision, json_text(raw, 'record_id'),
                    feed, json_text(intake, 'stamp'), json_text(intake, 'row_index'))
             .where(ArticleVersion.id.in_(versions))
             .execution_options(n2d_query_name='official.evidence.gkg'))
    refs = {}
    for url, article, revision, record_id, source_feed, stamp, index in session.exec(query):
        if not isinstance(record_id, str) or not re.fullmatch(r'[0-9]{14}-T?[0-9]+', record_id):
            continue
        if not re.fullmatch(r'[0-9]{14}', str(stamp or '')) or not re.fullmatch(r'[1-9][0-9]{0,8}', str(index or '')):
            continue
        refs[url] = dict(record_id=record_id, article_key=article, revision=revision,
                         **_ref(source_feed, stamp, int(index)))
    for doc in docs:
        doc['gkg'] = refs.get(doc['document'])


def official_evidence(session: Session, event_id: str, day: date | None = None, *,
                      now: datetime | None = None, limit: int = 15) -> dict | None:
    """Read a small evidential neighborhood of one official id as of a Beijing day."""
    now = now if now is not None else datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError('now must include a timezone')
    today = now.astimezone(BEIJING).date()
    day = day if day is not None else today
    if day > today or not 1 <= limit <= 50:
        raise ValueError('请选择已观测的日期和 1–50 条关联文档')
    begin, close = day_bounds_utc(day)
    # Local and upstream clocks are stored at whole-second precision. For a
    # live request, include the current second; for a past day, exclude midnight.
    horizon = now.astimezone(timezone.utc).replace(microsecond=0)
    end = _utc(min(close, horizon + timedelta(seconds=1)) if horizon < close else close)
    event = session.exec(select(OfficialEvent).outerjoin(IntakeBatch,
            IntakeBatch.key == OfficialEvent.feed + '_' + OfficialEvent.stamp).where(
            OfficialEvent.event_id == event_id, OfficialEvent.ingested_at != '',
            OfficialEvent.ingested_at < end, OfficialEvent.added_at < end,
            or_(IntakeBatch.key.is_(None), and_(IntakeBatch.completed_at != '',
                                                IntakeBatch.completed_at < end)))).first()
    if event is None:
        return None
    coverage = coverage_by_event(session, [event_id], _utc(begin), end)[event_id]
    query = _document_query([event_id], end)
    rows = query.selected_columns
    query = query.order_by(func.coalesce(rows.last_at, rows.first_at).desc(), rows.key).limit(limit)
    docs = [_as_document(row) for row in session.exec(query.execution_options(
        n2d_query_name='official.evidence.detail_documents'))]
    _attach_gkg(session, docs, end)
    return dict(event_id=event_id, day=day.isoformat(), cutoff=end, event_date=event.event_date,
                coverage_time_basis='gdelt_first_mention_before_local_cutoff',
                first_seen=event.first_seen if event.first_seen < end else '',
                cameo_code=event.event_code, cameo_base=event.event_base_code, cameo_root=event.event_root_code,
                cameo_label=CAMEO_LABELS.get(event.event_root_code, ''),
                actor1=dict(name=event.actor1_name, country=event.actor1_country, iso2=event.actor1_country_iso2),
                actor2=dict(name=event.actor2_name, country=event.actor2_country, iso2=event.actor2_country_iso2),
                action_place=dict(name=event.action_place, country=event.action_country, iso2=event.action_country_iso2),
                source_ref=_ref(event.feed, event.stamp, event.row_index), source_url=source_url(event.source_url),
                coverage=coverage, documents=docs, documents_limited=coverage['documents'] > len(docs))
