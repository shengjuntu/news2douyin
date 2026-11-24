"""Persistent plans, coverage and immutable publications for historical intake."""
from datetime import date

from sqlalchemy import BigInteger, Index
from sqlmodel import Field, SQLModel


class HistoryControl(SQLModel, table=True):
    key: str = Field(default='archive', primary_key=True)
    paused: bool = False


class HistoryProviderCooldown(SQLModel, table=True):
    provider: str = Field(primary_key=True)
    resume_at: float


class HistoryJob(SQLModel, table=True):
    key: str = Field(primary_key=True)
    date_from: date
    date_to: date
    mentions: bool = False
    state: str = Field(default='planning', index=True)
    case_key: str = Field(default='', index=True)
    node_key: str = ''
    serial: float = 0
    error: str = ''


class HistoryGroup(SQLModel, table=True):
    __table_args__ = (
        Index('ix_historygroup_queue', 'state', 'priority', 'date_to'),
        Index('ix_historygroup_coverage', 'date_to', 'date_from', 'kind'),
    )

    key: str = Field(primary_key=True)
    kind: str
    version: int
    period: str
    date_from: date
    date_to: date
    state: str = 'pending'
    priority: int = 0
    phase: str = ''
    archive_missing: int = 0
    total_files: int = 0
    downloaded_files: int = 0
    ready_files: int = 0
    failed_files: int = 0
    total_bytes: int = Field(default=0, sa_type=BigInteger)
    downloaded_bytes: int = Field(default=0, sa_type=BigInteger)
    ready_bytes: int = Field(default=0, sa_type=BigInteger)
    invalid_rows: int = 0
    error: str = ''
    retry_at: float = 0


class HistoryPart(SQLModel, table=True):
    __table_args__ = (Index('ix_historypart_group', 'group_key', 'state', 'key'),)

    key: str = Field(primary_key=True)
    group_key: str
    stamp: str
    url: str
    size: int = Field(sa_type=BigInteger)
    md5: str
    state: str = 'pending'
    error: str = ''
    publication_path: str = ''


class HistoryPublication(SQLModel, table=True):
    path: str = Field(primary_key=True)
    group_key: str = Field(index=True)


class HistoryVolume(SQLModel, table=True):
    key: str = Field(default='archive', primary_key=True)
    token: str


class HistoryPublicationCheck(SQLModel, table=True):
    path: str = Field(primary_key=True)
    file_signature: str
    invalid_rows: int
