"""Compact operational state and latest summaries; observation history is DuckDB."""
from sqlalchemy import Index
from sqlmodel import SQLModel,Field
from ..storage.models import utc_now_iso


class AttentionInbox(SQLModel,table=True):
    __table_args__=(Index('ix_attentioninbox_work','state','next_attempt','created'),)
    key:str=Field(primary_key=True)
    manifest_path:str
    manifest_sha256:str
    run_id:int|None=Field(default=None,index=True)
    created:str=Field(default_factory=utc_now_iso)
    state:str='pending'
    lease:str=''
    lease_until:float=0
    attempts:int=0
    next_attempt:float=0
    error:str=''
    finished_at:str=''


class AttentionMeta(SQLModel,table=True):
    key:str=Field(primary_key=True)
    value:str=''


class AttentionTopic(SQLModel,table=True):
    __table_args__=(Index('ix_attentiontopic_recent','last_seen','key'),)
    key:str=Field(primary_key=True)
    title:str
    normalized_title:str=''
    normalized_hash:str=Field(default='',index=True)
    signature_json:str='{}'
    created:str=Field(default_factory=utc_now_iso)
    first_seen:str=''
    last_seen:str=Field(default='',index=True)
    attention_score:float|None=Field(default=None,index=True)
    rise_score:float|None=Field(default=None,index=True)
    platform_count:int=0
    item_count:int=0
    baseline_state:str='insufficient'
    summary_json:str='{}'
    chart_json:str='[]'
    generation:str=''
    updated_at:str=Field(default_factory=utc_now_iso)
    match_checked_at:str=''
    match_next_at:float=Field(default=0,index=True)
    match_error:str=''


class AttentionItem(SQLModel,table=True):
    __table_args__=(Index('ix_attentionitem_topic_recent','topic','last_seen'),)
    key:str=Field(primary_key=True)
    topic:str=Field(index=True)
    source_id:str=Field(index=True)
    platform:str=''
    source_kind:str='unknown'
    audience_scope:str='unknown'
    native_id:str=''
    url:str=''
    canonical_url:str=''
    canonical_hash:str=Field(default='',index=True)
    title:str=''
    excerpt:str=''
    normalized_title:str=''
    first_seen:str=''
    last_seen:str=''
    list_position:int|None=None
    snapshot_ref_json:str='{}'
    grouping_json:str='{}'


class AttentionEventMatch(SQLModel,table=True):
    __table_args__=(Index('ix_attentioneventmatch_topic_status','topic','status'),)
    key:str=Field(primary_key=True)
    topic:str=Field(index=True)
    event_id:str=Field(index=True)
    status:str='candidate'
    method:str=''
    score:float=0
    reasons_json:str='[]'
    generation:str=''
    updated_at:str=Field(default_factory=utc_now_iso)


class AttentionWatchMatch(SQLModel,table=True):
    __table_args__=(Index('ix_attentionwatchmatch_watch_topic','watch','topic'),)
    key:str=Field(primary_key=True)
    topic:str=Field(index=True)
    watch:str=Field(index=True)
    scope_version:int=0
    reasons_json:str='[]'
    updated_at:str=Field(default_factory=utc_now_iso)


class AttentionSelection(SQLModel,table=True):
    __table_args__=(Index('ix_attentionselection_topic','topic','created'),)
    task:str=Field(primary_key=True)
    topic:str
    request_hash:str
    snapshot_json:str='{}'
    created:str=Field(default_factory=utc_now_iso)
