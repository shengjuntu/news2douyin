from typing import Optional

from sqlmodel import SQLModel, Field

from ..storage.models import utc_now_iso


class ResearchLock(SQLModel, table=True):
    name: str = Field(primary_key=True)
    revision: int = 0


class ResearchStrategy(SQLModel, table=True):
    strategy_id: str = Field(primary_key=True)
    name: str
    config_json: str
    revision: int = 1
    enabled: bool = True


class ResearchCase(SQLModel, table=True):
    case_id: str = Field(primary_key=True)
    title: str
    goal: str
    article_keys_json: str = '[]'
    event_key: str = ''
    strategy_json: str
    date_from: str = ''
    cutoff: str
    active_run_id: str = ''
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso, index=True)


class ResearchRun(SQLModel, table=True):
    run_id: str = Field(primary_key=True)
    case_id: str = Field(index=True)
    status: str = Field(default='queued', index=True)
    session_id: str = ''
    instance_id: str = ''
    workspace_id: str = ''
    dispatch_done: bool = False
    cursor: int = 0
    search_count: int = 0
    read_count: int = 0
    instruction: str = ''
    cutoff: str = ''
    error: str = ''
    started_at: str = Field(default_factory=utc_now_iso)
    finished_at: str = ''
    last_sync_at: str = ''


class ResearchSubmission(SQLModel, table=True):
    # Separate additive table: upgrading does not rewrite historical run rows.
    run_id: str = Field(primary_key=True)
    backend_url: str
    session_key: str = ''
    turn_key: str = ''
    session_payload: str = '{}'
    turn_payload: str = '{}'
    turn_attempted: bool = False
    rundesk_run_id: str = ''
    legacy: bool = False


class ResearchQuestion(SQLModel, table=True):
    question_id: str = Field(primary_key=True)
    case_id: str = Field(index=True)
    key: str
    text: str
    status: str = 'open'
    answer: str = ''
    claim_ids_json: str = '[]'
    updated_at: str = Field(default_factory=utc_now_iso)


class ResearchSource(SQLModel, table=True):
    source_id: str = Field(primary_key=True)
    case_id: str = Field(index=True)
    title: str
    url: str = ''
    kind: str
    content_hash: str
    paragraphs_json: str
    published_at: str = ''
    retrieved_at: str = Field(default_factory=utc_now_iso)
    provenance_json: str = '{}'


class ResearchClaim(SQLModel, table=True):
    claim_id: str = Field(primary_key=True)
    case_id: str = Field(index=True)
    run_id: str
    text: str
    kind: str = 'fact'
    evidence_json: str = '[]'
    occurred_at: str = ''
    review_status: str = 'draft'
    created_at: str = Field(default_factory=utc_now_iso)


class ResearchReport(SQLModel, table=True):
    report_id: str = Field(primary_key=True)
    case_id: str = Field(index=True)
    run_id: str
    version: int
    title: str
    sections_json: str
    gaps_json: str
    completeness: str
    cutoff: str = ''
    content_hash: str
    created_at: str = Field(default_factory=utc_now_iso)


class ResearchActivity(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    case_id: str = Field(index=True)
    run_id: str = ''
    kind: str
    message: str
    request_id: str = Field(default='', index=True)
    created_at: str = Field(default_factory=utc_now_iso)


class ResearchToolCall(SQLModel, table=True):
    call_id: str = Field(primary_key=True)
    run_id: str = Field(index=True)
    tool: str
    status: str = 'running'
    result_json: str = '{}'
    created_at: str = Field(default_factory=utc_now_iso)
