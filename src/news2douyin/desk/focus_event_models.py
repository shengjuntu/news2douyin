"""Durable publication state for a rebuildable GDELT focus graph."""

from sqlalchemy import Index
from sqlmodel import Field, SQLModel


class FocusEventGraph(SQLModel, table=True):
    __table_args__ = (Index('ix_focuseventgraph_due', 'state', 'refresh_at', 'watch'),)

    watch: str = Field(primary_key=True)
    token: str
    scope_version: int
    days: int
    until: str = ''
    state: str = 'queued'
    refresh_at: float = 0
    error: str = ''
    built_at: str = ''
    published_version: int = 0
    graph_name: str = ''
    retiring: str = ''
    snapshot: str = ''
