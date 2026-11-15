from sqlalchemy import Index
from sqlmodel import SQLModel, Field
from ..storage.models import utc_now_iso


class SelectionFilter(SQLModel, table=True):
    key: str = Field(primary_key=True)
    name: str = Field(index=True)
    filters_json: str
    updated: str = Field(default_factory=utc_now_iso)


class SelectionOrigin(SQLModel, table=True):
    __table_args__ = (Index('ix_selectionorigin_event', 'event_id', 'created'),)
    task: str = Field(primary_key=True)
    event_id: str
    request_hash: str
    snapshot_json: str
    created: str = Field(default_factory=utc_now_iso)
