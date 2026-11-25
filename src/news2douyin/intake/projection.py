"""Replayable working-set projection of an immutable intake archive.

The archive is authoritative for collection completeness.  These short SQL
transactions make reports searchable and feed the existing extraction/graph
queues; they never call a model, news search, or remote article fetcher.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timedelta, timezone
from threading import Event as StopEvent, Thread
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field as InputField, model_validator
from sqlalchemy import Index, case, func
from sqlalchemy.exc import SQLAlchemyError
from time import perf_counter, time as wall_time
from sqlmodel import SQLModel, Field, Session, select

from ..dedup.normalize import canonicalize_url, normalize_article
from ..desk.models import GkgSource, DeskDirty
from ..extraction.models import VersionReading
from ..extraction.schema import stable_id
from ..storage.articles import identity_key, latest_version, lock_news, record_version
from ..storage.models import Article, ArticleIdentity, ArticleEventLink, GdeltDiscovery, utc_now_iso
from ..translations.language import language_metadata, normalize_language

log=logging.getLogger(__name__)


class IntakeProjectionReceipt(SQLModel, table=True):
    key:str=Field(primary_key=True)
    feed:str=Field(index=True)
    stamp:str=Field(index=True)
    row_index:int=0
    source_record_id:str=''
    status:str=Field(index=True)
    article_key:str=Field(default='',index=True)
    revision:int=0
    error:str=''
    # External source observations are immutable here. Body bytes remain in the
    # referenced ArticleVersion; GKG uses its immutable lake row instead.
    envelope_json:str=''
    created:str=Field(default_factory=utc_now_iso)


class IntakeProjectionState(SQLModel, table=True):
    key:str=Field(default='global',primary_key=True)
    last_error:str=''
    updated:str=Field(default_factory=utc_now_iso)


class IntakeProjectionTurn(SQLModel, table=True):
    """Fair source-kind scheduling survives restarts and old GKG backlog."""
    key:str=Field(default='global',primary_key=True)
    kind:str='mentions'


class IntakeProjectionFailure(SQLModel, table=True):
    """A held cursor and retry schedule, never a successful/skipped receipt."""
    __table_args__=(Index('ix_intakeprojectionfailure_due','state','retry_after'),)
    key:str=Field(primary_key=True)
    feed:str=Field(index=True)
    stamp:str=''
    offset:int=0
    row_index:int|None=None  # Prefetch/chunk failures need not identify one row.
    error_context_json:str='{}'
    phase:str=''
    error_type:str=''
    error:str=''
    attempts:int=0
    retry_after:float=0
    state:str='blocked'
    last_failed_at:str=Field(default_factory=utc_now_iso)
    resolved_at:str=''


class IntakeProjectionMeter(SQLModel, table=True):
    key:str=Field(default='global',primary_key=True)
    history_known:bool=False
    created:str=Field(default_factory=utc_now_iso)


class IntakeProjectionCounter(SQLModel, table=True):
    """Small transactionally updated counters, not a historical receipt scan."""
    key:str=Field(primary_key=True)
    projected:int=0
    skipped:int=0
    last_chunk_json:str='{}'
    updated:str=Field(default_factory=utc_now_iso)


def _meter(session):
    row=session.get(IntakeProjectionMeter,'global')
    if row:return row
    from .models import IntakeBatch
    old_receipt=session.exec(select(IntakeProjectionReceipt.key).limit(1)).first()
    old_offset=session.exec(select(IntakeBatch.key).where(IntakeBatch.projected_offset>0).limit(1)).first()
    row=IntakeProjectionMeter(history_known=old_receipt is None and old_offset is None)
    session.add(row)
    return row


def _counter(session,key):
    row=session.get(IntakeProjectionCounter,key)
    if row is None:
        row=IntakeProjectionCounter(key=key);session.add(row)
    return row


def _object(value):
    try:result=json.loads(value or '{}')
    except (ValueError,TypeError):return {}
    return result if isinstance(result,dict) else {}


def _isolatable(exc):
    # A database outage, lock timeout, statement cancellation or schema failure
    # must not re-open the same database to report itself as a bad news row.
    if not isinstance(exc,SQLAlchemyError):return True
    original=getattr(exc,'orig',exc)
    code=getattr(original,'sqlstate',None) or getattr(original,'pgcode',None) or ''
    return code.startswith('22') or code in ('23502','23514','54000')


def _failure_message(exc):
    if isinstance(exc,SQLAlchemyError):
        original=getattr(exc,'orig',exc)
        code=getattr(original,'sqlstate',None) or getattr(original,'pgcode',None) or ''
        return '记录写入未通过数据库校验'+('（SQLSTATE '+code+'）' if code else '')
    return str(exc)[:500] or type(exc).__name__


def _block_batch(engine,key,feed,stamp,offset,row_index,phase,exc,now,read_end):
    from .models import IntakeBatch
    with Session(engine) as s:
        lock_news(s,'intake:projection-turn','intake:batch:'+key)
        batch=s.get(IntakeBatch,key)
        if not batch or batch.state!='archived' or batch.projected_offset!=offset:return False
        row=s.get(IntakeProjectionFailure,key) or IntakeProjectionFailure(key=key,feed=feed)
        row.feed,row.stamp,row.offset,row.row_index=feed,stamp,offset,row_index
        row.phase,row.error_type,row.error=phase,type(exc).__name__,_failure_message(exc)
        row.error_context_json=json.dumps(dict(read_start=offset+1,read_end=read_end,
            row_is_exact=row_index is not None),ensure_ascii=False)
        row.attempts+=1;row.state='blocked';row.resolved_at='';row.last_failed_at=utc_now_iso()
        row.retry_after=now+min(900,5*2**min(row.attempts-1,8))
        s.add(row)
        from .feeds import kind_for
        s.merge(IntakeProjectionTurn(key='global',kind=kind_for(feed)))
        s.commit()
    return True


def retry_projection(engine,key):
    """Release one backoff without editing coverage, input or receipts."""
    from .models import IntakeBatch
    if not isinstance(key,str) or len(key)>160:raise ValueError('投影批次编号无效')
    with Session(engine) as s:
        lock_news(s,'intake:batch:'+key)
        batch=s.get(IntakeBatch,key)
        if not batch:raise KeyError('投影批次不存在')
        row=s.get(IntakeProjectionFailure,key)
        if row:
            row.retry_after=0;s.add(row)
        s.commit()
    return dict(queued=True,key=key)


def _http_uri(value):
    if not value:return ''
    if any(ord(c)<33 for c in value) or '\\' in value:raise ValueError('报道地址格式无效')
    try:
        u=urlsplit(value)
        if u.scheme not in ('http','https') or not u.hostname or u.username is not None or u.password is not None:
            raise ValueError('仅接受不含凭证的HTTP/HTTPS报道地址')
        u.port
    except ValueError:
        raise ValueError('报道地址格式无效')
    return value


def _iso(value,*,optional=False):
    if not value and optional:return ''
    try:
        t=datetime.fromisoformat(value.replace('Z','+00:00'))
        if t.tzinfo is None:raise ValueError()
        return t.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
    except (ValueError,TypeError,AttributeError):raise ValueError('时间必须是带时区的ISO时间')


class IntakeDocument(BaseModel):
    """Connector envelope. Publication time is not event occurrence time."""
    model_config=ConfigDict(extra='forbid')
    provider:str=InputField(min_length=1,max_length=100)
    record_id:str=InputField(min_length=1,max_length=500)
    uri:str=InputField(default='',max_length=8000)
    title:str=InputField(default='',max_length=10000)
    content:str=InputField(default='',max_length=2000000)
    published_at:str=''
    observed_at:str=InputField(default_factory=utc_now_iso)
    language:str=InputField(default='',max_length=40)
    country:str=InputField(default='',max_length=20)
    metadata:dict[str,Any]=InputField(default_factory=dict)
    content_sha256:str=''

    @model_validator(mode='after')
    def check(self):
        self.provider=self.provider.strip();self.record_id=self.record_id.strip()
        if not self.provider or not self.record_id:raise ValueError('采集器与来源记录编号不能为空')
        if not re.fullmatch(r'[A-Za-z0-9_.:-]+',self.provider):raise ValueError('采集器编号只接受字母、数字、点、冒号、横线及下划线')
        self.uri=_http_uri(self.uri.strip())
        self.observed_at=_iso(self.observed_at)
        self.published_at=_iso(self.published_at,optional=True)
        digest=hashlib.sha256(self.content.encode('utf-8')).hexdigest()
        if self.content_sha256 and self.content_sha256.lower()!=digest:raise ValueError('正文SHA256与内容不一致')
        self.content_sha256=digest
        # Do not allow non-JSON side objects to be dropped from provenance.
        json.dumps(self.metadata,ensure_ascii=False,allow_nan=False)
        return self


def _document_identity(document):
    item=dict(provider=document.provider,url=document.uri,title=document.title,
        published_at=document.published_at,country=document.country)
    return identity_key(item) if document.uri else hashlib.sha256(
        ('connector:'+document.provider+':'+document.record_id).encode()).hexdigest()


def _article_identity(s,document,*,locked=False):
    ident=_document_identity(document)
    if not locked:lock_news(s,'article:'+ident)
    mapped=s.get(ArticleIdentity,ident)
    existing=s.exec(select(Article).where(Article.article_key==mapped.article_key)).first() if mapped else None
    if not existing and document.uri:
        # Old/manual rows may predate the identity ledger.
        canonical=canonicalize_url(document.uri)
        existing=s.exec(select(Article).where((Article.canonical_url==canonical)|(Article.url==document.uri))
            .order_by(Article.id).limit(1)).first()
    key=existing.article_key if existing else ident[:24]
    if not mapped:s.add(ArticleIdentity(identity_key=ident,article_key=key))
    return existing,key


def _set_normalized(row):
    n=normalize_article(row.title,row.content,row.url)
    for name in ('canonical_url','normalized_title','normalized_content','title_hash','content_hash','title_signature','content_signature'):
        setattr(row,name,getattr(n,name))


def _refresh_linked_events(s, article_key):
    from ..events.service import refresh_event

    keys = s.exec(select(ArticleEventLink.event_key).where(ArticleEventLink.article_key == article_key)
        .execution_options(n2d_query_name='intake.article.linked_events')).all()
    for key in keys:
        # This is a new version of evidence already admitted to the event.
        refresh_event(s, key)
        s.merge(DeskDirty(key=key))
    return bool(keys)


# Full GKG metadata remains in the immutable archive. These are the fields
# actually used by radar, topic recall and provenance; GCAM/XML/enhanced blobs
# must not be copied into every online article and immutable body snapshot.
GKG_ONLINE_FIELDS=('persons','orgs','themes','locations','mention_iso','author','source',
    'source_collection','translation','tone','document_date','gkg_record_id',
    'source_language','source_country','projection_eligible','projection_note')


def compact_gkg_metadata(metadata,pointer):
    return dict({key:metadata[key] for key in GKG_ONLINE_FIELDS if key in metadata},
                archive_ref=dict(pointer))


def _document_language(document):
    metadata=language_metadata(document.metadata.get('source_language',document.language),provider=document.provider)
    metadata['language']=normalize_language(document.language,provider=document.provider) or metadata['language']
    return metadata


def _upsert(s,document,*,pointer=None,discovery=False,locked=False):
    historical = bool(discovery and pointer and pointer.get('repair_day'))
    existing,key=_article_identity(s,document,locked=locked)
    metadata=compact_gkg_metadata(document.metadata,pointer) if discovery and pointer else document.metadata
    language=_document_language(document)
    raw=dict(provider=document.provider,record_id=document.record_id,uri=document.uri,
        observed_at=document.observed_at,published_at=document.published_at,
        metadata=metadata,content_sha256=document.content_sha256,**language)
    if pointer:raw['intake']=pointer
    raw['material_kind']='discovery_metadata' if discovery else 'connector_document'
    raw['title_missing']=not bool(document.title.strip())
    created=not existing
    row=existing or Article(article_key=key,provider=document.provider,url=document.uri,
        title=document.title,content=document.content,source_domain=urlsplit(document.uri).hostname or '',
        language=language['language'],country=document.country,published_at=document.published_at,
        fetched_at=document.observed_at,raw_json=json.dumps(raw,ensure_ascii=False))
    changed=created
    if existing:
        # Preserve a baseline before attaching fresh observations to a legacy row.
        if latest_version(s,key) is None:
            record_version(s,existing,origin='intake_history' if historical else 'legacy_baseline')
            s.flush()
        protect=(not document.content.strip() and bool(existing.content.strip())) or (
            discovery and existing.provider not in ('gkg','gdelt'))
        if discovery and pointer:
            previous=s.get(GkgSource,key)
            previous_stamp=json.loads(previous.attributes or '{}').get('intake_batch',previous.record_id[:14]) if previous else ''
            # A lagging stream must not roll a newer stored headline backwards.
            protect=protect or bool(previous_stamp and previous_stamp>=pointer['stamp'] and existing.title.strip())
        before=tuple(getattr(row,name) for name in ('title','content','published_at','language','country','url','source_domain','provider'))
        if not protect:
            # Discovery may fill/correct a headline but never empty an existing one.
            if document.title.strip():row.title=document.title
            if not discovery:row.content=document.content
            if document.published_at:row.published_at=document.published_at
            if language['language']:row.language=language['language']
            if document.country:row.country=document.country
            if document.uri:row.url=document.uri;row.source_domain=urlsplit(document.uri).hostname or row.source_domain
            if not discovery:row.provider=document.provider
        material_changed=before!=tuple(getattr(row,name) for name in ('title','content','published_at','language','country','url','source_domain','provider'))
        if material_changed:
            row.raw_json=json.dumps(raw,ensure_ascii=False)
            row.fetched_at=max(row.fetched_at or '',document.observed_at)
        # Every observation survives in a lake row or receipt. Re-observation,
        # metadata and fetched_at alone must not rewrite/reindex the article.
        changed=material_changed
    if changed:
        _set_normalized(row);s.add(row);s.flush()
        origin = 'intake_metadata' if discovery else 'connector'
        changed=record_version(s,row,origin='intake_history' if historical else origin) or created
        s.flush()
    version=latest_version(s,key)
    if version and not row.title.strip() and not row.content.strip():
        # Retain the version for auditing without queueing an empty model call.
        job=s.get(VersionReading,version.id)
        if job and job.state in ('baseline','focus_baseline','model_queued'):
            job.state='metadata_only';job.note='仅有采集元数据，等待可读标题或正文';s.add(job)
    gkg_metadata=bool(discovery and pointer)
    if changed:
        linked = _refresh_linked_events(s, key)
        if gkg_metadata and not linked and version:
            auto_events = os.getenv('NEWS2DOUYIN_GKG_AUTO_EVENTS', '0').strip().lower() in ('1','true','on','yes')
            if auto_events and row.title.strip():
                from ..candidates.promotion import approve_collected_article

                event_key='evt_intake_'+stable_id(key)[:20]
                approve_collected_article(s, row, event_key=event_key,
                    decision='legacy_auto', reason='intake_report_candidate', topic='global',
                    seen_at=row.published_at or row.fetched_at)
                s.merge(DeskDirty(key=event_key))
            else:
                # Discovery headlines only queue extraction after being selected.
                job=s.get(VersionReading,version.id)
                if job and job.state in ('baseline','focus_baseline','model_queued'):
                    job.state='metadata_only';job.note='来源元数据已保存，选题后再进行事件抽取';s.add(job)
    return row,version


def _lake_document(row):
    meta=row.get('metadata') or {}
    if isinstance(meta,str):meta=json.loads(meta)
    return IntakeDocument(provider='gkg',record_id=str(row.get('record_id') or ''),
        uri=str(row.get('url') or ''),title=str(row.get('title') or ''),
        observed_at=row.get('observed_at') or utc_now_iso(),
        language=meta.get('source_language') or '',country=meta.get('source_country') or '',
        metadata=meta)


def _receipt_key(feed,stamp,index):return 'intake_'+stable_id(feed,stamp,index)


def _receipt_data(row):
    return row.model_dump(exclude={'envelope_json'})


def lookup_receipt(s,feed,stamp,row_index):
    row=s.get(IntakeProjectionReceipt,_receipt_key(feed,stamp,row_index))
    return _receipt_data(row) if row else None


def _setting(name,default,minimum):
    try:value=float(os.getenv(name,str(default)))
    except (TypeError,ValueError):value=default
    return max(minimum,value) if value<float('inf') else default


def project_step(engine,storage_root,limit=None,*,time_budget_ms=None,now=None,observer=None):
    """Project a bounded atomic prefix; failures hold their cursor and yield."""
    from .models import IntakeBatch
    from .lake import Lake
    from .feeds import FEEDS,kind_for
    now=wall_time() if now is None else float(now)
    began=perf_counter();timings={}
    if limit is None:limit=_setting('NEWS2DOUYIN_GKG_PROJECTION_CHUNK',25,1)
    limit=max(1,min(int(limit),500))
    budget=max(1,float(time_budget_ms)) if time_budget_ms is not None else _setting('NEWS2DOUYIN_GKG_PROJECTION_BUDGET_MS',250,1)
    lanes=('gkg','events','mentions')
    # A historical repair can insert a much older stamp into this ledger.
    # Reserve one in four GKG turns for older batches when both windows have work.
    recent_from=(datetime.fromtimestamp(now,timezone.utc)-timedelta(days=7)).strftime('%Y%m%d%H%M%S')
    gkg_turn_key='gkg-window'
    with Session(engine) as s:
        turn=s.get(IntakeProjectionTurn,'global')
        previous=turn.kind if turn and turn.kind in lanes else 'mentions'
        start=(lanes.index(previous)+1)%len(lanes)
        batch=None
        blocked=select(IntakeProjectionFailure.key).where(
            IntakeProjectionFailure.key==IntakeBatch.key,
            IntakeProjectionFailure.state=='blocked',IntakeProjectionFailure.retry_after>now).correlate(IntakeBatch).exists()
        next_gkg_turn=None
        for lane in lanes[start:]+lanes[:start]:
            feeds=[feed for feed,spec in FEEDS.items() if spec['kind']==lane]
            pending=select(IntakeBatch).where(IntakeBatch.state=='archived',IntakeBatch.feed.in_(feeds),
                IntakeBatch.projected_offset<IntakeBatch.rows,~blocked).order_by(IntakeBatch.stamp,IntakeBatch.feed).limit(1)
            if lane=='gkg':
                saved=s.get(IntakeProjectionTurn,gkg_turn_key)
                recent_turns={'recent-1':1,'recent-2':2,'recent-3':3}.get(saved.kind,0) if saved else 0
                recent=IntakeBatch.stamp>=recent_from
                windows=((~recent,False),(recent,True)) if recent_turns==3 else ((recent,True),(~recent,False))
                for condition,is_recent in windows:
                    batch=s.exec(pending.where(condition).execution_options(
                        n2d_query_name='intake.projection.pick.gkg.'+('recent' if is_recent else 'history'))).first()
                    if batch:
                        next_gkg_turn='recent-'+str(min(3,recent_turns+1)) if is_recent else 'history'
                        break
            else:
                batch=s.exec(pending.execution_options(n2d_query_name='intake.projection.pick.'+lane)).first()
            if batch:break
        if not batch:return False
        key,feed,stamp,offset,total=batch.key,batch.feed,batch.stamp,batch.projected_offset,batch.rows
    timings['select']=round((perf_counter()-began)*1000,3)
    phase='lake_read';row_index=None
    try:
        tick=perf_counter();lake=Lake(storage_root)
        rows=lake.rows(feed,stamp,after=offset,limit=limit)
        official=kind_for(feed)!='gkg'
        reference=lake.archive_reference(feed,stamp,offset+1) if not official else None
        timings['lake_read']=round((perf_counter()-tick)*1000,3)
        if not rows:raise RuntimeError('完整归档存在但待投影记录不可读取')
        indices=[int(row['row_index']) for row in rows]
        if indices!=list(range(offset+1,offset+1+len(rows))) or indices[-1]>total:
            raise RuntimeError('归档行号不连续，投影已停止以防跳过材料')
        phase='prepare';tick=perf_counter();prepared=[]
        for raw in rows:
            document=None;error=''
            if official:
                prepared.append((raw,None,''));continue
            if not raw.get('valid',False):error='原始记录格式无效，原档仍完整保留'
            elif not raw.get('url'):error='来源未给出可投影的报道地址，原档仍完整保留'
            else:
                try:document=_lake_document(raw)
                except (ValueError,TypeError):error='来源字段未通过投影校验，原档仍完整保留'
            prepared.append((raw,document,error))
        timings['prepare']=round((perf_counter()-tick)*1000,3)
        phase='sql_lock';tick=perf_counter()
        with Session(engine) as s:
            lock_news(s,'intake:projection-turn','intake:batch:'+key)
            batch=s.get(IntakeBatch,key)
            if not batch or batch.state!='archived' or batch.projected_offset!=offset:return True
            failure=s.get(IntakeProjectionFailure,key)
            if failure and failure.state=='blocked' and failure.retry_after>now:return False
            resources=sorted({'article:'+_document_identity(doc) for _,doc,_ in prepared if doc is not None})
            if resources:lock_news(s,*resources)
            lock_news(s,'intake:projection-metrics')
            _meter(s);counter=_counter(s,feed)
            timings['sql_lock']=round((perf_counter()-tick)*1000,3)
            phase='sql_row';tick=perf_counter();new_projected=new_skipped=replayed=0
            changed_official=set();changed_gkg=set()
            started=time.monotonic()
            # The offset/turn locks fence all receipt writers for this batch.
            # Read at most this prepared chunk's keys once, including skipped
            # rows; replay must not perform N ledger round trips or re-count.
            receipt_keys=[_receipt_key(feed,stamp,int(raw['row_index'])) for raw,_,_ in prepared]
            present_receipts=set(s.exec(select(IntakeProjectionReceipt.key).where(
                IntakeProjectionReceipt.key.in_(receipt_keys)).execution_options(
                    n2d_query_name='intake.projection.receipts.chunk')).all())
            for raw,document,error in prepared:
                phase='sql_row';row_index=int(raw['row_index'])
                index=int(raw['row_index']);ident=_receipt_key(feed,stamp,index)
                if ident in present_receipts:
                    # Receipt and offset are one SQL transaction. Recovery from a
                    # deliberately rewound cursor reuses the receipt without rewriting.
                    batch.projected_offset=index;replayed+=1
                else:
                    article=None;version=None
                    if official:
                        from .official_projection import project_row
                        result=project_row(s,raw)
                        if result['event_id']:changed_official.add(result['event_id'])
                        s.add(IntakeProjectionReceipt(key=ident,feed=feed,stamp=stamp,row_index=index,
                            source_record_id=str(raw.get('record_id') or ''),status=result['status'],
                            error=result.get('error','')))
                        batch.projected_offset=index
                        new_skipped+=int(result['status']=='skipped');new_projected+=int(result['status']=='projected')
                        if (time.monotonic()-started)*1000>=budget:break
                        continue
                    if document is not None:
                        pointer=dict(reference,row_index=index)
                        if raw.get('repair_day'):
                            pointer['repair_day'] = raw['repair_day']
                        article,version=_upsert(s,document,pointer=pointer,discovery=True,locked=True)
                        meta=dict(compact_gkg_metadata(document.metadata,pointer),document_identifier=document.uri,
                            record_id=document.record_id,intake_feed=feed,
                            intake_batch=stamp,intake_row=index,title_missing=not bool(document.title.strip()))
                        previous=s.get(GkgSource,article.article_key)
                        previous_stamp=json.loads(previous.attributes or '{}').get('intake_batch',previous.record_id[:14]) if previous else ''
                        attributes=json.dumps(meta,ensure_ascii=False,sort_keys=True)
                        if not previous or (stamp>=previous_stamp and (previous.record_id!=document.record_id or previous.attributes!=attributes)):
                            s.merge(GkgSource(article_key=article.article_key,record_id=document.record_id,attributes=attributes))
                            changed_gkg.add(document.uri)
                        existing_discovery=s.get(GdeltDiscovery,article.article_key)
                        discovery=existing_discovery or GdeltDiscovery(article_key=article.article_key)
                        when=datetime.strptime(stamp,'%Y%m%d%H%M%S').strftime('%Y-%m-%dT%H:%M:%SZ')
                        if not existing_discovery or not discovery.seen_at or when<discovery.seen_at:
                            discovery.seen_at=when;s.add(discovery)
                    s.add(IntakeProjectionReceipt(key=ident,feed=feed,stamp=stamp,row_index=index,
                        source_record_id=str(raw.get('record_id') or ''),status='skipped' if error else 'projected',
                        article_key=article.article_key if article else '',revision=version.revision if version else 0,error=error))
                    batch.projected_offset=index
                    new_skipped+=int(bool(error));new_projected+=int(not error)
                # The budget bounds a transaction, never collection completeness.
                # At least one row is acknowledged; the next tick resumes exactly.
                if (time.monotonic()-started)*1000>=budget:break

            phase='sql_flush';row_index=None
            if changed_official:
                from ..desk.official_graph import refresh_tracked
                from .official_projection import track_focus_updates
                refresh_tracked(s,changed_official)
                track_focus_updates(s,changed_official)
            if changed_gkg:
                from ..desk.official_graph import refresh_documents
                refresh_documents(s,changed_gkg)
            s.flush()
            timings['sql_apply']=round((perf_counter()-tick)*1000,3)
            chunk=dict(feed=feed,stamp=stamp,key=key,measurement_id=uuid4().hex,at=utc_now_iso(),read_rows=len(rows),
                processed_rows=batch.projected_offset-offset,new_projected=new_projected,new_skipped=new_skipped,
                replayed=replayed,offset_before=offset,offset_after=batch.projected_offset,
                phase_ms=dict(timings,commit=None),elapsed_ms=round((perf_counter()-began)*1000,3),
                timing_note='SQL lock includes identity and metric locks; persisted timings exclude commit; lake_read includes lock wait, decoding and archive reference.')
            counter.projected+=new_projected;counter.skipped+=new_skipped
            counter.last_chunk_json=json.dumps(chunk,ensure_ascii=False);counter.updated=utc_now_iso();s.add(counter)
            if failure and failure.state=='blocked':
                failed_range=_object(failure.error_context_json)
                target=failure.row_index or failed_range.get('read_end') or total
                if batch.projected_offset>=target or batch.projected_offset>=total:
                    failure.state='resolved';failure.resolved_at=utc_now_iso();failure.retry_after=0;s.add(failure)
            s.add(batch);s.merge(IntakeProjectionTurn(key='global',kind=kind_for(feed)))
            if next_gkg_turn:
                s.merge(IntakeProjectionTurn(key=gkg_turn_key,kind=next_gkg_turn))
            phase='commit';row_index=None;tick=perf_counter();s.commit()
        chunk['phase_ms']['commit']=round((perf_counter()-tick)*1000,3)
        chunk['elapsed_ms']=round((perf_counter()-began)*1000,3)
        if observer is not None:
            try:observer(chunk)
            except Exception:log.warning('Projection timing observer failed after commit',exc_info=True)
        return True
    except Exception as exc:
        if _isolatable(exc):
            isolated=_block_batch(engine,key,feed,stamp,offset,row_index,phase,exc,now,min(total,offset+limit))
            if isolated:setattr(exc,'_intake_batch_isolated',True)
        raise

def projection_status(engine,worker=None):
    """Read compact counters and batch offsets, never scan receipt history."""
    from .models import IntakeBatch
    from .feeds import FEEDS
    with Session(engine) as s:
        values=s.exec(select(IntakeBatch.feed,func.coalesce(func.sum(IntakeBatch.rows),0),
            func.coalesce(func.sum(IntakeBatch.projected_offset),0),
            func.coalesce(func.sum(case((IntakeBatch.rows>IntakeBatch.projected_offset,1),else_=0)),0))
            .where(IntakeBatch.state=='archived').group_by(IntakeBatch.feed)
            .execution_options(n2d_query_name='intake.projection.batch_offsets')).all()
        totals={feed:dict(rows=int(rows),processed_rows=int(offset),waiting=max(0,int(rows)-int(offset)),
            unresolved_batches=int(pending)) for feed,rows,offset,pending in values}
        counters={row.key:row for row in s.exec(select(IntakeProjectionCounter))}
        meter=s.get(IntakeProjectionMeter,'global')
        if meter:known=meter.history_known
        else:
            # An indexed existence probe distinguishes a genuinely empty store
            # from legacy receipts; no COUNT/GROUP over the historical ledger.
            known=not any(row['processed_rows'] for row in totals.values()) and s.exec(
                select(IntakeProjectionReceipt.key).limit(1)).first() is None
        failures=s.exec(select(IntakeProjectionFailure).where(IntakeProjectionFailure.state=='blocked')
            .order_by(IntakeProjectionFailure.last_failed_at.desc(),IntakeProjectionFailure.key).limit(21)).all()
        failure_counts=dict(s.exec(select(IntakeProjectionFailure.feed,func.count()).where(
            IntakeProjectionFailure.state=='blocked').group_by(IntakeProjectionFailure.feed)).all())
        per_feed=[]
        for feed in FEEDS:
            counter=counters.get(feed)
            pending=s.exec(select(IntakeBatch.key,IntakeBatch.stamp,IntakeBatch.projected_offset)
                .where(IntakeBatch.feed==feed,IntakeBatch.state=='archived',
                    IntakeBatch.projected_offset<IntakeBatch.rows)
                .order_by(IntakeBatch.stamp,IntakeBatch.key).limit(1)).first()
            recent=_object(counter.last_chunk_json) if counter else {}
            observed=(getattr(worker,'last_chunks',{}) or {}).get(feed)
            if observed and observed.get('measurement_id')==recent.get('measurement_id'):recent=observed
            projected=counter.projected if counter else 0;skipped=counter.skipped if counter else 0
            per_feed.append(dict(feed=feed,**totals.get(feed,dict(rows=0,processed_rows=0,waiting=0,unresolved_batches=0)),
                projected=projected if known else None,skipped=skipped if known else None,
                since_upgrade_projected=projected,since_upgrade_skipped=skipped,counts_known=known,
                counts_scope='complete_since_empty_database' if known else 'since_upgrade_only',
                blocked_batches=int(failure_counts.get(feed,0)),last_chunk=recent,
                oldest_pending=dict(key=pending[0],stamp=pending[1],offset=pending[2]) if pending else None))
        def public_failure(row):
            data=row.model_dump(exclude={'error_context_json'})
            data['context']=_object(row.error_context_json)
            return data
        projected=sum(row.projected for key,row in counters.items() if key!='external')
        skipped=sum(row.skipped for key,row in counters.items() if key!='external')
        external=counters.get('external');external_count=external.projected if external else 0
        return dict(projected=projected if known else None,skipped=skipped if known else None,
            processed_rows=sum(row['processed_rows'] for row in totals.values()),
            waiting=sum(row['waiting'] for row in totals.values()),
            unresolved_batches=sum(row['unresolved_batches'] for row in totals.values()),
            blocked_batches=sum(failure_counts.values()),failures=[public_failure(row) for row in failures[:20]],
            failures_limited=len(failures)>20,per_feed=per_feed,counts_known=known,
            counts_scope='complete_since_empty_database' if known else 'since_upgrade_only',
            since_upgrade=dict(projected=projected,skipped=skipped,external_imported=external_count),
            last_error=getattr(worker,'last_error','') or (failures[0].error if failures else ''),
            external_imported=external_count if known else None,
            note='processed_rows为已处理源行游标，包含明确跳过；不是新增文章数。未完成批次不是错误批次。旧库不扫描历史回执，分类计数只展示本版以来的增量。')


def import_document(engine,payload):
    """Import an external connector envelope without performing network work."""
    document=IntakeDocument.model_validate(payload)
    content=document.model_dump();content.pop('observed_at',None)
    ident='external_'+hashlib.sha256(json.dumps(content,ensure_ascii=False,sort_keys=True,
        separators=(',',':')).encode('utf-8')).hexdigest()[:32]
    with Session(engine) as s:
        lock_news(s,'intake:receipt:'+ident,'article:'+_document_identity(document))
        old=s.get(IntakeProjectionReceipt,ident)
        if old:return dict(_receipt_data(old),reused=True)
        lock_news(s,'intake:projection-metrics');_meter(s);counter=_counter(s,'external')
        article,version=_upsert(s,document,locked=True)
        from ..candidates.service import record_reports

        record_reports(s, [dict(url=document.uri, title=document.title, content=document.content,
            content_kind=document.metadata.get('content_kind','text'), country=document.country,
            **_document_language(document),
            fetched_at=document.observed_at, published_at=document.published_at)],
            provider=document.provider, batch='connector/'+ident,
            source_ref={'receipt_key': ident, 'record_id': document.record_id,
                'article_key': article.article_key, 'revision': version.revision if version else 0,
                'content_sha256': document.content_sha256})
        receipt=IntakeProjectionReceipt(key=ident,feed='external:'+document.provider,stamp='',
            source_record_id=document.record_id,status='projected',article_key=article.article_key,
            revision=version.revision if version else 0,
            envelope_json=json.dumps(document.model_dump(exclude={'content'}),ensure_ascii=False))
        s.add(receipt);counter.projected+=1;counter.updated=utc_now_iso();s.add(counter)
        s.flush();result=dict(_receipt_data(receipt),reused=False)
        s.commit();return result


def projection_interval(engine):
    """Busy delay, independent of idle/error backoff; explicit settings win."""
    default=.05 if getattr(getattr(engine,'dialect',None),'name','')=='postgresql' else 1.
    raw=(os.getenv('NEWS2DOUYIN_GDELT_PROJECTION_INTERVAL','').strip()
         or os.getenv('NEWS2DOUYIN_GKG_PROJECTION_INTERVAL','').strip())
    try:value=float(raw) if raw else default
    except ValueError:value=default
    return max(.01,value) if math.isfinite(value) else default


class ProjectionWorker:
    def __init__(self,engine,storage_root):
        self.engine,self.storage_root=engine,storage_root
        self.stop_event=StopEvent();self.thread=None
        self.last_error='';self.failures=0;self.last_chunks={}

    @property
    def running(self):return bool(self.thread and self.thread.is_alive())

    def _observe(self,chunk):self.last_chunks[chunk['feed']]=chunk

    def step(self,limit=None):return project_step(self.engine,self.storage_root,limit,observer=self._observe)

    def start(self):
        if self.thread and self.thread.is_alive():return
        configured=os.getenv('NEWS2DOUYIN_GDELT_PROJECTION_ENABLED','').strip() or os.getenv('NEWS2DOUYIN_GKG_PROJECTION_ENABLED','1').strip()
        if configured.lower() in ('0','false','off','no'):return
        self.stop_event.clear();self.thread=Thread(target=self.loop,daemon=True);self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=15)

    def loop(self):
        while not self.stop_event.is_set():
            try:changed=self.step()
            except Exception as exc:
                log.exception('intake projection failed')
                # A locked/unavailable transaction store must not be opened
                # again merely to persist its own failure report.
                self.last_error=type(exc).__name__;self.failures+=1
                delay=.2 if getattr(exc,'_intake_batch_isolated',False) else min(60,2**min(self.failures-1,6))
            else:
                self.last_error='';self.failures=0
                delay=projection_interval(self.engine) if changed else _setting('NEWS2DOUYIN_GKG_PROJECTION_IDLE_INTERVAL',3,1)
            self.stop_event.wait(delay)
