"""Bounded, document-linked observations; these are never publisher originals."""
from sqlalchemy import Index
from sqlmodel import Field, SQLModel


class NgramTarget(SQLModel, table=True):
    __table_args__ = (
        Index('ix_ngramtarget_due', 'state', 'priority', 'retry_at'),
        Index('ix_ngramtarget_minute', 'minute', 'state'),
        Index('ix_ngramtarget_event_latest', 'event_id', 'state', 'observed_at', 'key'),
        Index('ix_ngramtarget_available', 'state', 'observed_at', 'event_id'),
    )
    key: str = Field(primary_key=True)  # Exact (official event, document) mention key.
    event_id: str = Field(index=True)
    url: str
    actor1: str = ''
    actor2: str = ''
    root_code: str = ''
    reference_feed: str = ''
    reference_stamp: str
    reference_row: int
    minute: str
    minutes_left: int
    state: str = 'pending'
    priority: int = 1
    retry_at: float = 0.
    failures: int = 0
    lease: str = ''
    note: str = ''
    requested_at: str
    observed_at: str = ''  # Local completion. Never substitute an upstream date.
    language: str = ''
    excerpt: str = ''
    method: str = ''
    archive_sha256: str = ''
    archives_url: str = ''
    fragments: int = 0
    anchors_json: str = '[]'


class NgramBudget(SQLModel, table=True):
    day: str = Field(primary_key=True)
    automatic: int = 0
    scans: int = 0
