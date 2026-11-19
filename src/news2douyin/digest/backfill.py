"""Durable, date-scoped intake repair. The owning receiver processes one slot per turn."""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
import json
import logging
import time
from uuid import uuid4
from zipfile import BadZipFile

import requests
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from ..intake import service as intake
from ..intake.feeds import FEEDS
from ..intake.lake import Lake, hashes
from ..intake.models import DailyRepairJob, DailyRepairPart, FeedState, IntakeBatch
from ..storage.articles import lock_news
from ..storage.models import utc_now_iso
from .calendar import BEIJING, day_bounds_utc

ACTIVE = ('queued', 'running', 'submit_digest', 'awaiting_digest')
SLOT = timedelta(minutes=15)
log = logging.getLogger(__name__)


def _day_window(day):
    start, end = day_bounds_utc(day)
    return start.strftime('%Y%m%d%H%M%S'), end.strftime('%Y%m%d%H%M%S')


def _stamps(day):
    start, end = day_bounds_utc(day)
    stamps = []
    while start < end:
        stamps.append(start.strftime('%Y%m%d%H%M%S'))
        start += SLOT
    return stamps


def _feeds(session):
    configured = dict(session.exec(select(FeedState.key, FeedState.enabled)).all())
    return [feed for feed in FEEDS if configured.get(feed, True)]


def _summary(session, jobs):
    if not jobs:
        return []
    days = [job.day for job in jobs]
    counts = defaultdict(Counter)
    query = (select(DailyRepairPart.day, DailyRepairPart.state, func.count())
             .where(DailyRepairPart.day.in_(days)).group_by(DailyRepairPart.day, DailyRepairPart.state))
    for day, state, n in session.exec(query):
        counts[day][state] = n
    summaries = []
    for job in jobs:
        totals = counts[job.day]
        # Only inspect failed rows when present. Never put a large list of all
        # 576 historical slots onto the management page.
        errors = []
        if totals['failed'] or totals['retry']:
            q = (select(DailyRepairPart).where(DailyRepairPart.day == job.day,
                                              DailyRepairPart.state.in_(('failed','retry')))
                 .order_by(DailyRepairPart.stamp, DailyRepairPart.feed).limit(5))
            errors = [dict(feed=r.feed, stamp=r.stamp, state=r.state, error=r.error) for r in session.exec(q)]
        summaries.append(dict(day=job.day, id=job.id, status=job.status, total=sum(totals.values()),
                              done=totals['done'], failed=totals['failed'], pending=totals['pending']+totals['retry'],
                              retrying=totals['retry'], last_error=job.last_error, feeds=json.loads(job.feeds_json),
                              next_at=job.next_at, created_at=job.created_at, updated_at=job.updated_at,
                              examples=errors))
    return summaries


def list_jobs(engine, limit=30):
    with Session(engine) as session:
        jobs = list(session.exec(select(DailyRepairJob).order_by(DailyRepairJob.updated_at.desc(),
                        DailyRepairJob.day.desc()).limit(max(1,min(100,limit)))))
        return _summary(session, jobs)


def coverage(engine, start, end, *, now=None):
    """Cheap receipt-ledger view. The repair worker verifies the DuckDB manifest separately."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(BEIJING).date()
    if start > end or (end-start).days >= 31 or end > today:
        raise ValueError('请选择至多31个北京时间自然日，结束日期不能晚于今天')
    first, stop = _day_window(start)[0], _day_window(end)[1]
    latest = now.astimezone(timezone.utc).strftime('%Y%m%d%H%M%S')
    with Session(engine) as s:
        feeds = _feeds(s)
        have = {(f,t):state for f,t,state in s.exec(select(IntakeBatch.feed,IntakeBatch.stamp,IntakeBatch.state)
                     .where(IntakeBatch.stamp >= first, IntakeBatch.stamp < stop, IntakeBatch.feed.in_(feeds)))}
        parts = s.exec(select(DailyRepairPart.feed, DailyRepairPart.stamp, DailyRepairPart.state).where(
                       DailyRepairPart.day >= start.isoformat(), DailyRepairPart.day <= end.isoformat(),
                       DailyRepairPart.state.in_(('failed','retry'))))
        for f,t,state in parts:
            have[f,t] = state
        jobs = {j['day']:j for j in _summary(s,list(s.exec(select(DailyRepairJob).where(
                       DailyRepairJob.day >= start.isoformat(), DailyRepairJob.day <= end.isoformat()))))}
    days = []
    for offset in range((end-start).days+1):
        day = start + timedelta(days=offset)
        slots = _stamps(day)
        due = [stamp for stamp in slots if stamp <= latest]
        rows = []
        for feed in feeds:
            missing = [stamp for stamp in due if have.get((feed,stamp)) != 'archived']
            rows.append(dict(feed=feed, label=FEEDS[feed]['label'], expected=len(due),
                             stored=len(due)-len(missing), missing=len(missing),
                             failed=sum(have.get((feed,t)) in ('error','failed','retry') for t in missing),
                             not_due=len(slots)-len(due), missing_examples=missing[:3]))
        totals = {key:sum(row[key] for row in rows) for key in ('expected','stored','missing','failed','not_due')}
        days.append(dict(day=day.isoformat(), feeds=rows, job=jobs.get(day.isoformat()), **totals))
    return dict(days=days, enabled_feeds=feeds, receiver_enabled=intake.runtime_enabled())


def enqueue(engine, day, *, retry=False):
    if day >= datetime.now(BEIJING).date():
        raise ValueError('补采任务仅适用于已结束的北京时间日期')
    if not intake.runtime_enabled():
        raise RuntimeError('全量接收器已停用，无法执行历史补采')
    with Session(engine) as s:
        lock_news(s,'daily:repair:'+day.isoformat())
        feeds = _feeds(s)
        if not feeds:
            raise ValueError('请先启用至少一条 GDELT 接收流')
        job = s.get(DailyRepairJob, day.isoformat())
        if job and (job.status in ACTIVE or (not retry and json.loads(job.feeds_json)==feeds)):
            return _summary(s,[job])[0]
        if job is None:
            job = DailyRepairJob(day=day.isoformat(),id='')
        else:
            s.exec(DailyRepairPart.__table__.delete().where(DailyRepairPart.day == job.day))
        job.id = uuid4().hex
        job.feeds_json = json.dumps(feeds)
        job.status = 'queued'
        job.partial_publish = False
        job.created_at = job.updated_at = utc_now_iso()
        job.next_at = 0
        job.last_error = ''
        s.add(job)
        slots = _stamps(day)
        s.add_all([DailyRepairPart(key=feed+'_'+stamp,day=job.day,feed=feed,stamp=stamp)
                   for feed in feeds for stamp in slots])
        s.commit()
        return _summary(s,[job])[0]


def restored_gkg(session, day):
    job = session.get(DailyRepairJob, day.isoformat())
    if job is None or not job.ready_id:
        return '', set()
    return job.ready_id, set(tuple(pair) for pair in json.loads(job.restored_json))


def _arm_digest(s, job):
    # Only explicitly checked, late archives for this exact upstream date are
    # added to the historical reader; the normal as-observed bounds stay intact.
    day = date.fromisoformat(job.day)
    end = day_bounds_utc(day)[1].strftime('%Y-%m-%dT%H:%M:%SZ')
    checked = set(json.loads(job.feeds_json))
    late = {tuple(pair) for pair in json.loads(job.restored_json) if pair[0] not in checked}
    for feed,stamp in s.exec(select(DailyRepairPart.feed, DailyRepairPart.stamp).where(
            DailyRepairPart.day == job.day, DailyRepairPart.state=='done',
            DailyRepairPart.feed.in_(('gkg_english','gkg_translation')), DailyRepairPart.completed_at >= end)):
        late.add((feed,stamp))
    job.ready_id = job.id
    job.restored_json = json.dumps(sorted(late))
    job.status = 'submit_digest'
    job.next_at = 0


def publish_available(engine, day):
    if not intake.runtime_enabled():
        raise RuntimeError('全量接收器已停用')
    with Session(engine) as s:
        lock_news(s, 'daily:repair:'+day.isoformat())
        job = s.get(DailyRepairJob,day.isoformat())
        if not job:
            raise ValueError('该日尚无补采任务')
        if job.status == 'partial':
            done = s.exec(select(DailyRepairPart.key).where(DailyRepairPart.day==job.day,
                                                             DailyRepairPart.state=='done').limit(1)).first()
            if not done:
                raise ValueError('尚无可用于重算的已核对批次')
            job.partial_publish = True
            _arm_digest(s,job)
            job.updated_at = utc_now_iso()
            s.add(job)
            s.commit()
        elif job.status not in ('submit_digest','awaiting_digest','completed_partial'):
            raise ValueError('此任务不处于可发布部分结果的状态')
        return _summary(s,[job])[0]


def _reconcile_batch(session, part, receipt, stored):
    if stored is not None and stored.sha256 and stored.sha256 != receipt['sha256']:
        raise ValueError('archive checksum conflicts with intake ledger')
    if stored is None:
        stored = IntakeBatch(key=part.key, feed=part.feed, stamp=part.stamp)
    for name,value in receipt.items():
        setattr(stored,name,value)
    stored.state = 'archived'
    stored.last_error = ''
    session.add(stored)
    part.state = 'done'
    part.error = ''
    part.completed_at = receipt['completed_at']
    session.add(part)


def _prepare(engine, lake, day, token):
    start, stop = _day_window(day)
    with lake.connection() as conn:
        receipts = {}
        if conn is not None and 'intake_archives' in lake.tables(conn):
            cur = conn.execute('SELECT feed, stamp, sha256, md5, compressed_bytes, archive_path, '
                               'rows, valid_rows, invalid_rows, completed_at FROM intake_archives '
                               'WHERE stamp>=? AND stamp<?', [start,stop])
            names = [desc[0] for desc in cur.description[2:]]
            receipts = {(r[0],r[1]):dict(zip(names,r[2:])) for r in cur.fetchall()}
    with Session(engine) as s:
        lock_news(s, 'daily:repair:'+day.isoformat())
        job = s.get(DailyRepairJob,day.isoformat())
        if job.id != token or job.status != 'queued':
            return
        ledger = {batch.key: batch for batch in s.exec(select(IntakeBatch).where(
            IntakeBatch.feed.in_(json.loads(job.feeds_json)), IntakeBatch.stamp >= start, IntakeBatch.stamp < stop))}
        query = select(DailyRepairPart).where(DailyRepairPart.day==job.day)
        for part in s.exec(query).all():
            stored = ledger.get(part.key)
            rec = receipts.get((part.feed,part.stamp))
            if rec:
                try:
                    _reconcile_batch(s,part,rec,stored)
                except ValueError as exc:
                    part.state='failed'
                    part.error=job.last_error=str(exc)
                    s.add(part)
            else:
                if stored and stored.state=='archived':
                    stored.state='error'
                    stored.last_error='历史补采核对时未找到 DuckDB 归档'
                    s.add(stored)
        job.status='running'
        job.updated_at=utc_now_iso()
        s.add(job)
        s.commit()


def _record_failure(engine, day, token, part_key, exc, now):
    from ..intake.fetch_diagnostics import error_details
    code=getattr(exc,'status',None)
    transient = isinstance(exc,(requests.ConnectionError, requests.Timeout, OSError)) or code in (408,425,429) or (code and 500<=code<600)
    description = error_details(exc,'archive' if isinstance(exc,(BadZipFile,ValueError)) else 'download')
    message = (f'HTTP {code} · ' if code else '')+description['message']
    if isinstance(exc, ValueError) and str(exc)=='archive checksum conflicts with intake ledger':
        message = '补采文件与工作库记录的 SHA-256 不一致'
    with Session(engine) as s:
        lock_news(s,'daily:repair:'+day.isoformat())
        job = s.get(DailyRepairJob,day.isoformat())
        if job.id!=token or job.status!='running':
            return
        part = s.get(DailyRepairPart,part_key)
        part.attempts+=1
        part.state = 'retry' if transient and part.attempts<3 else 'failed'
        part.error=job.last_error=message
        delay=max(30*2**(part.attempts-1),getattr(exc,'retry_after',0))
        part.next_at=now+delay if part.state=='retry' else 0
        if code==429:
            job.next_at=now+delay
        job.updated_at=utc_now_iso()
        s.add(job)
        s.add(part)
        s.commit()


def _acquire_part(engine, lake, day, token, part, stop, now):
    path = lake.root/'raw'/part.feed/(part.stamp+FEEDS[part.feed]['suffix'])
    with Session(engine) as s:
        stored=s.get(IntakeBatch,part.key)
        expected_md5=stored.expected_md5 if stored else ''
        expected_bytes=stored.expected_bytes if stored else 0
        expected_sha=stored.sha256 if stored and stored.completed_at else ''
    try:
        if not path.exists():
            intake.download(part.feed,part.stamp,path,expected_md5,expected_bytes,stop=stop)
        try:
            if expected_sha and hashes(path)[0]!=expected_sha:
                raise ValueError('archive checksum conflicts with intake ledger')
            receipt = lake.ingest_archive(part.feed,part.stamp,path,expected_md5,expected_bytes, repair_day=day)
        except (BadZipFile,ValueError):
            # Keep damaged bytes for inspection and let an explicit retry fetch again.
            if path.exists():
                path.replace(path.with_name(path.name+'.rejected-'+hashes(path)[0][:16]))
            raise
    except InterruptedError:
        return
    except (intake.IntakeHTTPError, requests.RequestException, OSError, BadZipFile, ValueError) as exc:
        _record_failure(engine,day,token,part.key,exc,now)
        return
    with Session(engine) as s:
        lock_news(s,'daily:repair:'+day.isoformat(),'intake:batch:'+part.key)
        job=s.get(DailyRepairJob,day.isoformat())
        if job.id!=token or job.status!='running':
            return
        saved=s.get(DailyRepairPart,part.key)
        _reconcile_batch(s,saved,receipt,s.get(IntakeBatch,part.key))
        job.updated_at=utc_now_iso()
        s.add(job)
        s.commit()


def _submit_or_observe(engine, day, token, state, now):
    from .service import read_issue_state, request_rebuild
    with Session(engine) as s:
        issue=read_issue_state(s,day)
    acknowledged = issue.get('backfill_id') == token
    error = ''
    if not acknowledged and state=='submit_digest':
        try:
            request_rebuild(engine,day)
        except (ValueError, RuntimeError) as exc:
            error = str(exc)
    elif not acknowledged and issue.get('build_status')=='failed':
        error = '历史日报重算失败：' + (issue.get('error') or '未知原因')
    with Session(engine) as s:
        lock_news(s,'daily:repair:'+day.isoformat())
        job=s.get(DailyRepairJob,day.isoformat())
        if job.id!=token or job.status!=state:
            return
        if acknowledged:
            job.status='completed_partial' if job.partial_publish else 'completed'
            if not job.partial_publish:
                job.last_error=''
        elif error:
            job.status='failed'
            job.last_error=error
        else:
            job.status='awaiting_digest'
        job.next_at=now+5
        job.updated_at=utc_now_iso()
        s.add(job)
        s.commit()


def step(engine, storage_root, *, now=None, stop=None):
    """One unit of work, under the same OS receiver ownership as regular intake."""
    now = time.time() if now is None else now
    with Session(engine) as s:
        value = s.exec(select(DailyRepairJob.day, DailyRepairJob.id, DailyRepairJob.status)
                       .where(DailyRepairJob.status.in_(ACTIVE), DailyRepairJob.next_at<=now)
                       .order_by(DailyRepairJob.updated_at,DailyRepairJob.day).limit(1)).first()
    if not value:
        return False
    stamp, token, state = value
    try:
        _run_step(engine, storage_root, stamp, token, state, now, stop)
    except (SQLAlchemyError, InterruptedError):
        # The owning receiver handles DB backoff and reacquires leases.
        raise
    except Exception as exc:
        # Damage to the lake or a schema error must not starve other days and
        # live acquisition. Do not expose file names, URLs or server responses.
        log.exception('Historical repair failed for %s in %s', stamp, state)
        with Session(engine) as s:
            lock_news(s, 'daily:repair:'+stamp)
            job = s.get(DailyRepairJob, stamp)
            if job.id == token and job.status == state:
                job.status = 'failed'
                job.last_error = '历史补采失败 · ' + type(exc).__name__
                job.updated_at = utc_now_iso()
                s.add(job)
                s.commit()
    return True


def _run_step(engine, storage_root, stamp, token, state, now, stop):
    day = date.fromisoformat(stamp)
    if state=='queued':
        _prepare(engine,Lake(storage_root),day,token)
    elif state in ('submit_digest','awaiting_digest'):
        _submit_or_observe(engine,day,token,state,now)
    else:
        with Session(engine,expire_on_commit=False) as s:
            part=s.exec(select(DailyRepairPart).where(DailyRepairPart.day==stamp,
                        DailyRepairPart.state.in_(('pending','retry')), DailyRepairPart.next_at<=now)
                        .order_by(DailyRepairPart.stamp,DailyRepairPart.feed).limit(1)).first()
        if part:
            _acquire_part(engine,Lake(storage_root),day,token,part,stop,now)
        else:
            with Session(engine) as s:
                lock_news(s,'daily:repair:'+stamp)
                job=s.get(DailyRepairJob,stamp)
                if job.id!=token or job.status!=state:
                    return True
                pending=s.exec(select(func.min(DailyRepairPart.next_at)).where(
                            DailyRepairPart.day==stamp,DailyRepairPart.state.in_(('retry','pending')))).one()
                if pending is not None:
                    job.next_at=max(now+1,pending)
                else:
                    failure=s.exec(select(DailyRepairPart.error).where(
                            DailyRepairPart.day==stamp,DailyRepairPart.state=='failed').limit(1)).first()
                    if failure is not None:
                        job.status='partial'
                        job.last_error=failure
                    else:
                        _arm_digest(s,job)
                job.updated_at=utc_now_iso()
                s.add(job)
                s.commit()
    return True
