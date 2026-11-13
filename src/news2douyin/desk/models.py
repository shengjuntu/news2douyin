from sqlmodel import SQLModel, Field
from sqlalchemy import Index, text
from ..storage.models import utc_now_iso


class Watch(SQLModel, table=True):
    key: str = Field(primary_key=True)
    name: str = Field(unique=True)
    kind: str
    scope: str
    dimensions: str = ''
    exclude: str = ''
    question: str = ''
    version: int = 1
    enabled: bool = True
    auto_research: bool = False
    review_days: int = 7
    next_review: str = ''
    last_auto_day: str = ''
    created: str = Field(default_factory=utc_now_iso)


class RadarEntry(SQLModel, table=True):
    __table_args__ = (Index('ix_radarentry_read_scope', 'key', 'seen', 'priority', 'band'),)
    key: str = Field(primary_key=True)
    title: str
    seen: str = Field(index=True)
    priority: int = Field(index=True)
    band: str = Field(index=True)
    reason: str
    evidence: str
    headings: str
    material: str
    sources: str
    geography: str


class WatchEvent(SQLModel, table=True):
    watch: str = Field(primary_key=True)
    event: str = Field(primary_key=True, index=True)
    choice: str = 'auto'
    relevance: int = 0
    branch: str = ''
    reason: str = ''
    first_seen: str = Field(default_factory=utc_now_iso)


class DeskDirty(SQLModel, table=True):
    key: str = Field(primary_key=True)


class DeskMeta(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str = ''


class GraphPending(SQLModel, table=True):
    __table_args__ = (Index('ix_graphpending_pending_key','key',
        sqlite_where=text('token <> written'),postgresql_where=text('token <> written')),)
    key: str = Field(primary_key=True)
    token: str
    written: str = ''


class GraphLease(SQLModel, table=True):
    """Short SQL transactions coordinate replayable work across external graph I/O."""
    key: str = Field(primary_key=True)
    owner: str = ''
    until: float = 0.0


class WatchReview(SQLModel, table=True):
    key: str = Field(primary_key=True)
    watch: str = Field(index=True)
    event: str
    question: str
    case_key: str
    automatic: bool = False
    cutoff: str = ''
    scope_version: int = 1
    status: str = 'pending'
    message: str = ''
    created: str = Field(default_factory=utc_now_iso)


class GkgSource(SQLModel, table=True):
    article_key: str = Field(primary_key=True)
    record_id: str
    attributes: str


class EventPlace(SQLModel, table=True):
    key: str = Field(primary_key=True)
    event: str = Field(index=True)
    role: str
    iso: str
    article: str
    revision: int
    quote: str

