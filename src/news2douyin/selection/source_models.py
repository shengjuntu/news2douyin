"""Small source-reading previews, separate from researched event assertions."""
from sqlalchemy import Index
from sqlmodel import SQLModel, Field
from ..storage.models import utc_now_iso


class SourcePreview(SQLModel, table=True):
    key: str = Field(primary_key=True)
    source_url: str
    final_url: str = ''
    title: str = ''
    excerpt: str = ''
    language: str = ''
    published_at: str = ''
    domain: str = ''
    origin: str = 'source_page'
    title_origin: str = ''
    excerpt_origin: str = ''
    article_key: str = ''
    content_hash: str = ''
    fetched_at: str = Field(default_factory=utc_now_iso)
    material_kind: str = 'source_excerpt'


class SourcePreviewJob(SQLModel, table=True):
    __table_args__ = (Index('ix_sourcepreviewjob_due', 'state', 'retry_at', 'priority'),
                      Index('ix_sourcepreviewjob_state_updated','state','updated_at'))
    key: str = Field(primary_key=True)
    source_url: str
    state: str = 'pending'
    priority: int = 100
    attempts: int = 0
    basis_hash: str = ''
    retry_at: float = 0
    lease: str = ''
    lease_until: float = 0
    error: str = ''
    error_kind: str = ''
    requested_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
    finished_at: str = ''
