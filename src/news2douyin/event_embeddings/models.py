from sqlalchemy import Column, Index, Text
from sqlmodel import Field, SQLModel

from ..storage.models import utc_now_iso


class EventEmbeddingJob(SQLModel, table=True):
    __table_args__ = (
        Index('ix_eventembeddingjob_due', 'state', 'available_at', 'event_key'),
        Index('ix_eventembeddingjob_batch_state', 'batch_id', 'state'),
    )

    event_key: str = Field(primary_key=True)
    state: str = 'pending'
    available_at: float = 0.0
    lease: str = ''
    attempts: int = 0
    batch_id: str = ''
    error: str = ''


class EventEmbeddingRecord(SQLModel, table=True):
    event_key: str = Field(primary_key=True)
    profile: str
    model: str
    content_hash: str
    # Text preserves the same scalar on SQLite, PostgreSQL and offline migration.
    embedding: str = Field(sa_column=Column(Text, nullable=False))
    updated_at: str = Field(default_factory=utc_now_iso)


class EventEmbeddingBackfill(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    batch_id: str
    start_date: str
    end_date: str
    lower: str
    upper: str
    cursor: int = 0
    ceiling: int
    scanned: int = 0
    state: str = 'scanning'
