"""Durable, bounded source-content enrichment without network in page reads."""
from __future__ import annotations
from datetime import datetime,timedelta,timezone
import logging
import os
from pathlib import Path
from threading import Event,Lock,Thread
import time
from uuid import uuid4

from sqlalchemy import or_,union_all
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session,select

from ..intake.official_models import OfficialEvent,OfficialMention
from ..intake.runtime import ReceiverLock,ReceiverBusy
from ..storage.articles import identity_key,lock_news
from ..storage.models import utc_now_iso
from ..tasks.control import TaskConflict
from .source_content import resolve_previews,source_url,usable_title,preview_hash,text,local_basis,MAX_TITLE,MAX_EXCERPT
from .source_models import SourcePreview,SourcePreviewJob

log=logging.getLogger(__name__)
ACTIVE=('pending','running','failed')


def _event_urls(session, event_ids):
    rows=session.exec(select(OfficialEvent.event_id,OfficialEvent.source_url)
        .where(OfficialEvent.event_id.in_(event_ids))).all()
    found={key for key,_ in rows};urls=[url for _,url in rows if source_url(url)]
    if found:
        # A LIMIT under each branch can use the existing event/recent index.
        # row_number over all mentions would still scan/sort a huge event even
        # when only its first two URLs were returned.
        branches=[]
        for event_id in sorted(found):
            recent=select(OfficialMention.doc_identifier).where(OfficialMention.event_id==event_id)
            # Ties deliberately remain arbitrary. A secondary key sort would
            # inspect every same-batch mention before LIMIT on the existing
            # (event_id,last_seen) index, defeating the bounded seek.
            recent=recent.order_by(OfficialMention.last_seen.desc()).limit(2).subquery()
            branches.append(select(recent.c.doc_identifier))
        candidates=union_all(*branches).subquery()
        urls.extend(url for url in session.exec(select(candidates.c.doc_identifier))
                    if source_url(url))
    return list(dict.fromkeys(urls)),found


def _enqueue(session, urls, *, priority=100, retry=False, active_limit=1000):
    lock_news(session,'source-preview-queue')
    active=session.exec(select(SourcePreviewJob.key).where(SourcePreviewJob.state.in_(ACTIVE),
        SourcePreviewJob.attempts<3).limit(active_limit+1)).all()
    jobs={r.key:r for r in session.exec(select(SourcePreviewJob).where(
        SourcePreviewJob.key.in_([identity_key({'url':u}) for u in urls])))} if urls else {}
    cached=resolve_previews(session,urls) if urls else {}
    capacity=max(0,active_limit-len(active));queued=0;existing=0;throttled=0
    for url in urls:
        key=identity_key({'url':url});row=jobs.get(key)
        preview=cached[url];basis=local_basis(preview)
        if preview['ready'] and preview['has_excerpt']:
            existing+=1;continue
        if row is None:
            if not capacity:throttled+=1;continue
            row=SourcePreviewJob(key=key,source_url=url,priority=priority,basis_hash=basis);capacity-=1;queued+=1
            session.add(row);jobs[key]=row
        elif (retry or basis and row.basis_hash!=basis) and row.state not in ('running',):
            if row.state not in ACTIVE:
                if not capacity:throttled+=1;continue
                capacity-=1
            row.state='pending';row.attempts=0;row.retry_at=0;row.error='';row.error_kind='';row.priority=max(priority,row.priority)
            row.basis_hash=basis;row.updated_at=utc_now_iso();session.add(row);queued+=1
        else:
            if row.state in ACTIVE and row.attempts<3:
                row.priority=max(priority,row.priority);session.add(row)
            existing+=1
    return dict(queued=queued,existing=existing,throttled=throttled)


def enqueue_events(engine,event_ids,*,retry=False):
    if not isinstance(event_ids,list) or len(event_ids)>30 or any(not isinstance(v,str) or not v.isdigit() or len(v)>30 for v in event_ids):
        raise ValueError('一次最多补充30条有效事件线索')
    event_ids=list(dict.fromkeys(event_ids))
    with Session(engine) as s:
        urls,found=_event_urls(s,event_ids)
        result=_enqueue(s,urls,priority=1000,retry=bool(retry))
        s.commit()
    return dict(result,events=len(found),missing=[v for v in event_ids if v not in found],
        source_urls=len(urls),state='no_source' if not urls else 'throttled' if result['throttled'] else 'queued' if result['queued'] else 'existing',
        note='尚无可读取的关联报道地址，不能仅凭编码生成内容。' if not urls else
             '只补关联报道的标题与摘要；不确认事件内容，不推进原始接收游标。')


def status(engine,worker=None):
    with Session(engine) as s:
        rows=s.exec(select(SourcePreviewJob.state,SourcePreviewJob.error,SourcePreviewJob.updated_at)
            .where(SourcePreviewJob.state.in_(ACTIVE)).order_by(SourcePreviewJob.priority.desc(),SourcePreviewJob.requested_at)
            .limit(201)).all()
        unavailable=s.exec(select(SourcePreviewJob.error).where(SourcePreviewJob.state=='unavailable')
            .order_by(SourcePreviewJob.updated_at.desc()).limit(1)).first() or ''
        completed=s.exec(select(SourcePreviewJob.finished_at).where(SourcePreviewJob.state=='ready')
            .order_by(SourcePreviewJob.updated_at.desc()).limit(1)).first() or ''
    return dict(enabled=getattr(worker,'enabled',True),running=bool(worker and worker.running),
        state=getattr(worker,'state','idle'),pending=sum(row[0] in ('pending','running','failed') for row in rows[:200]),
        pending_limited=len(rows)>200,error=getattr(worker,'error','') or next((r[1] for r in rows if r[1]),'') or unavailable,
        last_processed=completed,
        scope='自动补充最近7天中最新60条事件的来源；页面显式请求优先；每版本最多自动尝试3次，已有标题缺摘要只自动补一次。')


class SourceContentWorker:
    def __init__(self,engine,storage_root):
        self.engine,self.storage_root=engine,str(storage_root)
        self.enabled=os.getenv('NEWS2DOUYIN_SOURCE_CONTENT_ENABLED','1').lower() not in ('0','false','off','no')
        self.stop_event=Event();self.thread=None;self._step_lock=Lock();self._discover_at=0.
        self.state='idle' if self.enabled else 'disabled';self.error=''

    @property
    def running(self):return bool(self.thread and self.thread.is_alive())

    def start(self):
        if not self.enabled or self.running:return
        self.stop_event.clear();self.thread=Thread(target=self._loop,name='source-content',daemon=True);self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:self.thread.join(timeout=5)

    def _discover(self):
        now=time.monotonic()
        if now<self._discover_at:return False
        self._discover_at=now+30
        lower=(datetime.now(timezone.utc)-timedelta(days=7)).isoformat(timespec='seconds').replace('+00:00','Z')
        with Session(self.engine) as s:
            keys=s.exec(select(OfficialEvent.event_id).where(OfficialEvent.last_seen>=lower)
                .order_by(OfficialEvent.last_seen.desc(),OfficialEvent.event_id.desc()).limit(60)).all()
            if not keys:return False
            urls,_=_event_urls(s,keys)
            result=_enqueue(s,urls,priority=100,active_limit=200);s.commit()
        return bool(result['queued'])

    def _claim(self):
        now=time.time()
        with Session(self.engine) as s:
            lock_news(s,'source-preview-queue')
            exhausted=s.exec(select(SourcePreviewJob).where(SourcePreviewJob.state=='running',
                SourcePreviewJob.lease_until<now,SourcePreviewJob.attempts>=3)
                .order_by(SourcePreviewJob.lease_until,SourcePreviewJob.key).limit(1)).first()
            if exhausted:
                exhausted.state='unavailable';exhausted.lease='';exhausted.lease_until=0
                exhausted.error_kind='interrupted';exhausted.error='上次补充未完成，自动尝试已达上限，请明确重试。'
                exhausted.updated_at=utc_now_iso();s.add(exhausted)
            row=s.exec(select(SourcePreviewJob).where(or_(
                (SourcePreviewJob.state.in_(('pending','failed')))&(SourcePreviewJob.retry_at<=now)&(SourcePreviewJob.attempts<3),
                (SourcePreviewJob.state=='running')&(SourcePreviewJob.lease_until<now)&(SourcePreviewJob.attempts<3)))
                .order_by(SourcePreviewJob.priority.desc(),SourcePreviewJob.requested_at,SourcePreviewJob.key).limit(1)).first()
            if row is None:
                if exhausted:s.commit()
                else:s.rollback()
                return None
            row.state='running';row.lease=uuid4().hex;row.lease_until=now+180;row.attempts+=1;row.updated_at=utc_now_iso()
            s.add(row);data=dict(key=row.key,url=row.source_url,token=row.lease,attempts=row.attempts);s.commit();return data

    def _heartbeat(self,job,done):
        while not done.wait(30):
            try:
                with Session(self.engine) as s:
                    lock_news(s,'source-preview-queue');row=s.get(SourcePreviewJob,job['key'])
                    if not row or row.state!='running' or row.lease!=job['token']:return
                    row.lease_until=time.time()+180;s.add(row);s.commit()
            except SQLAlchemyError:log.warning('Source preview lease renewal deferred',exc_info=True)

    def _publish(self,job,preview):
        with Session(self.engine) as s:
            lock_news(s,'source-preview-queue');row=s.get(SourcePreviewJob,job['key'])
            if not row or row.state!='running' or row.lease!=job['token'] or row.lease_until<time.time():return False
            if not usable_title(preview.get('title')):raise ValueError('未取得可读的报道标题')
            value=dict(source_url=job['url'],final_url=source_url(preview.get('final_url')) or job['url'],
                title=text(preview.get('title'),MAX_TITLE),excerpt=text(preview.get('excerpt'),MAX_EXCERPT),
                language=str(preview.get('language') or '')[:40],published_at=str(preview.get('published_at') or '')[:100],
                domain=str(preview.get('domain') or '')[:300],origin=str(preview.get('origin') or 'source_page')[:40],
                title_origin=str(preview.get('title_origin') or '')[:40],excerpt_origin=str(preview.get('excerpt_origin') or '')[:40],
                article_key=str(preview.get('article_key') or '')[:160],fetched_at=utc_now_iso())
            cached=s.get(SourcePreview,job['key']) or SourcePreview(key=job['key'],source_url=job['url'])
            for name,val in value.items():setattr(cached,name,val)
            cached.content_hash=preview_hash(value);s.add(cached)
            current=resolve_previews(s,[job['url']])[job['url']]
            row.basis_hash=local_basis(current) or row.basis_hash
            row.state='ready';row.lease='';row.lease_until=0;row.retry_at=0;row.error='';row.error_kind=''
            row.finished_at=row.updated_at=utc_now_iso();s.add(row);s.commit();return True

    def _process(self,job):
        done=Event();heartbeat=Thread(target=self._heartbeat,args=(job,done),daemon=True);heartbeat.start()
        title_only=False;basis=''
        try:
            with Session(self.engine) as s:preview=resolve_previews(s,[job['url']])[job['url']]
            title_only=bool(preview['ready'] and not preview['has_excerpt'])
            basis=local_basis(preview)
            if not preview['ready'] or title_only:
                from .source_fetch import fetch_preview
                preview=fetch_preview(job['url'])
            self._publish(job,preview);self.error='';self.state='idle'
        except SQLAlchemyError:
            raise
        except Exception as exc:
            kind=str(getattr(exc,'kind','source_unavailable'))[:60]
            message=str(getattr(exc,'message','未能取得报道内容，请查看来源或稍后重试。'))[:300]
            with Session(self.engine) as s:
                lock_news(s,'source-preview-queue');row=s.get(SourcePreviewJob,job['key'])
                if row and row.lease==job['token'] and row.state=='running':
                    row.state='unavailable' if row.attempts>=3 or title_only else 'failed'
                    row.basis_hash=basis or row.basis_hash
                    row.lease='';row.lease_until=0;row.error=message;row.error_kind=kind
                    row.retry_at=time.time()+(60 if row.attempts==1 else 300 if row.attempts==2 else 86400)
                    row.updated_at=utc_now_iso();s.add(row);s.commit()
            self.error=message;self.state='error'
        finally:done.set();heartbeat.join(timeout=1)

    def step(self):
        if not self.enabled or not self._step_lock.acquire(blocking=False):return False
        owner=ReceiverLock(self.storage_root);owner.path=Path(self.storage_root).resolve()/'sources'/'preview-worker.lock'
        try:
            try:owner.acquire()
            except ReceiverBusy:return False
            self.state='idle';self.error=''
            discovered=self._discover();job=self._claim()
            if job:self.state='processing';self._process(job);return True
            return discovered
        finally:owner.release();self._step_lock.release()

    def _loop(self):
        while not self.stop_event.is_set():
            try:changed=self.step()
            except Exception:
                self.error='报道内容后台暂不可用，请检查数据库或服务日志。';self.state='error'
                log.warning('Source content work deferred',exc_info=True);changed=False
            self.stop_event.wait(1 if changed and not self.error else 5)
