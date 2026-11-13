"""Replayable working-set projection of an immutable intake archive.

The archive is authoritative for collection completeness.  These short SQL
transactions make reports searchable and feed the existing extraction/graph
queues; they never call a model, news search, or remote article fetcher.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from threading import Event as StopEvent, Thread
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field as InputField, model_validator
from sqlalchemy import func
from sqlmodel import SQLModel, Field, Session, select

from ..dedup.normalize import canonicalize_url, normalize_article
from ..desk.models import GkgSource, DeskDirty
from ..extraction.models import VersionReading
from ..extraction.schema import stable_id
from ..storage.articles import identity_key, latest_version, lock_news, record_version
from ..storage.models import (Article, ArticleIdentity, ArticleEventLink, Event,
    EventAssignment, GdeltDiscovery, utc_now_iso)

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


def _ensure_event(s,row):
    # A metadata-only URL is not an event. A real headline/body supplies an
    # honest report candidate; later graph analysis can connect candidates.
    if not row.title.strip() and not row.content.strip():return
    linked=s.exec(select(ArticleEventLink).where(ArticleEventLink.article_key==row.article_key)).all()
    if not linked:
        key='evt_intake_'+stable_id(row.article_key)[:20]
        event=s.exec(select(Event).where(Event.event_key==key)).first()
        if not event:
            event=Event(event_key=key,event_title=row.title or '待整理的原文报道',topic='global',
                summary=row.content[:400],first_seen_at=row.published_at or row.fetched_at,
                last_seen_at=row.published_at or row.fetched_at,market_scope='global',article_count=1,
                countries_json=json.dumps([row.country] if row.country else []))
            s.add(event)
        s.add(ArticleEventLink(article_key=row.article_key,event_key=key))
        s.merge(EventAssignment(article_key=row.article_key,event_key=key,reason='intake_report_candidate',score=0))
        s.merge(DeskDirty(key=key))
    else:
        for link in linked:
            # Keep curator overrides and article counts through the existing updater.
            from ..events.service import refresh_event
            refresh_event(s,link.event_key)
            s.merge(DeskDirty(key=link.event_key))


def _upsert(s,document,*,pointer=None,discovery=False,locked=False):
    existing,key=_article_identity(s,document,locked=locked)
    raw=dict(provider=document.provider,record_id=document.record_id,uri=document.uri,
        observed_at=document.observed_at,published_at=document.published_at,
        metadata=document.metadata,content_sha256=document.content_sha256)
    if pointer:raw['intake']=pointer
    raw['material_kind']='discovery_metadata' if discovery else 'connector_document'
    raw['title_missing']=not bool(document.title.strip())
    created=not existing
    row=existing or Article(article_key=key,provider=document.provider,url=document.uri,
        title=document.title,content=document.content,source_domain=urlsplit(document.uri).hostname or '',
        language=document.language,country=document.country,published_at=document.published_at,
        fetched_at=document.observed_at,raw_json=json.dumps(raw,ensure_ascii=False))
    changed=created
    if existing:
        # Preserve a baseline before attaching fresh observations to a legacy row.
        if latest_version(s,key) is None:record_version(s,existing,origin='legacy_baseline');s.flush()
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
            if document.language:row.language=document.language
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
        changed=record_version(s,row,origin='intake_metadata' if discovery else 'connector') or created
        s.flush()
    version=latest_version(s,key)
    if version and not row.title.strip() and not row.content.strip():
        # Retain the version for auditing without queueing an empty model call.
        job=s.get(VersionReading,version.id)
        if job and job.state in ('baseline','model_queued'):
            job.state='metadata_only';job.note='仅有采集元数据，等待可读标题或正文';s.add(job)
    if changed:_ensure_event(s,row)
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


def project_step(engine,storage_root,limit=None,*,time_budget_ms=None):
    """Project all archived rows in bounded, replayable transactions."""
    from .models import IntakeBatch
    from .lake import Lake
    if limit is None:limit=_setting('NEWS2DOUYIN_GKG_PROJECTION_CHUNK',25,1)
    limit=max(1,min(int(limit),500))
    budget=max(1,float(time_budget_ms)) if time_budget_ms is not None else _setting('NEWS2DOUYIN_GKG_PROJECTION_BUDGET_MS',250,1)
    with Session(engine) as s:
        batch=s.exec(select(IntakeBatch).where(IntakeBatch.state=='archived',
            IntakeBatch.projected_offset<IntakeBatch.rows).order_by(IntakeBatch.stamp,IntakeBatch.feed).limit(1)).first()
        if not batch:return False
        key,feed,stamp,offset,total=batch.key,batch.feed,batch.stamp,batch.projected_offset,batch.rows
    rows=Lake(storage_root).rows(feed,stamp,after=offset,limit=limit)
    if not rows:raise RuntimeError('完整归档存在但待投影记录不可读取')
    # Archive API must return every row (including invalid) in exact source order.
    indices=[int(row['row_index']) for row in rows]
    if indices!=list(range(offset+1,offset+1+len(rows))) or indices[-1]>total:
        raise RuntimeError('归档行号不连续，投影已停止以防跳过材料')
    prepared=[]
    for raw in rows:
        document=None;error=''
        if not raw.get('valid',False):error='原始记录格式无效，原档仍完整保留'
        elif not raw.get('url'):error='来源未给出可投影的报道地址，原档仍完整保留'
        else:
            try:document=_lake_document(raw)
            except (ValueError,TypeError):error='来源字段未通过投影校验，原档仍完整保留'
        prepared.append((raw,document,error))
    with Session(engine) as s:
        lock_news(s,'intake:batch:'+key)
        batch=s.get(IntakeBatch,key)
        if not batch or batch.state!='archived' or batch.projected_offset!=offset:return True
        # Acquire article identities in one deterministic order before any row
        # mutation. Different batches may observe the same URLs concurrently.
        resources=sorted({'article:'+_document_identity(doc) for _,doc,_ in prepared if doc is not None})
        if resources:lock_news(s,*resources)
        started=time.monotonic()
        for raw,document,error in prepared:
            index=int(raw['row_index']);ident=_receipt_key(feed,stamp,index)
            if s.get(IntakeProjectionReceipt,ident):
                # Receipt and offset are one SQL transaction. Recovery from a
                # deliberately rewound cursor reuses the receipt without rewriting.
                batch.projected_offset=index
            else:
                article=None;version=None
                if document is not None:
                    article,version=_upsert(s,document,pointer=dict(feed=feed,stamp=stamp,row_index=index),discovery=True,locked=True)
                    meta=dict(document.metadata,record_id=document.record_id,intake_feed=feed,
                        intake_batch=stamp,intake_row=index,title_missing=not bool(document.title.strip()))
                    previous=s.get(GkgSource,article.article_key)
                    previous_stamp=json.loads(previous.attributes or '{}').get('intake_batch',previous.record_id[:14]) if previous else ''
                    attributes=json.dumps(meta,ensure_ascii=False,sort_keys=True)
                    if not previous or (stamp>=previous_stamp and (previous.record_id!=document.record_id or previous.attributes!=attributes)):
                        s.merge(GkgSource(article_key=article.article_key,record_id=document.record_id,attributes=attributes))
                    existing_discovery=s.get(GdeltDiscovery,article.article_key)
                    discovery=existing_discovery or GdeltDiscovery(article_key=article.article_key)
                    when=datetime.strptime(stamp,'%Y%m%d%H%M%S').strftime('%Y-%m-%dT%H:%M:%SZ')
                    if not existing_discovery or not discovery.seen_at or when<discovery.seen_at:
                        discovery.seen_at=when;s.add(discovery)
                s.add(IntakeProjectionReceipt(key=ident,feed=feed,stamp=stamp,row_index=index,
                    source_record_id=str(raw.get('record_id') or ''),status='skipped' if error else 'projected',
                    article_key=article.article_key if article else '',revision=version.revision if version else 0,error=error))
                batch.projected_offset=index
            # The budget bounds a transaction, never collection completeness.
            # At least one row is acknowledged; the next tick resumes exactly.
            if (time.monotonic()-started)*1000>=budget:break
        s.add(batch);s.commit()
    return True


def projection_status(engine,worker=None):
    from .models import IntakeBatch
    with Session(engine) as s:
        counts=dict(s.exec(select(IntakeProjectionReceipt.status,func.count())
            .where(~IntakeProjectionReceipt.feed.startswith('external:'))
            .group_by(IntakeProjectionReceipt.status)).all())
        external=s.exec(select(func.count()).select_from(IntakeProjectionReceipt)
            .where(IntakeProjectionReceipt.feed.startswith('external:'))).one()
        batches=s.exec(select(IntakeBatch).where(IntakeBatch.state=='archived')).all()
        return dict(projected=counts.get('projected',0),skipped=counts.get('skipped',0),
            waiting=sum(max(0,b.rows-b.projected_offset) for b in batches),
            unresolved_batches=sum(b.rows>b.projected_offset for b in batches),
            last_error=getattr(worker,'last_error',''),external_imported=external)


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
        article,version=_upsert(s,document,locked=True)
        receipt=IntakeProjectionReceipt(key=ident,feed='external:'+document.provider,stamp='',
            source_record_id=document.record_id,status='projected',article_key=article.article_key,
            revision=version.revision if version else 0,
            envelope_json=json.dumps(document.model_dump(exclude={'content'}),ensure_ascii=False))
        s.add(receipt);s.flush();result=dict(_receipt_data(receipt),reused=False)
        s.commit();return result


class ProjectionWorker:
    def __init__(self,engine,storage_root):
        self.engine,self.storage_root=engine,storage_root
        self.stop_event=StopEvent();self.thread=None
        self.last_error='';self.failures=0

    @property
    def running(self):return bool(self.thread and self.thread.is_alive())

    def step(self,limit=None):return project_step(self.engine,self.storage_root,limit)

    def start(self):
        if self.thread and self.thread.is_alive():return
        if os.getenv('NEWS2DOUYIN_GKG_PROJECTION_ENABLED','1').lower() in ('0','false','off'):return
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
                delay=min(60,2**min(self.failures-1,6))
            else:
                self.last_error='';self.failures=0
                delay=_setting('NEWS2DOUYIN_GKG_PROJECTION_INTERVAL',1,1) if changed else _setting('NEWS2DOUYIN_GKG_PROJECTION_IDLE_INTERVAL',3,1)
            self.stop_event.wait(delay)
