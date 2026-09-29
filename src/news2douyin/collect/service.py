from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from loguru import logger
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from .profiles import PROFILE_FIELDS, normalize_profile_dict, load_profile_file
from .providers.mock import fetch_mock_news
from .providers.worldnewsapi import fetch_news as fetch_worldnewsapi
from ..dedup.service import decide_duplicate
from ..enrich.service import enrich_item
from ..storage.models import CollectProfile, RunRecord, Article, Event, ArticleEventLink, utc_now_iso
from ..storage.utils import dumps, loads
from ..utils.hashing import stable_hash
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


def _select_event_key(session: Session, item: dict[str, Any], decision) -> str:
    # Prefer the original article's persisted event. Never recalculate its event
    # from a different (title vs. content) signature on a duplicate path.
    chain = []
    key = decision.duplicate_of_article_key
    while key and key not in chain:
        chain.append(key)
        parent = session.exec(select(Article).where(Article.article_key == key)).first()
        key = parent.duplicate_of_article_key if parent else None
    for key in reversed(chain):
        event = session.exec(
            select(Event).join(ArticleEventLink, Event.event_key == ArticleEventLink.event_key)
            .where(ArticleEventLink.article_key == key).order_by(Event.id)
        ).first()
        if event:
            return event.event_key
    country = (item.get('country') or 'us').lower()
    return 'evt_' + stable_hash(country, decision.dedup_group_id, length=20)


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
    profile = dict(profile_snapshot) if profile_snapshot is not None else get_profile(session, profile_name)
    if control:
        control.check()
    if override:
        profile.update({k: v for k, v in override.items() if v is not None})
        profile = normalize_profile_dict(profile)
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
        if saved_filter is None:
            filtered, filter_mode = llm_filter_items(raw_items, profile, check_cancel=control.check) if control else llm_filter_items(raw_items, profile)
            if control:
                control.save_checkpoint('filtered', {'items': filtered, 'mode': filter_mode})
        else:
            filtered, filter_mode = saved_filter['items'], saved_filter['mode']
        if control:
            control.progress('storing', 0, len(filtered))

        stored_articles = []
        dup_count = 0
        skipped_existing = 0
        created_events = 0

        for index, item in enumerate(filtered):
            if control:
                control.check()
                if control.item_done(session, index):
                    continue
            item = enrich_item(dict(item), profile)
            article_key = stable_hash(item.get('url',''), item.get('title',''), item.get('published_at',''), length=24)
            if session.exec(select(Article).where(Article.article_key == article_key)).first() is not None:
                # Already stored (re-fetched article): count as duplicate and skip
                # re-insert to avoid a UNIQUE constraint crash on the deterministic key.
                dup_count += 1
                skipped_existing += 1
                if control:
                    control.fence(session)
                    control.record_item(session, index, article_key, 'existing', duplicate=True)
                    session.commit()
                    control.progress('storing', index + 1, len(filtered))
                continue
            decision = decide_duplicate(session, item)
            if control:
                control.fence(session)
            event_key = _select_event_key(session, item, decision)
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
            try:
                session.add(row)
                session.flush()
                session.add(ArticleEventLink(article_key=article_key, event_key=event_key, relation_type='duplicate' if decision.is_duplicate else 'primary'))
                event = _upsert_event(session, item, event_key)
                is_new_event = event.id is None
                if control:
                    control.record_item(session, index, article_key, 'stored', decision.is_duplicate, is_new_event)
                session.commit()
            except IntegrityError:
                session.rollback()
                # Another collector can win the deterministic article-key race.
                # Only suppress that known race, never unrelated DB failures.
                if session.exec(select(Article).where(Article.article_key == article_key)).first() is None:
                    raise
                dup_count += 1
                skipped_existing += 1
                if control:
                    control.fence(session)
                    control.record_item(session, index, article_key, 'existing', duplicate=True)
                    session.commit()
                    control.progress('storing', index + 1, len(filtered))
                continue
            created_events += int(is_new_event)
            if decision.is_duplicate:
                dup_count += 1
            stored_articles.append(row)
            if control:
                control.progress('storing', index + 1, len(filtered))

        if control:
            ledger = control.items(session)
            stored_articles = [session.exec(select(Article).where(Article.article_key == entry.article_key)).one()
                               for entry in ledger if entry.disposition == 'stored']
            dup_count = sum(entry.is_duplicate for entry in ledger)
            skipped_existing = sum(entry.disposition == 'existing' for entry in ledger)
            created_events = sum(entry.event_created for entry in ledger)
            control.progress('exporting', len(filtered), len(filtered))
        stats = {
            'fetched': len(raw_items),
            'after_filter': len(filtered),
            'filter_mode': filter_mode,
            'stored_articles': len(stored_articles),
            'duplicates': dup_count,
            'skipped_existing': skipped_existing,
            'events_delta': created_events,
            'provider': provider,
            'profile_name': profile_name,
        }
        if control:
            control.fence(session)
        write_jsonl(run_dir / 'articles.jsonl', [json.loads(a.raw_json) for a in stored_articles])
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
