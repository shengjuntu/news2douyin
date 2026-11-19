"""Compact source-event index, separate from local research Event identities.

Original observations are immutable in the lake/ZIP and their intake receipts.
The SQL mention index collapses repeated (event, document) observations; it is
not a replacement for the complete GDELT Mentions archive.
"""
from sqlalchemy import Index
from sqlmodel import SQLModel, Field
from ..storage.models import utc_now_iso


class OfficialEvent(SQLModel, table=True):
    __table_args__ = (
        Index('ix_officialevent_root_recent', 'event_root_code', 'last_seen', 'event_id'),
        Index('ix_officialevent_country_recent', 'action_country_iso2', 'last_seen', 'event_id'),
    )
    event_id: str = Field(primary_key=True)
    event_date: str = Field(index=True)
    added_at: str = ''
    first_seen: str = ''
    last_seen: str = Field(default='', index=True)
    actor1_name: str = ''
    actor2_name: str = ''
    actor1_country: str = ''
    actor2_country: str = ''
    actor1_country_iso2: str = ''
    actor2_country_iso2: str = ''
    event_code: str = ''
    event_base_code: str = ''
    event_root_code: str = ''
    quad_class: int | None = None
    goldstein: float | None = None
    initial_mentions: int | None = None
    initial_sources: int | None = None
    initial_articles: int | None = None
    avg_tone: float | None = None
    is_root: bool = False
    action_country: str = ''
    action_country_iso2: str = ''
    action_place: str = ''
    latitude: float | None = None
    longitude: float | None = None
    source_url: str = ''
    feed: str
    stamp: str
    row_index: int
    ingested_at: str = Field(default_factory=utc_now_iso)


class OfficialMention(SQLModel, table=True):
    __table_args__ = (Index('ix_officialmention_event_recent', 'event_id', 'last_seen'),)
    key: str = Field(primary_key=True)
    event_id: str = Field(index=True)
    doc_identifier: str
    domain: str = Field(default='', index=True)
    source_name: str = ''
    mention_type: int | None = None
    event_time: str = ''
    first_seen: str = ''
    last_seen: str = Field(default='', index=True)
    confidence: int | None = None
    sentence_id: int | None = None
    actor1_offset: int | None = None
    actor2_offset: int | None = None
    action_offset: int | None = None
    in_raw_text: bool | None = None
    doc_length: int | None = None
    doc_tone: float | None = None
    first_feed: str
    first_stamp: str
    first_row_index: int
    last_feed: str
    last_stamp: str
    last_row_index: int
    updated_at: str = Field(default_factory=utc_now_iso)


class OfficialFocusUpdate(SQLModel, table=True):
    """Recent availability clock for the compact official event projection."""

    event_id: str = Field(primary_key=True)
    available_at: str = Field(index=True)
