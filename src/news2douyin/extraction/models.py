from sqlmodel import SQLModel, Field


class VersionReading(SQLModel, table=True):
    version_id: int = Field(primary_key=True)
    state: str = Field(index=True)
    lease: str = ''
    until: float = 0.0
    method: str = ''
    model: str = ''
    note: str = ''


class ExtractionAttempt(SQLModel, table=True):
    version_id: int = Field(primary_key=True)
    attempts: int = 0
    method: str = ''


class TextMention(SQLModel, table=True):
    id: str = Field(primary_key=True)
    version_id: int = Field(index=True)
    article: str = Field(index=True)
    revision: int
    signature: str = Field(default='', index=True)
    action_date: str = Field(default='', index=True)
    data: str


class MentionReview(SQLModel, table=True):
    id: str = Field(primary_key=True)
    decision: str


class FocusPoint(SQLModel, table=True):
    key: str = Field(primary_key=True)
    watch: str = Field(index=True)
    parent: str
    text: str
    created: str


class PointReview(SQLModel, table=True):
    ticket: str = Field(primary_key=True)
    point: str = Field(index=True)


class SurveyPacket(SQLModel, table=True):
    case_id: str = Field(primary_key=True)
    data: str
