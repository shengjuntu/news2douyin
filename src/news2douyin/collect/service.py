from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from loguru import logger
from sqlalchemy import inspect
from sqlmodel import Session, select

from .profiles import PROFILE_FIELDS, normalize_profile_dict, load_profile_file
from .providers.mock import fetch_mock_news
from .providers.worldnewsapi import fetch_news as fetch_worldnewsapi
from ..dedup.service import decide_duplicate
from ..enrich.service import enrich_item
from ..storage.models import CollectProfile, RunRecord, Article, Event, ArticleEventLink, ArticleIdentity, EventAssignment, ArticleVersion, CollectedObservation, utc_now_iso
from ..storage.articles import lock_news, identity_key, find_article, record_version, latest_version
from ..events.service import choose_event, refresh_event
from ..storage.utils import dumps, loads
from ..store.io_jsonl import write_json, write_jsonl

PROVIDERS = {
    'worldnewsapi': fetch_worldnewsapi,
    'mock': fetch_mock_news,
}


def row_to_profile(row: CollectProfile) -> dict[str, Any]:
    # Normalize legacy nested extras before applying the authoritative columns.
    extras = normalize_profile_dict(loads(row.extra_json, {}))
    return normalize_profile_dict(
        {
            **extras,
            'name': row.name,
            'provider': row.provider,
            'country': row.country,
            'language': row.language,
            'categories': loads(row.categories_json, []),
            'keywords_include': loads(row.keywords_include_json, []),
            'keywords_exclude': loads(row.keywords_exclude_json, []),
            'source_whitelist': loads(row.whitelist_domains_json, []),
            'source_blacklist': loads(row.blacklist_domains_json, []),
            'max_items': row.max_items,
            'market_scope': row.market_scope,
            'market_tags': loads(row.market_tags_json, []),
        }
    )


def get_profile(session: Session, profile_name: str) -> dict[str, Any]:
    row = session.exec(select(CollectProfile).where(CollectProfile.name == profile_name)).first()
    if not row:
        raise KeyError(f'profile not found: {profile_name}')
    return row_to_profile(row)


def create_or_update_profile(session: Session, profile: dict[str, Any]) -> CollectProfile:
    profile = normalize_profile_dict(profile)
    lock_news(session)
    row = session.exec(select(CollectProfile).where(CollectProfile.name == profile['name'])).first()
    now = utc_now_iso()
    if not row:
        row = CollectProfile(name=profile['name'])
        session.add(row)
    row.provider = profile['provider']
    row.country = profile['country']
    row.language = profile['language']
    row.categories_json = dumps(profile.get('categories', []))
    row.keywords_include_json = dumps(profile.get('keywords_include', []))
    row.keywords_exclude_json = dumps(profile.get('keywords_exclude', []))
    row.whitelist_domains_json = dumps(profile.get('source_whitelist', []))
    row.blacklist_domains_json = dumps(profile.get('source_blacklist', []))
    row.max_items = int(profile.get('max_items', 100))
    row.market_scope = profile.get('market_scope', profile['country'])
    row.market_tags_json = dumps(profile.get('market_tags', []))
    extra = {k: v for k, v in profile.items() if k not in PROFILE_FIELDS}
    row.extra_json = dumps(extra)
    row.updated_at = now
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def import_profile_file(session: Session, path: str | Path) -> CollectProfile:
    return create_or_update_profile(session, load_profile_file(path))


# Rule filter moved to filters.py; LLM filter (with rule fallback) in llm_filter.py
from .filters import filter_items as _filter_items  # noqa: E402
from .llm_filter import llm_filter_items  # noqa: E402


def _make_run_dir(storage_root: str | Path) -> Path:
    now = datetime.now(timezone.utc)
    run_dir = Path(storage_root) / now.strftime('%Y-%m-%d') / f"run_{now.strftime('%H%M%S')}_{uuid4().hex}"
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _utc_timestamp(value: str, fallback: str) -> str:
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    except (ValueError, TypeError, AttributeError):
        return fallback


def _upsert_event(session: Session, item: dict[str, Any], event_key: str) -> Event:
    row = session.exec(select(Event).where(Event.event_key == event_key).execution_options(populate_existing=True)).first()
    now = utc_now_iso()
    published = _utc_timestamp(item.get('published_at'), now)
    if not row:
        row = Event(
            event_key=event_key,
            event_title=item.get('title',''),
            topic=(item.get('sector_tags') or ['market'])[0] if item.get('sector_tags') else 'market',
            summary=item.get('content','')[:400],
            first_seen_at=published,
            last_seen_at=published,
            importance=float(item.get('market_relevance_score', 0.0)),
            sentiment=item.get('sentiment','neutral'),
            market_scope=item.get('country') or 'global',
            sectors_json=dumps(item.get('sector_tags', [])),
            symbols_json=dumps(item.get('symbols', [])),
            countries_json=dumps(sorted(set([item.get('country','')]))),
            article_count=1,
        )
        session.add(row)
    else:
        row.first_seen_at = min(_utc_timestamp(row.first_seen_at, published), published)
        row.last_seen_at = max(_utc_timestamp(row.last_seen_at, published), published)
        row.article_count += 1
        row.importance = max(row.importance, float(item.get('market_relevance_score', 0.0)))
        if len(item.get('content','')) > len(row.summary or ''):
            row.summary = item.get('content','')[:400]
    session.add(row)
    # The caller commits the article, link and event as one transaction.
    return row


def run_collection(session: Session, profile_name: str, *, storage_root: str | Path = 'runs_v7', trigger_type: str = 'manual', job_id: Optional[int] = None, override: Optional[dict[str, Any]] = None, profile_snapshot: Optional[dict[str, Any]] = None, control=None) -> RunRecord:
    if profile_snapshot is None:
        from .management import require_profile
        lock_news(session)
        require_profile(session, profile_name, enabled=True)
    profile = dict(profile_snapshot) if profile_snapshot is not None else get_profile(session, profile_name)
    if control:
        control.check()
    if override:
        profile.update({k: v for k, v in override.items() if v is not None})
        profile = normalize_profile_dict(profile)
    from ..search.dates import calendar_day, timezone_name
    profile['timezone'] = timezone_name(profile.get('timezone', ''))
    profile['date_str'] = calendar_day(profile.get('date_str', ''), profile['timezone'])
    run_dir = _make_run_dir(storage_root)
    run_key = str(run_dir.relative_to(Path(storage_root))) if Path(storage_root) in run_dir.parents else run_dir.name
    run = RunRecord(run_key=run_key, job_id=job_id, trigger_type=trigger_type, profile_name=profile_name, storage_path=str(run_dir), status='running')
    if control:
        control.attach_run(session, run)
    session.add(run)
    session.commit(); session.refresh(run)

    try:
        provider = profile.get('provider', 'worldnewsapi')
        fetcher = PROVIDERS.get(provider)
        if not fetcher:
            raise KeyError(f'unsupported provider: {provider}')

        if control:
            control.progress('fetching')
        raw_items = control.checkpoint('raw') if control else None
        if raw_items is None:
            raw_items = list(fetcher(profile))
            if control:
                control.save_checkpoint('raw', raw_items)
        if control:
            control.fence(session)
        write_jsonl(run_dir / 'raw.jsonl', raw_items)
        write_json(run_dir / 'meta.json', {'run_key': run_key, 'profile': profile, 'status': 'running'})
        if control:
            session.commit()
        # LLM filter with automatic rule-based fallback (mode: llm | mixed | rules)
        if control:
            control.progress('filtering', 0, len(raw_items))
        saved_filter = control.checkpoint('filtered') if control else None
        diagnostics = {}
        if saved_filter is None:
            filtered, filter_mode = llm_filter_items(raw_items, profile, check_cancel=control.check if control else None,
                                                    diagnostics=diagnostics)
            if control:
                control.save_checkpoint('filtered', {'items': filtered, 'mode': filter_mode, 'diagnostics': diagnostics})
        else:
            filtered, filter_mode = saved_filter['items'], saved_filter['mode']
            diagnostics = saved_filter.get('diagnostics', {})
        if trigger_type == 'profile_test':
            stats = {'dry_run': True, 'fetched': len(raw_items), 'after_filter': len(filtered),
                     'filter_mode': filter_mode, 'stored_articles': 0, 'updated_articles': 0,
                     'skipped_existing': 0, 'duplicate_reports': 0, 'duplicates': 0, 'events_delta': 0,
                     'diagnostics': diagnostics, 'provider': provider, 'profile_name': profile_name,
                     'collection_date': profile['date_str'], 'timezone': profile['timezone']}
            if control:
                control.progress('exporting', len(filtered), len(filtered))
                control.fence(session)
            write_json(run_dir / 'diagnostics.json', diagnostics)
            write_json(run_dir / 'meta.json', {'run_key': run_key, 'stats': stats, 'profile': profile, 'status': 'succeeded'})
            run.status, run.finished_at, run.stats_json = 'succeeded', utc_now_iso(), dumps(stats)
            if control:
                control.complete(session, run)
            session.add(run)
            session.commit(); session.refresh(run)
            return run
        if control:
            control.progress('storing', 0, len(filtered))

        stored_articles = []
        dup_count = 0
        skipped_existing = 0
        updated_articles = 0
        created_events = 0
        scope_id = control.task_id if control else run.run_key

        for index, item in enumerate(filtered):
            if control:
                control.check()
                if control.item_done(session, index):
                    continue
            item = enrich_item(dict(item), profile)
            item['provider'] = provider
            item['country'] = (item.get('country') or profile.get('country', 'us')).lower()
            item['language'] = item.get('language') or profile.get('language', 'en')
            item['published_at'] = _utc_timestamp(item.get('published_at'), '')
            # External fetch/filter work finished before this short write lock.
            lock_news(session)
            if control:
                control.fence(session)
            existing = find_article(session, item)
            article_key = existing.article_key if existing else identity_key(item)[:24]
            decision = decide_duplicate(session, item, before_id=existing.id) if existing else decide_duplicate(session, item)
            assignment = None if existing else choose_event(session, item, decision)
            row = Article(
                article_key=article_key,
                provider=provider,
                url=item.get('url',''),
                canonical_url=decision.normalized.canonical_url,
                title=item.get('title',''),
                content=item.get('content',''),
                source_domain=((item.get('source') or {}).get('domain')) or '',
                published_at=item.get('published_at') or '',
                fetched_at=item.get('fetched_at') or utc_now_iso(),
                language=item.get('language') or profile.get('language', 'en'),
                country=item.get('country') or profile.get('country', 'us'),
                raw_json=dumps(item),
                normalized_title=decision.normalized.normalized_title,
                normalized_content=decision.normalized.normalized_content,
                title_hash=decision.normalized.title_hash,
                content_hash=decision.normalized.content_hash,
                title_signature=decision.normalized.title_signature,
                content_signature=decision.normalized.content_signature,
                category_tags_json=dumps(item.get('category_tags', [])),
                keyword_tags_json=dumps((item.get('sector_tags') or []) + (item.get('symbols') or [])),
                market_relevance_score=float(item.get('market_relevance_score', 0.0)),
                sentiment=item.get('sentiment', 'neutral'),
                is_duplicate=decision.is_duplicate,
                duplicate_of_article_key=decision.duplicate_of_article_key,
                dedup_group_id=decision.dedup_group_id,
                dedup_reason=decision.reason,
                dedup_score=decision.score,
            )
            if existing:
                # Legacy/directly inserted rows get an honest baseline first.
                record_version(session, existing, origin='legacy_baseline')
                session.flush()
                changed = record_version(session, row, run_key=run.run_key)
                if changed:
                    for field in Article.model_fields:
                        if field not in {'id', 'article_key', 'created_at'}:
                            setattr(existing, field, getattr(row, field))
                    session.add(existing)
                    links = session.exec(select(ArticleEventLink).where(ArticleEventLink.article_key == article_key)).all()
                    for link in links:
                        link.relation_type = 'duplicate' if decision.is_duplicate else 'primary'
                        session.add(link)
                        refresh_event(session, link.event_key)
                    updated_articles += 1
                    stored_articles.append(existing)
                    disposition = 'updated'
                else:
                    existing.fetched_at = row.fetched_at
                    session.add(existing)
                    skipped_existing += 1
                    dup_count += 1
                    disposition = 'existing'
                if control:
                    control.record_item(session, index, article_key, disposition,
                                        duplicate=decision.is_duplicate if changed else True)
                if changed and decision.is_duplicate:
                    dup_count += 1
            else:
                disposition = 'stored'
                session.add(row)
                session.flush()
                session.add(ArticleIdentity(identity_key=identity_key(item), article_key=article_key))
                record_version(session, row, run_key=run.run_key)
                session.add(ArticleEventLink(article_key=article_key, event_key=assignment.event_key,
                            relation_type='duplicate' if decision.is_duplicate else 'primary'))
                event = _upsert_event(session, item, assignment.event_key)
                is_new_event = event.id is None
                refresh_event(session, assignment.event_key)
                session.add(EventAssignment(article_key=article_key, event_key=assignment.event_key,
                            reason=assignment.reason, score=assignment.score,
                            matched_article_key=assignment.matched_article_key))
                if control:
                    control.record_item(session, index, article_key, 'stored', decision.is_duplicate, is_new_event)
                created_events += int(is_new_event)
                dup_count += int(decision.is_duplicate)
                stored_articles.append(row)
            session.flush()
            version = latest_version(session, article_key)
            session.add(CollectedObservation(observation_key=f'{scope_id}:{index}', scope_id=scope_id,
                        input_index=index, article_key=article_key, revision=version.revision,
                        disposition=disposition))
            session.commit()
            if control:
                control.progress('storing', index + 1, len(filtered))

        if control:
            ledger = control.items(session)
            stored_articles = [session.exec(select(Article).where(Article.article_key == entry.article_key)).one()
                               for entry in ledger if entry.disposition in {'stored', 'updated'}]
            dup_count = sum(entry.is_duplicate for entry in ledger)
            skipped_existing = sum(entry.disposition == 'existing' for entry in ledger)
            updated_articles = sum(entry.disposition == 'updated' for entry in ledger)
            created_events = sum(entry.event_created for entry in ledger)
            control.progress('exporting', len(filtered), len(filtered))
        stats = {
            'fetched': len(raw_items),
            'after_filter': len(filtered),
            'filter_mode': filter_mode,
            'stored_articles': len(stored_articles) - updated_articles,
            'updated_articles': updated_articles,
            'duplicates': dup_count,
            'duplicate_reports': dup_count - skipped_existing,
            'skipped_existing': skipped_existing,
            'events_delta': created_events,
            'provider': provider,
            'profile_name': profile_name,
            'collection_date': profile['date_str'],
            'timezone': profile['timezone'],
            'diagnostics': diagnostics,
        }
        if control:
            control.fence(session)
        snapshots = session.exec(select(ArticleVersion.payload_json)
            .join(CollectedObservation, (CollectedObservation.article_key == ArticleVersion.article_key) &
                  (CollectedObservation.revision == ArticleVersion.revision))
            .where(CollectedObservation.scope_id == scope_id, CollectedObservation.disposition.in_(['stored', 'updated']))
            .order_by(CollectedObservation.input_index)).all()
        write_jsonl(run_dir / 'articles.jsonl', [json.loads(value)['raw'] for value in snapshots])
        write_json(run_dir / 'diagnostics.json', diagnostics)
        write_json(run_dir / 'meta.json', {'run_key': run_key, 'stats': stats, 'profile': profile, 'status': 'succeeded'})
        run.status = 'succeeded'
        run.finished_at = utc_now_iso()
        run.stats_json = dumps(stats)
        if control:
            control.complete(session, run)
        session.add(run)
        session.commit(); session.refresh(run)
        logger.info(f'run_collection done run_key={run.run_key} stats={stats}')
        return run
    except Exception as e:
        if control:
            session.rollback()
            raise
        try:
            fail_run(session, run, f'{type(e).__name__}: {e}')
            write_json(run_dir / 'meta.json', {'run_key': run_key, 'profile': profile,
                                             'status': 'failed', 'error_text': run.error_text})
        except Exception:
            logger.exception('Could not persist failed run; preserving original exception')
        raise


def fail_run(session: Session, run: RunRecord, error_text: str) -> RunRecord:
    # Reading an expired ORM attribute before rollback can itself raise
    # PendingRollbackError. The identity is available without touching the DB.
    run_id = inspect(run).identity[0]
    session.rollback()
    run = session.get(RunRecord, run_id)
    run.status = 'failed'
    run.finished_at = utc_now_iso()
    run.error_text = error_text[:4000]
    session.add(run)
    session.commit(); session.refresh(run)
    return run
