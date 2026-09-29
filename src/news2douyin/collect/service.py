from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from loguru import logger
from sqlmodel import Session, select

from .profiles import normalize_profile_dict, load_profile_file
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
    return normalize_profile_dict(
        {
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
            **loads(row.extra_json, {}),
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
    extra = {k: v for k, v in profile.items() if k not in {'name','provider','country','language','categories','keywords_include','keywords_exclude','source_whitelist','source_blacklist','max_items','market_scope','market_tags'}}
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
    now = datetime.utcnow()
    run_dir = Path(storage_root) / now.strftime('%Y-%m-%d') / f"run_{now.strftime('%H%M%S')}"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _select_event_key(item: dict[str, Any], decision) -> str:
    seed = decision.event_seed or decision.normalized.title_signature or decision.normalized.content_signature or item.get('title','')
    country = (item.get('country') or 'us').lower()
    return 'evt_' + stable_hash(country, seed, length=20)


def _upsert_event(session: Session, item: dict[str, Any], event_key: str) -> Event:
    row = session.exec(select(Event).where(Event.event_key == event_key)).first()
    now = utc_now_iso()
    if not row:
        row = Event(
            event_key=event_key,
            event_title=item.get('title',''),
            topic=(item.get('sector_tags') or ['market'])[0] if item.get('sector_tags') else 'market',
            summary=item.get('content','')[:400],
            first_seen_at=item.get('published_at') or now,
            last_seen_at=item.get('published_at') or now,
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
        row.last_seen_at = item.get('published_at') or now
        row.article_count += 1
        row.importance = max(row.importance, float(item.get('market_relevance_score', 0.0)))
        if len(item.get('content','')) > len(row.summary or ''):
            row.summary = item.get('content','')[:400]
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def run_collection(session: Session, profile_name: str, *, storage_root: str | Path = 'runs_v7', trigger_type: str = 'manual', job_id: Optional[int] = None, override: Optional[dict[str, Any]] = None) -> RunRecord:
    profile = get_profile(session, profile_name)
    if override:
        profile.update({k: v for k, v in override.items() if v is not None})
        profile = normalize_profile_dict(profile)
    run_dir = _make_run_dir(storage_root)
    run_key = str(run_dir.relative_to(Path(storage_root))) if Path(storage_root) in run_dir.parents else run_dir.name
    run = RunRecord(run_key=run_key, job_id=job_id, trigger_type=trigger_type, profile_name=profile_name, storage_path=str(run_dir), status='running')
    session.add(run)
    session.commit(); session.refresh(run)

    try:
        provider = profile.get('provider', 'worldnewsapi')
        fetcher = PROVIDERS.get(provider)
        if not fetcher:
            raise KeyError(f'unsupported provider: {provider}')

        items = fetcher(profile)
        raw_items = list(items)
        # LLM filter with automatic rule-based fallback (mode: llm | mixed | rules)
        filtered, filter_mode = llm_filter_items(items, profile)

        stored_articles = []
        dup_count = 0
        skipped_existing = 0
        event_count_before = session.exec(select(Event)).all()
        event_count_before_n = len(event_count_before)

        for item in filtered:
            item = enrich_item(item, profile)
            decision = decide_duplicate(session, item)
            article_key = stable_hash(item.get('url',''), item.get('title',''), item.get('published_at',''), length=24)
            if session.exec(select(Article).where(Article.article_key == article_key)).first() is not None:
                # Already stored (re-fetched article): count as duplicate and skip
                # re-insert to avoid a UNIQUE constraint crash on the deterministic key.
                dup_count += 1
                skipped_existing += 1
                continue
            event_key = _select_event_key(item, decision)
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
            session.add(row)
            session.commit(); session.refresh(row)
            session.add(ArticleEventLink(article_key=article_key, event_key=event_key, relation_type='duplicate' if decision.is_duplicate else 'primary'))
            _upsert_event(session, item, event_key)
            session.commit()
            if decision.is_duplicate:
                dup_count += 1
            stored_articles.append(row)

        stats = {
            'fetched': len(raw_items),
            'after_filter': len(filtered),
            'filter_mode': filter_mode,
            'stored_articles': len(stored_articles),
            'duplicates': dup_count,
            'skipped_existing': skipped_existing,
            'events_delta': max(0, len(session.exec(select(Event)).all()) - event_count_before_n),
            'provider': provider,
            'profile_name': profile_name,
        }
        write_jsonl(run_dir / 'raw.jsonl', raw_items)
        write_jsonl(run_dir / 'articles.jsonl', [json.loads(a.raw_json) for a in stored_articles])
        write_json(run_dir / 'meta.json', {'run_key': run_key, 'stats': stats, 'profile': profile})
        run.status = 'succeeded'
        run.finished_at = utc_now_iso()
        run.stats_json = dumps(stats)
        session.add(run)
        session.commit(); session.refresh(run)
        logger.info(f'run_collection done run_key={run.run_key} stats={stats}')
        return run
    except Exception as e:
        # Mark the run as failed before propagating so RunRecord never stays 'running'.
        fail_run(session, run, f'{type(e).__name__}: {e}')
        raise


def fail_run(session: Session, run: RunRecord, error_text: str) -> RunRecord:
    run.status = 'failed'
    run.finished_at = utc_now_iso()
    run.error_text = error_text[:4000]
    session.add(run)
    session.commit(); session.refresh(run)
    return run
