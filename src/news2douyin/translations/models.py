from sqlmodel import SQLModel, Field
from ..storage.models import utc_now_iso


class ReadingTranslation(SQLModel, table=True):
    id: str = Field(primary_key=True)
    kind: str
    resource_key: str
    target: str
    scope: str
    revision: int | None = None
    input_hash: str
    original: str
    language_json: str
    truncated: bool = False
    translated_text: str = ''
    task_id: str = Field(index=True)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)
