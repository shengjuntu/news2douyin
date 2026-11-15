"""Per-stream durable acquisition checkpoints and honest queue/coverage state."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import hashlib
import os
import re
import time
from uuid import uuid4
from zipfile import BadZipFile
import requests
from sqlmodel import Session,select
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from ..storage.articles import lock_news
from ..storage.models import utc_now_iso
from .models import FeedState,IntakeBatch
from .lake import Lake,hashes,positive_limit
from . import fetch_diagnostics as fetch_diag

BASE='https://data.gdeltproject.org/gdeltv2/'
from .feeds import FEEDS



class IntakeHTTPError(RuntimeError):
    def __init__(self,status,retry_after=0):
        self.status=status;self.retry_after=retry_after
        super().__init__('GDELT HTTP '+str(status))


def runtime_enabled():
    value=os.getenv('NEWS2DOUYIN_GDELT_STREAM_ENABLED','').strip()
    if not value:value=os.getenv('NEWS2DOUYIN_GKG_STREAM_ENABLED','1').strip()
    return value.lower() not in ('0','false','off','no')


def stamp_checked(value):
    if not re.fullmatch(r'\d{14}',value or ''):raise ValueError('批次须为14位UTC时间，例如20261104120000')
    dt=datetime.strptime(value,'%Y%m%d%H%M%S')
    if dt.second or dt.minute%15:raise ValueError('批次时间须为UTC整刻')
    return value


def next_stamp(value):return (datetime.strptime(stamp_checked(value),'%Y%m%d%H%M%S')+timedelta(minutes=15)).strftime('%Y%m%d%H%M%S')


def url_for(feed,stamp):
    if feed not in FEEDS:raise ValueError('未知接收流')
    return BASE+stamp_checked(stamp)+FEEDS[feed]['suffix']


def timeout():
    from ..collect.providers.gdelt import request_timeouts
    return request_timeouts()


def response_error(response):
    if response.status_code==200:return
    delay=0
    if response.status_code==429:
        raw=response.headers.get('Retry-After','')
        try:delay=max(0,float(raw))
        except ValueError:
            from email.utils import parsedate_to_datetime
            try:delay=max(0,parsedate_to_datetime(raw).timestamp()-time.time())
            except (ValueError,TypeError,OverflowError):pass
    raise IntakeHTTPError(response.status_code,delay)


def discover(feed):
    if feed not in FEEDS:raise ValueError('未知接收流')
    http_status=None
    try:
        with requests.get(BASE+FEEDS[feed]['manifest'],timeout=timeout(),allow_redirects=False,stream=True) as response:
            http_status=response.status_code
            response_error(response);parts=[];size=0
            for part in response.iter_content(8192):
                size+=len(part)
                if size>65536:raise ValueError('update_manifest_too_large')
                parts.append(part)
        candidates=[]
        for line in b''.join(parts).decode('utf8').splitlines():
            fields=line.split()
            if len(fields)!=3 or not fields[0].isdigit() or not re.fullmatch('[0-9a-fA-F]{32}',fields[1]):continue
            pattern=r'https?://data\.gdeltproject\.org/gdeltv2/(\d{14})'+re.escape(FEEDS[feed]['suffix'])
            matched=re.fullmatch(pattern,fields[2])
            if matched:
                stamp=stamp_checked(matched[1]);candidates.append(dict(stamp=stamp,bytes=int(fields[0]),md5=fields[1].lower(),url=url_for(feed,stamp),
                    manifest_url=BASE+FEEDS[feed]['manifest'],listed_url=fields[2],http_status=http_status))
        if not candidates:raise ValueError('update_manifest_missing_'+FEEDS[feed]['kind'])
        return max(candidates,key=lambda item:item['stamp'])
    except Exception as exc:
        exc.intake_http_status=http_status
        raise


def download(feed,stamp,destination,expected_md5='',expected_bytes=0,stop=None):
    path=Path(destination);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid4().hex+'.part')
    maximum=positive_limit('NEWS2DOUYIN_GKG_MAX_ARCHIVE_BYTES',256*1024*1024)
    sha=hashlib.sha256();md5=hashlib.md5();size=0
    http_status=None
    try:
        with temporary.open('wb') as out:
            with requests.get(url_for(feed,stamp),stream=True,timeout=timeout(),allow_redirects=False) as response:
                http_status=response.status_code
                response_error(response)
                for block in response.iter_content(131072):
                    if stop is not None and stop.is_set():raise InterruptedError('intake_stopping')
                    size+=len(block)
                    if size>maximum:raise ValueError('archive_download_size_limit')
                    sha.update(block);md5.update(block);out.write(block)
            out.flush();os.fsync(out.fileno())
        if expected_bytes and size!=expected_bytes:raise ValueError('archive_size_mismatch')
        if expected_md5 and md5.hexdigest().lower()!=expected_md5.lower():raise ValueError('archive_md5_mismatch')
        if not temporary.exists():raise ValueError('empty_archive_download')
        os.replace(temporary,path)
        if hasattr(os,'O_DIRECTORY'):
            try:
                descriptor=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
                try:os.fsync(descriptor)
                finally:os.close(descriptor)
            except OSError:pass  # Directory fsync is not supported on every filesystem.
        return dict(sha256=sha.hexdigest(),md5=md5.hexdigest(),compressed_bytes=size,http_status=http_status)
    except Exception as exc:
        exc.intake_http_status=http_status
        raise
    finally:
        if temporary.exists():temporary.unlink()


def initialize(engine):
    with Session(engine) as s:
        missing=[key for key in FEEDS if not s.get(FeedState,key)]
    if not missing:return
    with Session(engine) as s:
        lock_news(s,*('intake:feed:'+key for key in missing))
        for key in missing:
            if not s.get(FeedState,key):s.add(FeedState(key=key))
        s.commit()


def recover_leases(engine):
    """Resume interrupted work; caller must own the receiver's OS lock."""
    with Session(engine) as s:
        keys=list(s.exec(select(FeedState.key).where(FeedState.lease_until>0)))
    if not keys:return False
    with Session(engine) as s:
        lock_news(s,*('intake:feed:'+key for key in sorted(keys)))
        for key in keys:
            row=s.get(FeedState,key)
            if row and row.lease_until>0:
                row.lease='';row.lease_until=0;s.add(row)
        s.commit()
    return True


def configure(engine,feed,enabled,start_batch=''):
    if feed not in FEEDS:raise ValueError('未知接收流')
    if start_batch:stamp_checked(start_batch)
    with Session(engine) as s:
        lock_news(s,'intake:feed:'+feed);row=s.get(FeedState,feed)
        if not row:raise KeyError(feed)
        has_batch=s.exec(select(IntakeBatch.key).where(IntakeBatch.feed==feed).limit(1)).first()
        if start_batch and start_batch!=row.next_batch and (row.next_batch or row.last_complete or has_batch or row.lease_until>time.time()):
            raise ValueError('已开始接收后不能移动游标；请保留补档顺序')
        row.enabled=bool(enabled)
        if start_batch:row.next_batch=start_batch
        row.updated_at=utc_now_iso();s.add(row);s.commit()
    return status(engine,None)


def retry(engine,feed):
    if feed not in FEEDS:raise ValueError('未知接收流')
    with Session(engine) as s:
        lock_news(s,'intake:feed:'+feed);row=s.get(FeedState,feed)
        if not row:raise KeyError(feed)
        row.enabled=True;row.retry_after=0;row.last_error='';s.add(row);s.commit()
    return dict(queued=True)


def status(engine,storage_root):
    with Session(engine) as s:
        feeds=[dict(f.model_dump(),label=FEEDS.get(f.key,{}).get('label',f.key)) for f in s.exec(select(FeedState).order_by(FeedState.key))]
        batches=[b.model_dump() for b in s.exec(select(IntakeBatch).order_by(IntakeBatch.stamp.desc(),IntakeBatch.feed).limit(20))]
        totals=s.exec(select(func.count(),func.coalesce(func.sum(IntakeBatch.rows),0),func.coalesce(func.sum(IntakeBatch.valid_rows),0),func.coalesce(func.sum(IntakeBatch.invalid_rows),0),func.coalesce(func.sum(IntakeBatch.projected_offset),0)).where(IntakeBatch.state=='archived')).one()
        diagnostics=fetch_diag.list_saved(s,{row['key']:row.get('lease','') for row in feeds})
    total,rows,valid,invalid,projected=map(int,totals)
    for feed in feeds:
        feed.pop('lease',None)
        feed['fetch_diagnostics']=diagnostics.get(feed['key'],{})
        try:feed['backlog_batches']=max(0,int((datetime.strptime(feed['latest_available'],'%Y%m%d%H%M%S')-datetime.strptime(feed['next_batch'],'%Y%m%d%H%M%S')).total_seconds()//900)+1)
        except ValueError:feed['backlog_batches']=0
    return dict(feeds=feeds,batches=batches,fetch_diagnostics=list(diagnostics.values()),counters=dict(batches=total,rows=rows,valid_rows=valid,invalid_rows=invalid,projected_rows=projected,processed_rows=projected,backlog_rows=max(0,rows-projected)),
        runtime_enabled=runtime_enabled(),lake_exists=None,lake_state='not_probed_in_status',
        initial_scope='每条流首次默认从发现的最新批次开始；不是历史全库。此后按15分钟逐批补齐，遇失败不跳过。')


def _probe_archive(feed,stamp,expected_md5='',expected_bytes=0):
    """Request only headers via streaming GET; never retain archive contents."""
    result=dict(status='not_requested',stamp=stamp,request_url='',url='',http_status=None,
        error_kind='',message='',attempted_at=utc_now_iso(),completed_at='',
        expected_md5=expected_md5,expected_bytes=expected_bytes,archive_verified=False)
    if not stamp:
        result['message']='尚无可检查的批次时间';return result
    result['request_url']=result['url']=url_for(feed,stamp)
    try:
        with requests.get(result['request_url'],timeout=timeout(),allow_redirects=False,stream=True) as response:
            result['http_status']=response.status_code
            response_error(response)
            result.update(status='succeeded',message='已取得HTTP响应头；未下载完整文件，未验证ZIP、大小或MD5')
    except Exception as exc:
        if not hasattr(exc,'intake_http_status'):exc.intake_http_status=result['http_status']
        result.update(status='failed',**fetch_diag.error_details(exc,'download'))
    result['completed_at']=utc_now_iso()
    return result


def probe(engine,feed):
    """Explicit official-source diagnostic, with no SQL writes or cursor moves.

    End the read transaction before HTTP. The result is a transient observation,
    not a successful collection receipt; a 200 response is not ZIP verification.
    """
    if feed not in FEEDS:raise ValueError('未知接收流')
    with Session(engine) as s:
        row=s.get(FeedState,feed)
        target=row.next_batch if row else ''
        cached_latest=row.latest_available if row else ''
        cached_md5=row.latest_md5 if row else ''
        cached_bytes=row.latest_bytes if row else 0
        batch=s.get(IntakeBatch,feed+'_'+target) if target else None
        expected_md5=batch.expected_md5 if batch else ''
        expected_bytes=batch.expected_bytes if batch else 0
    manifest_url=BASE+FEEDS[feed]['manifest']
    result=dict(feed=feed,target_stamp=target,latest_stamp=cached_latest,
        latest_from='stored_state' if cached_latest else 'unknown',manifest_url=manifest_url,
        attempted_at=utc_now_iso(),completed_at='',cursor_unchanged=True,
        transport_policy='https_only',timeout_policy='same_as_intake',
        manifest=dict(status='failed',request_url=manifest_url,url=manifest_url,http_status=None,
            stamp='',error_kind='',message=''),target={},latest={})
    try:
        found=discover(feed)
        result['latest_stamp']=found['stamp'];result['latest_from']='manifest'
        result['manifest'].update(status='succeeded',http_status=found.get('http_status'),stamp=found['stamp'],
            listed_url=found.get('listed_url',''),expected_md5=found['md5'],expected_bytes=found['bytes'],
            message='已读取官方清单；清单列出文件不等于归档下载和校验成功')
        latest_md5,latest_bytes=found['md5'],found['bytes']
    except Exception as exc:
        result['manifest'].update(**fetch_diag.error_details(exc,'manifest'))
        latest_md5,latest_bytes=cached_md5,cached_bytes
    latest=result['latest_stamp']
    if not target:target=latest
    result['target_stamp']=target
    if target and target==latest:expected_md5,expected_bytes=latest_md5,latest_bytes
    result['target']=_probe_archive(feed,target,expected_md5,expected_bytes)
    # One HTTP request is enough when the cursor and manifest identify the same
    # file. The aliases describe the same observation, not two successes.
    if target and target==latest:
        result['latest']=dict(result['target'],same_as_target=True)
    else:
        result['latest']=_probe_archive(feed,latest,latest_md5,latest_bytes)
    result['completed_at']=utc_now_iso()
    result['message']='检查只读取官方清单和响应头；未更改游标、重试时间、归档或采集状态。404原因尚待结合目标批次和清单核对。'
    return result


def step(engine,storage_root,feed=None,now=None,stop=None):
    now=time.time() if now is None else now
    # A receiver with no due work is read-only. Recheck the selected feed after
    # acquiring its scoped lock, so separate processes cannot claim it twice.
    with Session(engine) as s:
        query=select(FeedState.key).where(FeedState.enabled==True,FeedState.retry_after<=now,FeedState.lease_until<=now)
        if feed:query=query.where(FeedState.key==feed)
        selected=s.exec(query.order_by(FeedState.updated_at,FeedState.key).limit(1)).first()
    if not selected:return False
    with Session(engine,expire_on_commit=False) as s:
        lock_news(s,'intake:feed:'+selected)
        item=s.get(FeedState,selected)
        if not item or not item.enabled or item.retry_after>now or item.lease_until>now:return False
        token=uuid4().hex;item.lease=token;item.lease_until=now+1800;item.updated_at=utc_now_iso()
        manifest_due=not item.latest_available or now-item.manifest_checked>=60 or (item.next_batch and item.next_batch>item.latest_available)
        diag=fetch_diag.snapshot(item.key,attempt_id=token,target_stamp=item.next_batch,latest_stamp=item.latest_available,
            phase='manifest' if manifest_due else 'download',
            request_url=BASE+FEEDS[item.key]['manifest'] if manifest_due else url_for(item.key,item.next_batch or item.latest_available),
            manifest_url=BASE+FEEDS[item.key]['manifest'],expected_md5=item.latest_md5 if item.next_batch==item.latest_available else '',
            expected_bytes=item.latest_bytes if item.next_batch==item.latest_available else 0)
        fetch_diag.save(s,item.key,diag);s.add(item);s.commit()
    target=item.next_batch
    try:
        if not item.latest_available or now-item.manifest_checked>=60 or (target and target>item.latest_available):
            diag.update(phase='manifest',request_url=BASE+FEEDS[item.key]['manifest'])
            discovered=discover(item.key)
            item.latest_available=discovered['stamp'];item.latest_md5=discovered['md5'];item.latest_bytes=discovered['bytes'];item.manifest_checked=now
            diag.update(latest_stamp=item.latest_available,manifest_listed_url=discovered.get('listed_url',''),
                manifest_http_status=discovered.get('http_status'))
        if not target:target=item.latest_available
        diag.update(target_stamp=target,latest_stamp=item.latest_available)
        if target>item.latest_available:
            with Session(engine) as s:
                lock_news(s,'intake:feed:'+item.key);current=s.get(FeedState,item.key)
                if current.lease==token:
                    current.lease='';current.lease_until=0;current.retry_after=now+30
                    current.latest_available=item.latest_available;current.latest_md5=item.latest_md5;current.latest_bytes=item.latest_bytes;current.manifest_checked=item.manifest_checked
                    diag.update(status='waiting',completed_at=utc_now_iso(),message='当前游标晚于清单最新批次，等待上游发布；未跳过游标',http_status=diag.get('manifest_http_status'))
                    fetch_diag.save(s,item.key,diag);s.add(current);s.commit()
            return False
        stamp_checked(target)
        key=item.key+'_'+target
        with Session(engine,expire_on_commit=False) as s:
            lock_news(s,'intake:feed:'+item.key,'intake:batch:'+key)
            current=s.get(FeedState,item.key)
            if not current or current.lease!=token:return True
            batch=s.get(IntakeBatch,key) or IntakeBatch(key=key,feed=item.key,stamp=target)
            if target==item.latest_available:
                batch.expected_md5=item.latest_md5;batch.expected_bytes=item.latest_bytes
            diag.update(status='running',phase='download',request_url=url_for(item.key,target),http_status=None,
                expected_md5=batch.expected_md5,expected_bytes=batch.expected_bytes)
            fetch_diag.save(s,item.key,diag)
            s.add(batch);s.commit()
        lake=Lake(storage_root);path=lake.root/'raw'/item.key/(target+FEEDS[item.key]['suffix'])
        diag['archive_reused']=path.exists()
        if not path.exists():
            fetched=download(item.key,target,path,batch.expected_md5,batch.expected_bytes,stop)
            diag['http_status']=fetched.get('http_status') if isinstance(fetched,dict) else None
        diag.update(phase='archive')
        sha,md5,compressed_bytes=hashes(path)
        with Session(engine) as s:
            lock_news(s,'intake:feed:'+item.key,'intake:batch:'+key)
            current=s.get(FeedState,item.key)
            if not current or current.lease!=token:return True
            saved=s.get(IntakeBatch,key)
            saved.archive_path=str(path.resolve());saved.sha256=sha;saved.md5=md5;saved.compressed_bytes=compressed_bytes
            # Lease verification and diagnostics publication share the feed
            # lock, so a new claim cannot interleave between check and write.
            fetch_diag.save(s,item.key,diag)
            s.add(saved);s.commit()
        try:result=lake.ingest_archive(item.key,target,path,batch.expected_md5,batch.expected_bytes)
        except (ValueError,BadZipFile):
            if path.exists():
                sha,_,_=hashes(path);quarantine=path.with_name(path.name+'.rejected-'+sha[:16])
                with Session(engine) as s:
                    lock_news(s,'intake:feed:'+item.key,'intake:batch:'+key)
                    current=s.get(FeedState,item.key)
                    if not current or current.lease!=token:return True
                    os.replace(path,quarantine)
                    saved=s.get(IntakeBatch,key);saved.archive_path=str(quarantine.resolve());s.add(saved);s.commit()
            raise
        # DuckDB commit is durable before the transaction store advances.
        with Session(engine) as s:
            lock_news(s,'intake:feed:'+item.key,'intake:batch:'+key);current=s.get(FeedState,item.key)
            if current.lease!=token:return True
            stored=s.get(IntakeBatch,key)
            for name,value in result.items():setattr(stored,name,value)
            stored.state='archived';stored.last_error='';s.add(stored)
            current.next_batch=next_stamp(target);current.last_complete=target;current.latest_available=item.latest_available
            current.latest_md5=item.latest_md5;current.latest_bytes=item.latest_bytes;current.manifest_checked=item.manifest_checked
            current.last_error='';current.retry_after=0;current.attempts=0;current.lease='';current.lease_until=0;current.updated_at=utc_now_iso();s.add(current)
            diag.update(status='succeeded',completed_at=utc_now_iso(),error_kind='',message='完整归档已校验并发布，接收游标已推进')
            fetch_diag.save(s,item.key,diag);s.commit()
        return True
    except SQLAlchemyError:
        # A database failure must reach the worker. Do not start another write
        # transaction just to record a database/lock failure in that same store.
        raise
    except Exception as exc:
        if diag['phase']=='archive' and not hasattr(exc,'intake_http_status'):
            exc.intake_http_status=diag.get('http_status')
        diag.update(status='failed',completed_at=utc_now_iso(),**fetch_diag.error_details(exc,diag['phase']))
        delay=max(min(30*2**min(item.attempts,7),3600),getattr(exc,'retry_after',0))
        prefix='GDELT HTTP '+str(diag['http_status'])+' · ' if diag.get('http_status') not in (None,200) else ''
        message=prefix+diag['error_kind']+' · '+diag['message']
        with Session(engine) as s:
            resources=['intake:feed:'+item.key]
            if target:resources.append('intake:batch:'+item.key+'_'+target)
            lock_news(s,*resources);current=s.get(FeedState,item.key)
            if current.lease!=token:return True
            current.next_batch=target or current.next_batch;current.last_error=message;current.retry_after=now+delay
            current.attempts+=1;current.lease='';current.lease_until=0;current.updated_at=utc_now_iso()
            current.latest_available=item.latest_available;current.latest_md5=item.latest_md5;current.latest_bytes=item.latest_bytes;current.manifest_checked=item.manifest_checked;s.add(current)
            fetch_diag.save(s,item.key,diag)
            if target:
                key=item.key+'_'+target;batch=s.get(IntakeBatch,key) or IntakeBatch(key=key,feed=item.key,stamp=target)
                batch.state='error';batch.last_error=message;s.add(batch)
            s.commit()
        return True
