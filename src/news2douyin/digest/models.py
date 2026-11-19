"""At most one compact editorial edition per Beijing calendar day."""
from sqlalchemy import Index
from sqlmodel import Field, SQLModel


class DailyDigestIssue(SQLModel, table=True):
    __table_args__ = (Index('ix_dailydigestissue_request', 'requested', 'status', 'day'),)
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
