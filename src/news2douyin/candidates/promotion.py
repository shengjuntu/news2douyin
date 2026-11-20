"""The only writer of curated events and their article attachments.

Callers of the staging functions own the transaction. The candidate API
resolves material from storage and commits the event and audit record together.
"""
from __future__ import annotations

from hashlib import sha256
from urllib.parse import urlsplit
from uuid import uuid4

from sqlmodel import select

from ..dedup.normalize import canonicalize_url
from ..dedup.service import decide_duplicate
from ..enrich.service import enrich_item
from ..selection.source_content import source_url, usable_title
from ..storage.articles import find_article, identity_key, latest_version, lock_news, record_version
from ..storage.models import (
    Article, ArticleEventLink, ArticleIdentity, Event, EventActivity, EventAssignment,
    EventState, utc_now_iso,
)
from ..storage.utils import dumps, loads
from ..tasks.control import TaskConflict


def stage_event(session, *, title, summary='', topic='', market_scope='global', key='',
                seen_at=None, manual=True, article=None):
    """Stage an intentionally adopted event with its initial version state."""
    from ..event_embeddings.queue import enqueue_event

    key = key or 'evt_' + uuid4().hex[:20]
    values = dict(event_key=key, event_title=title, summary=summary, topic=topic, market_scope=market_scope)
    if seen_at:
        values.update(first_seen_at=seen_at, last_seen_at=seen_at)
    if article is not None:
        values.update(importance=article.market_relevance_score, sentiment=article.sentiment,
                      countries_json=dumps([article.country] if article.country else []))
    event = Event(**values)
    state = EventState(event_key=key, title_override=title if manual else None,
                       summary_override=summary if manual else None)
    session.add(event)
    session.add(state)
    enqueue_event(session, key)
    return event, state


def attach_articles(session, state, keys, *, note='', origin='manual', sources=None, assignment=None):
    """Apply explicit editorial membership and pin the source version used."""
    from ..events.workbench import ensure_version, linked_keys

    key = state.event_key
    existing = linked_keys(session, key)
    notes = loads(state.source_notes_json, {})
    added = []
    for article_key in dict.fromkeys(keys):
        article = session.exec(select(Article).where(Article.article_key == article_key)).first()
        if not article:
            raise KeyError('新闻不存在：' + article_key)
        if article_key in existing:
            continue
        version = ensure_version(session, article)
        extra = (sources or {}).get(article_key, {})
        session.add(ArticleEventLink(article_key=article_key, event_key=key,
                                     relation_type='duplicate' if article.is_duplicate else 'primary'))
        notes[article_key] = dict(origin=origin, note=note, revision=version.revision,
                                  added_at=utc_now_iso(), **extra)
        if not session.get(EventAssignment, article_key):
            session.add(EventAssignment(article_key=article_key, event_key=key,
                                        **(assignment or {'reason': origin})))
        existing.add(article_key)
        added.append(article_key)
    state.source_notes_json = dumps(notes)
    session.add(state)
    return added


def approve_collected_article(session, article, *, event_key, decision, reason, score=0,
                              matched_article_key=None, topic='', seen_at=None):
    """Reuse the same writer for explicitly enabled GKG compatibility and demos."""
    if decision not in {'demo', 'legacy_auto'}:
        raise ValueError('采集归属必须经过事件整理')
    from ..events.service import refresh_event
    from ..events.workbench import audit, canonical_event, state_for, touch

    lock_news(session, 'event:' + event_key)
    target = canonical_event(session, event_key)
    if target != event_key:
        lock_news(session, 'event:' + target)
    event = session.exec(select(Event).where(Event.event_key == target)).first()
    created = event is None
    if created:
        event, state = stage_event(session, key=target, title=article.title or '待整理的原文报道',
                                  summary=article.content[:400], topic=topic,
                                  market_scope=article.country or 'global', seen_at=seen_at,
                                  manual=False, article=article)
        if decision == 'demo':
            reporting = loads(article.raw_json, {})
            event.sectors_json = dumps(reporting.get('sector_tags', []))
            event.symbols_json = dumps(reporting.get('symbols', []))
    else:
        state = state_for(session, target)
    added = attach_articles(session, state, [article.article_key], origin=decision,
                            assignment=dict(reason=reason, score=score, matched_article_key=matched_article_key))
    if added:
        audit(session, target, 'approve_collected_article',
              dict(decision=decision, reason=reason, score=score, articles=added))
        refresh_event(session, target, touch_version=False)
        touch(session, state)
    return created


def store_article(session, item, *, origin, run_key=None):
    """Preserve existing publisher text, upgrading an earlier lead when adopted."""
    from ..events.workbench import ensure_version
    from ..research.materials import article_document, is_lead, raw_metadata

    url = source_url(item.get('url'))
    if not url or '\\' in url or not usable_title(item.get('title')):
        raise ValueError('来源需要有可读标题和有效的原文网址')
    identity = identity_key(item)
    canonical = canonicalize_url(url)
    existing = find_article(session, item)
    if not existing and item.get('article_key'):
        known = session.exec(select(Article).where(Article.article_key == item['article_key'])).first()
        if known and canonicalize_url(known.url) == canonical:
            existing = known
    if not existing and canonical:
        # Older workbenches may contain articles from before the identity index.
        existing = session.exec(select(Article).where(Article.canonical_url == canonical)
                                .order_by(Article.id).limit(1)).first()
    if existing:
        version = ensure_version(session, existing)
        if not session.get(ArticleIdentity, identity):
            session.add(ArticleIdentity(identity_key=identity, article_key=existing.article_key))
        old_meta = raw_metadata(existing)
        research_meta = old_meta.get('research_meta')
        old_kind = old_meta.get('content_kind') or (
            research_meta.get('content_kind') if isinstance(research_meta, dict) else '')
        old_is_lead = is_lead(article_document(existing)) or old_kind in (
            'summary', 'excerpt', 'reconstruction', 'metadata')
        has_text = item.get('content_kind') == 'text' and bool(item.get('content', '').strip())
        if not has_text or (existing.content.strip() and not old_is_lead):
            return existing, version, 'existing_preserved'

    item = enrich_item(item, {'country': item.get('country', ''), 'categories': []})
    decision = decide_duplicate(session, item, before_id=existing.id) if existing else decide_duplicate(session, item)
    norm = decision.normalized
    fields = dict(
        provider=item['provider'], url=url, canonical_url=norm.canonical_url,
        title=item['title'], content=item.get('content', ''), source_domain=urlsplit(url).hostname or '',
        published_at=item.get('published_at') or '', fetched_at=item.get('fetched_at') or utc_now_iso(),
        country=item.get('country') or '', language=item.get('language') or '', raw_json=dumps(item),
        normalized_title=norm.normalized_title, normalized_content=norm.normalized_content,
        title_hash=norm.title_hash, content_hash=norm.content_hash,
        title_signature=norm.title_signature, content_signature=norm.content_signature,
        category_tags_json=dumps(item.get('category_tags', [])), sentiment=item.get('sentiment', 'neutral'),
        market_relevance_score=item.get('market_relevance_score', 0), is_duplicate=decision.is_duplicate,
        duplicate_of_article_key=decision.duplicate_of_article_key, dedup_group_id=decision.dedup_group_id,
        dedup_reason=decision.reason, dedup_score=decision.score,
    )
    if existing:
        article = existing
        for name, value in fields.items():
            setattr(article, name, value)
    else:
        article = Article(article_key=identity[:24], **fields)
        session.add(ArticleIdentity(identity_key=identity, article_key=article.article_key))
    session.add(article)
    session.flush()
    record_version(session, article, origin=origin, run_key=run_key, provenance_upgrade=bool(existing))
    session.flush()
    version = latest_version(session, article.article_key)
    if item.get('material_kind') == 'candidate_lead':
        from ..extraction.models import VersionReading
        reading = session.get(VersionReading, version.id)
        if reading:
            reading.state = 'metadata_only'
            reading.note = '标题或摘要线索，等待原文'
            session.add(reading)
    return article, version, 'original_text_added' if existing else 'created'


def promote(session, storage_root, candidate_key, *, expected_token, request_id,
            event_key='', expected_version=None, title='', note='', decision='manual'):
    """Adopt a persisted candidate as a new event or evidence for an active event."""
    from .service import candidate_token, resolve_material
    from ..events.workbench import audit, canonical_event, finish, lock_event, workspace

    if not isinstance(request_id, str) or not 8 <= len(request_id) <= 128:
        raise ValueError('整理请求编号须为 8–128 字符')
    if decision != 'manual':
        raise ValueError('请确认采用这条材料')
    if bool(event_key) != (expected_version is not None):
        raise ValueError('加入已有事件时请同时提供事件及其当前版本')
    if len(title) > 250 or len(note) > 1000:
        raise ValueError('标题或整理说明过长')

    operation = 'candidate/' + sha256(request_id.encode()).hexdigest()
    request = dict(candidate=candidate_key, token=expected_token, event_key=event_key,
                   expected_version=expected_version, title=title, note=note, decision=decision)
    lock_news(session, 'promotion:' + operation)
    saved = session.exec(select(EventActivity).where(EventActivity.operation_key == operation,
                         EventActivity.action == 'promote_candidate')).first()
    if saved:
        if loads(saved.payload_json, {}).get('request') != request:
            raise TaskConflict('该请求编号已用于其他整理操作')
        return workspace(session, canonical_event(session, saved.event_key))

    material = resolve_material(session, storage_root, candidate_key)
    candidate = material['candidate']
    if candidate_token(candidate) != expected_token:
        raise TaskConflict('候选材料已变化，请重新打开后采用')
    kind = material.get('content_kind') or 'metadata'
    complete = kind == 'text' and bool((material.get('content') or '').strip())
    item = dict(provider=material['provider'], url=material['url'], title=material['title'],
                content=(material.get('content') or '') if complete else '',
                fetched_at=material['observed_at'], published_at=material.get('published_at') or '',
                country=material.get('country') or '', language=material.get('language') or '',
                candidate_key=candidate_key, source_kind=material['source_kind'], article_key=material.get('article_key'),
                source_ref=material.get('source_ref'), content_kind=kind,
                material_kind='candidate_document' if complete else 'candidate_lead',
                requires_original_text=not complete)
    lock_news(session, 'article:' + identity_key(item))
    chosen_title = title.strip() or material['title'][:250]
    if event_key:
        state = lock_event(session, event_key, expected_version)
    else:
        event, state = stage_event(session, title=chosen_title,
                                   seen_at=item['published_at'] or item['fetched_at'])
        event_key = event.event_key
        state.summary_override = None
    article, version, disposition = store_article(session, item, origin='candidate_promotion')
    reference = dict(candidate_key=candidate_key, provider=material['provider'],
                     source_kind=material['source_kind'], source_ref=material.get('source_ref'),
                     content_kind=kind, requires_original_text=not complete,
                     excerpt=(material.get('content') or '')[:800] if not complete else '',
                     source_url=material['url'], observed_at=material['observed_at'],
                     content_hash=version.content_hash)
    added = attach_articles(session, state, [article.article_key], note=note, origin='candidate',
                            sources={article.article_key: reference})
    if not added and disposition == 'original_text_added':
        notes = loads(state.source_notes_json, {})
        notes[article.article_key] = dict(
            origin='candidate', note=note, revision=version.revision, added_at=utc_now_iso(),
            previous_origin=notes.get(article.article_key), **reference)
        state.source_notes_json = dumps(notes)
        session.add(state)
    audit(session, event_key, 'promote_candidate',
          dict(request=request, material=reference, article_key=article.article_key,
               article_revision=version.revision, disposition=disposition, attached=bool(added),
               action='attach_existing' if request['event_key'] else 'create_event'), operation)
    return finish(session, event_key, state)
