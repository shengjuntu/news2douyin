"""Compact editorial editions keyed by their Beijing calendar day."""
from sqlalchemy import Index
from sqlmodel import Field, SQLModel


class _Issue(SQLModel):
    day: str = Field(primary_key=True)
    status: str = 'pending'
    requested: bool = False
    generated_at: str = ''
    items_json: str = '[]'
    stats_json: str = '{}'
    # A separate, narrow column keeps evidence snapshots off prior-key reads.
    keys_json: str = '[]'
    lease_token: str = ''
    lease_until: float = 0
    next_retry: float = 0
    error: str = ''


class DailyDigestIssue(_Issue, table=True):
    __table_args__ = (Index('ix_dailydigestissue_request', 'requested', 'status', 'day'),)


class VideoDigestIssue(_Issue, table=True):
    __table_args__ = (Index('ix_videodigestissue_request', 'requested', 'status', 'day'),)
