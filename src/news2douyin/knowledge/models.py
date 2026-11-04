from sqlmodel import SQLModel, Field
from ..storage.models import utc_now_iso


class GraphMetric(SQLModel, table=True):
    event: str = Field(primary_key=True)
    generation: str
    score: float = Field(index=True)
    community: str = Field(index=True)
    data: str


class GraphRelation(SQLModel, table=True):
    key: str = Field(primary_key=True)
    source: str = Field(index=True)
    target: str = Field(index=True)
    kind: str
    weight: float
    evidence: str
    generation: str


class FocusChange(SQLModel, table=True):
    key: str = Field(primary_key=True)
    watch: str = Field(index=True)
    event: str
    kind: str
    text: str
    created: str = Field(default_factory=utc_now_iso, index=True)


class FocusVisit(SQLModel, table=True):
    watch: str = Field(primary_key=True)
    seen: str = ''


class PointMaterial(SQLModel, table=True):
    key: str = Field(primary_key=True)
    point: str = Field(index=True)
    event: str = Field(index=True)
    reason: str
    generation: str
