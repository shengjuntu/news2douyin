from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Optional

from sqlmodel import SQLModel, Field
from sqlalchemy import UniqueConstraint


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


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


class ProfileState(SQLModel, table=True):
    """Added separately so pre-0.7 databases need no table rewrite."""
    name: str = Field(primary_key=True)
    enabled: bool = True
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


class ScheduleOccurrence(SQLModel, table=True):
    """Durable at-most-once claim for a job's scheduled UTC minute.

    New occurrences commit with a TaskRecord. Legacy `claimed` rows are retained
    without replay because their original business effects may already exist.
    """
    occurrence_key: str = Field(primary_key=True)
    job_id: int = Field(index=True)
    scheduled_at: str = Field(index=True)
    status: str = Field(default='claimed', index=True)
    run_id: Optional[int] = None
    error_text: Optional[str] = None
    created_at: str = Field(default_factory=utc_now_iso)
    finished_at: Optional[str] = None


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


class NewsWriteLock(SQLModel, table=True):
    """Serialize short collection writes, including synchronous collectors."""
    name: str = Field(primary_key=True)
    generation: int = 0


class ArticleIdentity(SQLModel, table=True):
    identity_key: str = Field(primary_key=True)
    article_key: str = Field(index=True)


class ArticleVersion(SQLModel, table=True):
    __table_args__ = (UniqueConstraint('article_key', 'revision'),)
    id: Optional[int] = Field(default=None, primary_key=True)
    article_key: str = Field(index=True)
    revision: int
    content_hash: str = Field(index=True)
    payload_json: str
    origin: str = 'collected'
    run_key: Optional[str] = None
    observed_at: str = Field(default_factory=utc_now_iso)


class EventAssignment(SQLModel, table=True):
    article_key: str = Field(primary_key=True)
    event_key: str = Field(index=True)
    reason: str
    score: float = 0
    matched_article_key: Optional[str] = None
    created_at: str = Field(default_factory=utc_now_iso)


class CollectedObservation(SQLModel, table=True):
    observation_key: str = Field(primary_key=True)
    scope_id: str = Field(index=True)
    input_index: int
    article_key: str = Field(index=True)
    revision: int
    disposition: str


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


class DailySelection(SQLModel, table=True):
    selection_key: str = Field(primary_key=True)
    day: str = Field(index=True)
    timezone: str
    article_key: str = Field(index=True)
    active: bool = True
    package_key: Optional[str] = Field(default=None, index=True)
    created_at: str = Field(default_factory=utc_now_iso)


class TaskRecord(SQLModel, table=True):
    task_id: str = Field(primary_key=True)
    profile_name: str = Field(index=True)
    profile_json: str
    request_hash: str
    idempotency_key: Optional[str] = Field(default=None, unique=True, index=True)
    status: str = Field(default='queued', index=True)
    stage: str = 'queued'
    progress_current: int = 0
    progress_total: int = 0
    attempts: int = 0
    max_attempts: int = 3
    trigger_type: str = 'manual'
    job_id: Optional[int] = Field(default=None, index=True)
    occurrence_key: Optional[str] = None
    run_id: Optional[int] = None
    lease_owner: Optional[str] = None
    lease_until: float = Field(default=0, index=True)
    queued_at: float = Field(default_factory=time.time, index=True)
    error_text: Optional[str] = None
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    started_at: Optional[str] = None
    finished_at: Optional[str] = None


class TaskEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    task_id: str = Field(index=True)
    event_type: str
    payload_json: str
    created_at: str = Field(default_factory=utc_now_iso)


class TaskCheckpoint(SQLModel, table=True):
    checkpoint_key: str = Field(primary_key=True)
    task_id: str = Field(index=True)
    payload_json: str


class TaskItem(SQLModel, table=True):
    item_key: str = Field(primary_key=True)
    task_id: str = Field(index=True)
    input_index: int
    article_key: str = Field(index=True)
    disposition: str
    is_duplicate: bool = False
    event_created: bool = False


class ScriptState(SQLModel, table=True):
    package_key: str = Field(primary_key=True)
    current_revision: int = 1
    version: int = 1
    status: str = Field(default='draft', index=True)
    approved_revision: Optional[int] = None
    root_dir: str
    updated_at: str = Field(default_factory=utc_now_iso, index=True)


class ScriptRevision(SQLModel, table=True):
    revision_key: str = Field(primary_key=True)
    package_key: str = Field(index=True)
    revision: int
    payload_json: str
    content_hash: str
    output_dir: str
    change_note: str = ''
    restored_from: Optional[int] = None
    created_at: str = Field(default_factory=utc_now_iso)


class ScriptReviewEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    package_key: str = Field(index=True)
    revision: int
    state_version: int
    action: str
    status: str
    note: str = ''
    reviewer: str = ''
    checks_json: str = '{}'
    created_at: str = Field(default_factory=utc_now_iso)


class ScriptExport(SQLModel, table=True):
    export_key: str = Field(primary_key=True)
    package_key: str = Field(index=True)
    revision: int
    state_version: int
    content_hash: str
    archive_sha256: str
    archive_path: str
    size_bytes: int
    created_at: str = Field(default_factory=utc_now_iso)


class VideoAsset(SQLModel, table=True):
    asset_id: str = Field(primary_key=True)
    package_key: str = Field(index=True)
    kind: str
    original_name: str
    storage_path: str
    sha256: str
    size_bytes: int
    metadata_json: str = '{}'
    created_at: str = Field(default_factory=utc_now_iso)


class VideoProduction(SQLModel, table=True):
    task_id: str = Field(primary_key=True)
    package_key: str = Field(index=True)
    revision: int
    export_key: str
    input_hash: str
    spec_json: str
    result_json: str = '{}'
    created_at: str = Field(default_factory=utc_now_iso)


class EventState(SQLModel, table=True):
    """Additive curation state; old events start at version zero."""
    event_key: str = Field(primary_key=True)
    version: int = 0
    title_override: Optional[str] = None
    summary_override: Optional[str] = None
    merged_into: Optional[str] = Field(default=None, index=True)
    source_notes_json: str = '{}'
    updated_at: str = Field(default_factory=utc_now_iso)


class EventMoment(SQLModel, table=True):
    moment_key: str = Field(primary_key=True)
    event_key: str = Field(index=True)
    title: str
    description: str = ''
    time_kind: str = 'unknown'
    date_start: str = ''
    date_end: str = ''
    certainty: str = 'unknown'
    time_note: str = ''
    reviewed: bool = False
    sources_json: str = '[]'
    deleted: bool = False
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class EventRelation(SQLModel, table=True):
    relation_key: str = Field(primary_key=True)
    from_event: str = Field(index=True)
    to_event: str = Field(index=True)
    kind: str = 'related'
    note: str = ''
    created_at: str = Field(default_factory=utc_now_iso)


class EventActivity(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    event_key: str = Field(index=True)
    operation_key: str = Field(index=True)
    action: str
    payload_json: str
    created_at: str = Field(default_factory=utc_now_iso)


class EventResearch(SQLModel, table=True):
    research_key: str = Field(primary_key=True)
    event_key: str = Field(index=True)
    status: str = 'running'
    query_json: str
    results_json: str = '[]'
    available: int = 0
    imported_json: str = '{}'
    error_text: str = ''
    created_at: str = Field(default_factory=utc_now_iso)
    finished_at: Optional[str] = None


class ScriptGeneration(SQLModel, table=True):
    """Frozen evidence input and one atomic draft result per queued task."""
    task_id: str = Field(primary_key=True)
    event_key: str = Field(index=True)
    input_hash: str
    spec_json: str
    package_key: Optional[str] = Field(default=None, index=True, unique=True)
    created_at: str = Field(default_factory=utc_now_iso)
