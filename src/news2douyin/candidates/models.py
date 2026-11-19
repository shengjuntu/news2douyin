"""Compact operational index for observations outside the existing GDELT/NewsNow archives."""
from sqlalchemy import Index
from sqlmodel import Field, SQLModel


class SourceReport(SQLModel, table=True):
    __table_args__ = (
        Index('ix_sourcereport_observed_key', 'observed_at', 'key'),
        Index('ix_sourcereport_provider_observed', 'provider', 'observed_at', 'key'),
        Index('ix_sourcereport_language_observed', 'language', 'observed_at', 'key'),
    )

    key: str = Field(primary_key=True)
    provider: str
    batch: str
    row_index: int
    url: str
    title: str
    excerpt: str = ''
    content: str = ''
    content_kind: str = ''
    language: str = ''
    country: str = ''
    published_at: str = ''
    observed_at: str
    provenance_json: str = '{}'
