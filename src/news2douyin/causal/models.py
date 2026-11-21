from sqlalchemy import Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from ..storage.models import utc_now_iso


class CausalInvestigation(SQLModel, table=True):
    key: str = Field(primary_key=True)
    text: str
    event_at: str = ''
    event_key: str = ''
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso, index=True)


class CausalNode(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint('case_key', 'parent_key', 'ref'),
        Index('ix_causalnode_case_depth', 'case_key', 'depth'),
    )

    key: str = Field(primary_key=True)
    case_key: str
    parent_key: str = ''
    ref: str = ''
    text: str
    event_key: str = ''
    occurred_at: str = ''
    published_at: str = ''
    time_status: str = 'unknown'
    reason: str = ''
    sources_json: str = '[]'
    score: float = 0
    depth: int = 0
    beam_selected: bool = False


class CausalJob(SQLModel, table=True):
    __table_args__ = (
        Index('ix_causaljob_queue', 'state', 'due_at', 'node_key'),
        Index('ix_causaljob_case_state', 'case_key', 'state'),
    )

    node_key: str = Field(primary_key=True)
    case_key: str
    state: str = 'pending'
    due_at: float = 0
    automatic: bool = False
    lease: str = ''
    error: str = ''


class CausalArchiveLink(SQLModel, table=True):
    case_key: str = Field(primary_key=True)
    day: str = Field(primary_key=True)


class CausalFocus(SQLModel, table=True):
    case_key: str = Field(primary_key=True)
    focus_key: str = Field(index=True)


class CausalProgram(SQLModel, table=True):
    case_key: str = Field(primary_key=True)
    date_from: str


class CausalProgramStep(SQLModel, table=True):
    node_key: str = Field(primary_key=True)
    case_key: str
    queries_json: str = '[]'
    candidate_count: int = 0
    retrieval_mode: str = ''
    stop_reason: str = ''


class CausalProgramRetrieval(SQLModel, table=True):
    node_key: str = Field(primary_key=True)
    report_json: str = '{}'


class CausalAgentLink(SQLModel, table=True):
    case_key: str = Field(primary_key=True)
    research_case_id: str = Field(index=True, unique=True)
    date_from: str
    current_run_id: str = ''
    last_error: str = ''


class CausalAgentQuery(SQLModel, table=True):
    __table_args__ = (Index('ix_causalagentquery_case_run', 'case_key', 'run_id', 'node_key'),)

    key: str = Field(primary_key=True)
    case_key: str
    run_id: str
    node_key: str
    query: str
    date_from: str
    date_to: str
    state: str = 'running'
    results_json: str = '[]'
    error: str = ''


class CausalAgentNote(SQLModel, table=True):
    node_key: str = Field(primary_key=True)
    case_key: str = Field(index=True)
    stop_reason: str = ''
    relation: str = ''
