"""Additive durable state for description-based watches and bounded history work.

These are ordinary application tables: SQLite -> PostgreSQL migration copies
them with the same IDs. No pre-existing Watch columns are changed.
"""
from sqlmodel import SQLModel, Field
from sqlalchemy import Index
from ..storage.models import utc_now_iso


LOCAL_METHOD = 'local'


class FocusDefinition(SQLModel, table=True):
    watch: str = Field(primary_key=True)
    description: str
    spec_json: str
    method: str = ''
    scope_version: int = 1
    updated: str = Field(default_factory=utc_now_iso)


class FocusTrash(SQLModel, table=True):
    """Recoverable topic removal; shared evidence and research keep their IDs."""
    watch: str = Field(primary_key=True)
    name: str
    deleted_at: str = Field(default_factory=utc_now_iso)
    restored_at: str = ''
    previous_enabled: bool = False
    previous_auto_research: bool = False
    previous_scope_version: int = 0
    restored_version: int = 0


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


class FocusLead(SQLModel, table=True):
    """A match against a source observation, independent of local event membership."""

    __table_args__ = (
        Index('ix_focuslead_scope', 'watch', 'scope_version', 'observed_at', 'key'),
        Index('ix_focuslead_work', 'status', 'next_attempt', 'observed_at', 'key'),
        Index('ix_focuslead_judge_day', 'judge_day', 'watch'),
    )
    key: str = Field(primary_key=True)
    watch: str
    scope_version: int
    candidate: str
    observed_at: str
    title: str
    url: str
    provider: str
    language: str = ''
    excerpt: str = ''
    recall_json: str = '{}'
    reason: str = ''
    status: str = 'done'
    decision: str = 'pending'
    evidence_json: str = '[]'
    judge_day: str = ''
    attempts: int = 0
    next_attempt: float = 0.0
