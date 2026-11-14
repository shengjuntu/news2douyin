"""Additive checkpoints for bounded, replayable topic graph projection."""
from sqlalchemy import BigInteger, Index
from sqlmodel import SQLModel, Field
from ..storage.models import utc_now_iso


class GraphFocusState(SQLModel, table=True):
    namespace: str = Field(primary_key=True)
    watch: str = Field(primary_key=True)
    token: str = ''
    scope_version: int = 0
    rebuild_epoch: str = ''
    generation: str = ''
    phase: str = 'adopt'
    cursor: str = ''
    through_key: str = ''
    adopted: bool = False
    adopt_cursor: int = Field(default=0, sa_type=BigInteger)
    adopt_through: int = Field(default=-1, sa_type=BigInteger)
    scanned: int = Field(default=0, sa_type=BigInteger)
    changed: int = Field(default=0, sa_type=BigInteger)
    removed: int = Field(default=0, sa_type=BigInteger)
    batches: int = Field(default=0, sa_type=BigInteger)
    updated: str = Field(default_factory=utc_now_iso)


class GraphFocusReceipt(SQLModel, table=True):
    __table_args__ = (Index('ix_graphfocusreceipt_generation', 'namespace', 'watch', 'generation', 'kind', 'item'),)
    namespace: str = Field(primary_key=True)
    watch: str = Field(primary_key=True)
    kind: str = Field(primary_key=True)
    item: str = Field(primary_key=True)
    locator: str = '{}'
    fingerprint: str = ''
    generation: str = ''
