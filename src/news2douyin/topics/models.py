from sqlmodel import SQLModel, Field
from ..storage.models import utc_now_iso


class TopicWorkspace(SQLModel, table=True):
    workspace_id: str = Field(primary_key=True)
    request_hash: str
    case_id: str = Field(index=True, unique=True)
    title: str = '未命名选题'
    angle: str = ''
    scope_json: str = '{}'
    selected_json: str = '[]'
    revision: int = 1
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso, index=True)


class TopicSource(SQLModel, table=True):
    source_id: str = Field(primary_key=True)
    workspace_id: str = Field(index=True)
    article_key: str
    document_json: str
    content_hash: str
    observed_at: str = Field(default_factory=utc_now_iso)


class TopicMessage(SQLModel, table=True):
    request_id: str = ''
    message_id: str = Field(primary_key=True)
    workspace_id: str = Field(index=True)
    request_hash: str
    run_id: str = Field(index=True, unique=True)
    text: str
    context_json: str
    reads_json: str = '[]'
    reply_json: str = ''
    created_at: str = Field(default_factory=utc_now_iso, index=True)


class TopicCandidate(SQLModel, table=True):
    candidate_id: str = Field(primary_key=True)
    workspace_id: str = Field(index=True)
    run_id: str
    document_json: str
    created_at: str = Field(default_factory=utc_now_iso, index=True)


class TopicHandoff(SQLModel, table=True):
    request_id: str = ''
    run_id: str = ''
    handoff_id: str = Field(primary_key=True)
    workspace_id: str = Field(index=True)
    request_hash: str
    payload_json: str
    case_id: str
    created_at: str = Field(default_factory=utc_now_iso, index=True)


class TopicSearch(SQLModel, table=True):
    search_id: str = Field(primary_key=True)
    workspace_id: str = Field(index=True)
    run_id: str = Field(index=True)
    scope_json: str
    result_json: str
    created_at: str = Field(default_factory=utc_now_iso)
