from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlmodel import SQLModel, Field


def utc_now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'


class CollectProfile(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    provider: str = Field(default='worldnewsapi', index=True)
    country: str = Field(default='us', index=True)
    language: str = Field(default='en', index=True)
    categories_json: str = Field(default='[]')
    keywords_include_json: str = Field(default='[]')
    keywords_exclude_json: str = Field(default='[]')
    whitelist_domains_json: str = Field(default='[]')
    blacklist_domains_json: str = Field(default='[]')
    max_items: int = Field(default=100)
    market_scope: str = Field(default='global', index=True)
    market_tags_json: str = Field(default='[]')
    extra_json: str = Field(default='{}')
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class CollectJob(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    enabled: bool = Field(default=True, index=True)
    timezone: str = Field(default='UTC')
    schedule_type: str = Field(default='cron')
    cron_expr: str = Field(default='0 9 * * 1-5')
    profile_name: str = Field(index=True)
    auto_editorial: bool = Field(default=False)
    auto_video: bool = Field(default=False)
    auto_tts: bool = Field(default=False)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None


class RunRecord(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_key: str = Field(index=True, unique=True)
    job_id: Optional[int] = Field(default=None, index=True)
    trigger_type: str = Field(default='manual', index=True)
    profile_name: str = Field(index=True)
    started_at: str = Field(default_factory=utc_now_iso, index=True)
    finished_at: Optional[str] = None
    status: str = Field(default='running', index=True)
    stats_json: str = Field(default='{}')
    error_text: Optional[str] = None
    storage_path: Optional[str] = None
    created_at: str = Field(default_factory=utc_now_iso)


class Article(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    article_key: str = Field(index=True, unique=True)
    provider: str = Field(default='worldnewsapi', index=True)
    url: str = Field(default='')
    canonical_url: str = Field(default='', index=True)
    title: str = Field(default='')
    content: str = Field(default='')
    source_domain: str = Field(default='', index=True)
    published_at: str = Field(default='', index=True)
    fetched_at: str = Field(default_factory=utc_now_iso)
    language: str = Field(default='en', index=True)
    country: str = Field(default='us', index=True)
    raw_json: str = Field(default='{}')

    normalized_title: str = Field(default='')
    normalized_content: str = Field(default='')
    title_hash: str = Field(default='', index=True)
    content_hash: str = Field(default='', index=True)
    title_signature: str = Field(default='', index=True)
    content_signature: str = Field(default='', index=True)
    category_tags_json: str = Field(default='[]')
    keyword_tags_json: str = Field(default='[]')
    market_relevance_score: float = Field(default=0.0, index=True)
    sentiment: str = Field(default='neutral', index=True)
    is_duplicate: bool = Field(default=False, index=True)
    duplicate_of_article_key: Optional[str] = Field(default=None, index=True)
    dedup_group_id: Optional[str] = Field(default=None, index=True)
    dedup_reason: Optional[str] = None
    dedup_score: float = Field(default=0.0)
    created_at: str = Field(default_factory=utc_now_iso)


class Event(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_key: str = Field(index=True, unique=True)
    event_title: str = Field(default='', index=True)
    topic: str = Field(default='', index=True)
    summary: str = Field(default='')
    first_seen_at: str = Field(default_factory=utc_now_iso, index=True)
    last_seen_at: str = Field(default_factory=utc_now_iso, index=True)
    importance: float = Field(default=0.0, index=True)
    sentiment: str = Field(default='neutral', index=True)
    market_scope: str = Field(default='global', index=True)
    sectors_json: str = Field(default='[]')
    symbols_json: str = Field(default='[]')
    countries_json: str = Field(default='[]')
    article_count: int = Field(default=0)
    created_at: str = Field(default_factory=utc_now_iso)


class ArticleEventLink(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    article_key: str = Field(index=True)
    event_key: str = Field(index=True)
    relation_type: str = Field(default='primary', index=True)


class ScriptPackage(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    package_key: str = Field(index=True, unique=True)
    event_key: str = Field(index=True)
    profile_name: str = Field(default='default', index=True)
    created_at: str = Field(default_factory=utc_now_iso, index=True)
    script_text: str = Field(default='')
    script_json: str = Field(default='{}')
    output_dir: str = Field(default='')
    tts_status: str = Field(default='pending', index=True)
