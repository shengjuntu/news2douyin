"""Additive durable state for description-based watches and bounded history work.

These are ordinary application tables: SQLite -> PostgreSQL migration copies
them with the same IDs. No pre-existing Watch columns are changed.
"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Index
from ..storage.models import utc_now_iso


class FocusDefinition(SQLModel, table=True):
    watch: str = Field(primary_key=True)
    description: str
    spec_json: str
    method: str = ''
    scope_version: int = 1
    updated: str = Field(default_factory=utc_now_iso)


class FocusBuild(SQLModel, table=True):
    key: str = Field(primary_key=True)
    description: str
    status: str = Field(default='pending', index=True)
    spec_json: str = ''
    method: str = ''
    message: str = ''
    watch: str = ''
    attempts: int = 0
    created: str = Field(default_factory=utc_now_iso)
    updated: str = Field(default_factory=utc_now_iso)


class FocusBackfill(SQLModel, table=True):
    watch: str = Field(primary_key=True)
    scope_version: int
    status: str = Field(default='pending', index=True)
    cursor: str = ''
    through_key: str = ''
    checked: int = 0
    matched: int = 0
    total: int = 0
    error: str = ''
    updated: str = Field(default_factory=utc_now_iso)


class FocusCandidate(SQLModel, table=True):
    __table_args__ = (
        Index('ix_focuscandidate_work', 'status', 'next_attempt', 'created'),
        Index('ix_focuscandidate_scope', 'watch', 'scope_version', 'decision'),
        Index('ix_focuscandidate_event', 'watch', 'event', 'scope_version'),
    )
    key: str = Field(primary_key=True)
    watch: str
    event: str
    scope_version: int
    event_fingerprint: str
    status: str = 'pending'
    decision: str = ''
    reason: str = ''
    recall_json: str = '{}'
    evidence_json: str = '[]'
    questions_json: str = '[]'
    source_versions_json: str = '[]'
    method: str = ''
    attempts: int = 0
    next_attempt: float = 0.0
    created: str = Field(default_factory=utc_now_iso)
    updated: str = Field(default_factory=utc_now_iso)
