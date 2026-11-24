"""Explicit, bounded requests for the existing statement extraction worker."""

from sqlalchemy import case, func, tuple_
from sqlalchemy.orm import aliased
from sqlmodel import select

from ..extraction.models import VersionReading
from ..extraction.service import FOCUS_NEW, focus_material_note
from ..storage.articles import lock_news
from ..storage.models import ArticleEventLink, ArticleVersion
from .focus_lifecycle import require_active
from .focus_read import _membership
from .people_coverage import can_check_for_repair, version_coverage_columns
from .people_view import _material_sources, _window

LIMIT = 32
PRECHECK_CHARS = 65_536


def _state(version):
    state = version['reading_state']
    if can_check_for_repair(state, version['reading_method'], version['any_mentions'], version['failure_record']):
        return 'eligible'
    if state == 'error' or (state is None and version['failure_record']):
        return 'failed'
    if state in ('baseline', FOCUS_NEW, 'rule_running', 'rule_wait', 'model_queued', 'model_running', 'model_wait'):
        return 'pending'
    if state == 'ready' or (state is None and version['any_mentions']):
        return 'done'
    return 'unavailable'


def request_missing(session, watch, *, days, until):
    begin, end = _window(days, until)
    lock_news(session, 'watch:'+watch)
    require_active(session, watch)
    sources, checked, _ = _material_sources(session, watch, begin, end)
    ids = {source['version_id'] for source in sources}
    counts = dict(queued=0, pending=0, remaining=0, done=0, failed=0, unavailable=0, worker_checks=0, limit=LIMIT)
    eligible = []
    for version in checked.values():
        ident = version['version_id']
        if ident not in ids:
            continue
        state = _state(version)
        if state == 'eligible':
            eligible.append(ident)
        else:
            counts[state] += 1
    selected = sorted(eligible)[:LIMIT]
    counts['remaining'] = len(eligible) - len(selected)
    if not selected:
        session.commit()
        return counts

    # Fence concurrent model/review requests and worker claims. A duplicate
    # POST for a second focus must not reset a running lease or a completed job.
    lock_news(session, *('extract-version/'+str(ident) for ident in selected))
    jobs = {job.version_id: job for job in session.exec(select(VersionReading)
            .where(VersionReading.version_id.in_(selected)).order_by(VersionReading.version_id)
            .with_for_update())}
    pairs = {(source['event_key'], source['article_key']) for source in sources if source['version_id'] in selected}
    event_member = _membership(watch).subquery()
    linked = (select(ArticleEventLink.id).where(ArticleEventLink.article_key == ArticleVersion.article_key,
              tuple_(ArticleEventLink.event_key, ArticleEventLink.article_key).in_(sorted(pairs)),
              ArticleEventLink.event_key.in_(select(event_member.c.key))).correlate(ArticleVersion).exists())
    newer = aliased(ArticleVersion)
    current = ~select(newer.id).where(newer.article_key == ArticleVersion.article_key,
                                      newer.revision > ArticleVersion.revision).correlate(ArticleVersion).exists()
    # Large immutable bodies stay out of the web process. The rule worker
    # checks them after claiming the version, before invoking either reader.
    snippet = case((func.length(ArticleVersion.payload_json) <= PRECHECK_CHARS, ArticleVersion.payload_json))
    query = (select(ArticleVersion.id, snippet.label('payload'), *version_coverage_columns(ArticleVersion.id))
             .where(ArticleVersion.id.in_(selected), linked, current).order_by(ArticleVersion.id)
             .execution_options(yield_per=1))
    examined = 0
    for row in session.exec(query):
        examined += 1
        state = _state(row._mapping)
        if state != 'eligible':
            counts[state] += 1
            continue
        job = jobs.get(row.id) or VersionReading(version_id=row.id)
        note = focus_material_note(row.payload) if row.payload is not None else ''
        if note:
            job.state, job.note = 'metadata_only', note
            counts['unavailable'] += 1
        else:
            # Preserve any existing explicit request for a model pass after
            # the baseline. The worker still honors the configured reader mode.
            job.state = FOCUS_NEW
            counts['worker_checks'] += row.payload is None
            counts['queued'] += 1
        session.add(job)
    counts['unavailable'] += len(selected) - examined
    session.commit()
    return counts
