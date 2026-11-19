from sqlmodel import SQLModel, Field
from ..storage.models import utc_now_iso


class ContentTask(SQLModel, table=True):
    key: str = Field(primary_key=True)
    origin_hash: str = ''
    event: str = Field(default='', index=True)
    watch: str = Field(default='', index=True)
    phase: str = 'selection'
    version: int = 1
    created: str = Field(default_factory=utc_now_iso)
    updated: str = Field(default_factory=utc_now_iso, index=True)


class ContentTaskTrash(SQLModel, table=True):
    task: str = Field(primary_key=True)
    version: int = 0
    deleted_at: str = ''


class ContentBinding(SQLModel, table=True):
    key: str = Field(primary_key=True)
    task: str = Field(index=True)
    kind: str = Field(index=True)
    ref: str = Field(index=True)
    created: str = Field(default_factory=utc_now_iso)


class ContentRequest(SQLModel, table=True):
    key: str = Field(primary_key=True)
    task: str
    fingerprint: str
    result: str
