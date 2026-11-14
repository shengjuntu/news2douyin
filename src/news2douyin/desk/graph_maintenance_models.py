"""Durable, additive state for replayable graph maintenance.

Native IDs are only maintenance cursors. Application identities remain the
stable ``id`` properties in the graph and the original SQL business tables.
"""
from sqlmodel import SQLModel, Field
from sqlalchemy import BigInteger, Index
from ..storage.models import utc_now_iso


class GraphGcState(SQLModel, table=True):
    __table_args__ = (Index('ix_graphgcstate_scope', 'namespace', 'graph', 'next_due'),)
    key: str = Field(primary_key=True)
    namespace: str
    graph: str
    label: str
    cursor: int = Field(default=0, sa_type=BigInteger)
    through_key: int = Field(default=-1, sa_type=BigInteger)
    cycle: int = Field(default=0, sa_type=BigInteger)
    status: str = 'pending'
    phase: str = 'highwater'
    scanned: int = Field(default=0, sa_type=BigInteger)
    deleted: int = Field(default=0, sa_type=BigInteger)
    cycle_scanned: int = Field(default=0, sa_type=BigInteger)
    cycle_deleted: int = Field(default=0, sa_type=BigInteger)
    last_run: str = ''
    error: str = ''
    next_due: float = 0.0
    updated: str = Field(default_factory=utc_now_iso)


class GraphGcControl(SQLModel, table=True):
    """Namespace-wide operator pause, separate from genuine scan completion.

    Existing checkpoints never need to be edited to stop maintenance. A zero
    resume_at means an indefinite pause; a positive value is a UTC epoch.
    """
    key: str = Field(primary_key=True)
    namespace: str = Field(index=True)
    paused: bool = False
    resume_at: float = 0.0
    updated: str = Field(default_factory=utc_now_iso)
