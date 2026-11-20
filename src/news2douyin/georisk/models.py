"""Compact query state backed by locally derived statistics."""
from sqlmodel import Field, SQLModel

from ..storage.models import utc_now_iso


class GeoriskDay(SQLModel, table=True):
    config_key: str = Field(primary_key=True)
    day: str = Field(primary_key=True)
    available_batches: int = 0
    archived_batches: int = 0
    rejected_rows: int = 0
    groups_json: str = '{}'
    evidence_json: str = '{}'
    updated_at: str = Field(default_factory=utc_now_iso)


class GeoriskCoverage(SQLModel, table=True):
    config_key: str = Field(primary_key=True)
    archived_by_day_json: str = '{}'
