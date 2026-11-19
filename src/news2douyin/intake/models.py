"""Additive operational state. Raw document records live in the DuckDB lake."""
from sqlmodel import SQLModel, Field
from ..storage.models import utc_now_iso


class FeedState(SQLModel, table=True):
    key: str = Field(primary_key=True)
    enabled: bool = True
    next_batch: str = ''
    last_complete: str = ''
    latest_available: str = ''
    latest_md5: str = ''
    latest_bytes: int = 0
    manifest_checked: float = 0
    last_error: str = ''
    retry_after: float = 0
    attempts: int = 0
    lease: str = ''
    lease_until: float = 0
    updated_at: str = Field(default_factory=utc_now_iso)


class IntakeBatch(SQLModel, table=True):
    key: str = Field(primary_key=True)
    feed: str = Field(index=True)
    stamp: str = Field(index=True)
    state: str = Field(default='pending',index=True)
    rows: int = 0
    valid_rows: int = 0
    invalid_rows: int = 0
    archive_path: str = ''
    sha256: str = ''
    md5: str = ''
    expected_md5: str = ''
    expected_bytes: int = 0
    compressed_bytes: int = 0
    projected_offset: int = 0
    last_error: str = ''
    received_at: str = Field(default_factory=utc_now_iso)
    completed_at: str = ''


class DailyRepairJob(SQLModel, table=True):
    """One durable historical intake task per Beijing day; previous published scope survives retries."""
    day: str = Field(primary_key=True)
    id: str
    status: str = Field(default='queued', index=True)
    feeds_json: str = '[]'
    ready_id: str = ''
    restored_json: str = '[]'
    partial_publish: bool = False
    next_at: float = 0
    last_error: str = ''
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class DailyRepairPart(SQLModel, table=True):
    key: str = Field(primary_key=True)
    day: str = Field(index=True)
    feed: str
    stamp: str
    state: str = 'pending'
    attempts: int = 0
    next_at: float = 0
    error: str = ''
    completed_at: str = ''
