"""Durable background publication of archived NewsNow observations.

HTTP requests read PostgreSQL summaries. DuckDB and archive access happen only
here, outside SQL transactions. A process lock serializes the lake/publication
pipeline, while leased inbox rows make interrupted work replayable.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from threading import Event, Lock, Thread
import time
from uuid import uuid4

from sqlalchemy import or_
from sqlmodel import Session, select

from ..storage.articles import lock_news
from ..storage.models import RunRecord, utc_now_iso
from ..intake.runtime import ReceiverLock, ReceiverBusy
from .models import AttentionInbox, AttentionMeta

log = logging.getLogger(__name__)


def _manifest(root, value):
    root = Path(root).resolve()
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('NewsNow快照清单路径无效')
    relative = Path(value)
    target = (root / relative).resolve()
    if relative.is_absolute() or not target.is_relative_to(root) or '..' in relative.parts:
        raise ValueError('NewsNow快照清单必须位于采集目录内')
    if target.stat().st_size > 524288:
        raise ValueError('NewsNow快照清单过大')
    return relative.as_posix(), hashlib.sha256(target.read_bytes()).hexdigest()


def enqueue_manifest(engine, storage_root, report, run_id=None):
    """Freeze an archive reference, without reading raw lists into PostgreSQL."""
    value = report.get('manifest_path') if isinstance(report, dict) else report
    path, digest = _manifest(storage_root, value)
    key = hashlib.sha256(json.dumps([path, digest], separators=(',', ':')).encode()).hexdigest()
    with Session(engine) as s:
        lock_news(s, 'attention-inbox')
        if not s.get(AttentionInbox, key):
            s.add(AttentionInbox(key=key, manifest_path=path, manifest_sha256=digest, run_id=run_id))
        s.commit()
    return key


def backfill_step(engine, storage_root, limit=25):
    """Discover one PK page of historical runs; never scan articles or startup."""
    with Session(engine) as s:
        cursor = s.get(AttentionMeta, 'backfill_cursor')
        after = int(cursor.value) if cursor and cursor.value.isdigit() else 0
        generation = s.get(AttentionMeta, 'backfill_generation')
        epoch = generation.value if generation else ''
        rows = s.exec(select(RunRecord.id, RunRecord.run_key, RunRecord.trigger_type,
            RunRecord.status, RunRecord.stats_json).where(RunRecord.id > after)
            .order_by(RunRecord.id).limit(limit)).all()
    if not rows:
        return False
    failures = []
    for run_id, run_key, trigger, status, stats_json in rows:
        if trigger == 'profile_test' or status == 'running':
            continue
        try:
            stats = json.loads(stats_json or '{}')
            if not isinstance(stats,dict):raise ValueError('采集统计不是对象')
            report = stats.get('newsnow') or {}
            if not isinstance(report,dict):raise ValueError('NewsNow快照报告不是对象')
            if stats.get('provider') != 'newsnow' and not report:
                continue
            # Resumed tasks can retain an earlier run's frozen report while
            # publishing a new RunRecord. Its manifest remains authoritative.
            path = report.get('manifest_path') or str(Path(run_key) / 'newsnow-snapshots.json')
            enqueue_manifest(engine, storage_root, path, run_id)
        except (ValueError, OSError, TypeError) as exc:
            failures.append(dict(run_id=run_id, error=type(exc).__name__ + ': ' + str(exc)[:200]))
    with Session(engine) as s:
        lock_news(s, 'attention-backfill')
        generation = s.get(AttentionMeta, 'backfill_generation')
        if (generation.value if generation else '') != epoch:
            # A requested rescan must not be overwritten by an older page.
            s.rollback();return True
        cursor = s.get(AttentionMeta, 'backfill_cursor') or AttentionMeta(key='backfill_cursor')
        cursor.value = str(max(after, rows[-1][0])); s.add(cursor)
        if failures:
            old = s.get(AttentionMeta, 'backfill_error') or AttentionMeta(key='backfill_error')
            old.value = json.dumps(failures, ensure_ascii=False); s.add(old)
        s.commit()
    return True


def rescan(engine):
    with Session(engine) as s:
        lock_news(s, 'attention-backfill')
        for key in ('backfill_cursor', 'backfill_error'):
            row = s.get(AttentionMeta, key) or AttentionMeta(key=key)
            row.value = '0' if key == 'backfill_cursor' else ''; s.add(row)
        row=s.get(AttentionMeta,'backfill_generation') or AttentionMeta(key='backfill_generation')
        row.value=uuid4().hex;s.add(row)
        s.commit()


class AttentionWorker:
    def __init__(self, engine, storage_root):
        self.engine, self.storage_root = engine, str(storage_root)
        self.enabled = os.getenv('NEWS2DOUYIN_ATTENTION_ENABLED', '1').lower() not in ('0', 'false', 'off', 'no')
        self.stop_event, self.thread, self._step_lock = Event(), None, Lock()
        self.state = 'idle' if self.enabled else 'disabled'
        self.error = ''; self.last_processed = ''; self._backfill_at = 0.

    def start(self):
        if not self.enabled or self.thread and self.thread.is_alive(): return
        self.stop_event.clear()
        self.thread = Thread(target=self._loop, name='newsnow-attention', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread: self.thread.join(timeout=5)

    def _claim(self):
        now = time.time()
        with Session(self.engine) as s:
            lock_news(s, 'attention-inbox')
            row = s.exec(select(AttentionInbox).where(or_(
                (AttentionInbox.state.in_(['pending', 'error'])) & (AttentionInbox.next_attempt <= now),
                (AttentionInbox.state == 'running') & (AttentionInbox.lease_until < now)))
                .order_by(AttentionInbox.created, AttentionInbox.key).limit(1)).first()
            if not row: s.rollback(); return None
            row.state = 'running'; row.lease = uuid4().hex; row.lease_until = now + 120
            row.attempts += 1; s.add(row)
            result = dict(key=row.key, token=row.lease, path=row.manifest_path, sha=row.manifest_sha256, attempts=row.attempts)
            s.commit(); return result

    def _heartbeat(self, job, done):
        while not done.wait(30):
            try:
                with Session(self.engine) as s:
                    lock_news(s, 'attention-inbox')
                    row = s.get(AttentionInbox, job['key'])
                    if not row or row.lease != job['token'] or row.state != 'running': return
                    row.lease_until = time.time() + 120; s.add(row); s.commit()
            except Exception:
                log.warning('Attention lease renewal deferred', exc_info=True)

    def publish(self, job, prepared, metrics):
        """One SQL commit; monkeypatchable boundary for recovery verification."""
        from .grouping import apply_prepared
        with Session(self.engine) as s:
            lock_news(s, 'attention-inbox')
            row = s.get(AttentionInbox, job['key'])
            if not row or row.state != 'running' or row.lease != job['token'] or row.lease_until < time.time():
                raise RuntimeError('关注度任务租约已失效，等待安全重放')
            apply_prepared(s, prepared, metrics)
            row.state = 'done'; row.finished_at = utc_now_iso(); row.error = ''
            row.lease = ''; row.lease_until = 0; s.add(row); s.commit()

    def _process(self, job):
        from .lake import AttentionLake
        from .grouping import prepare_batch
        done = Event()
        heartbeat = Thread(target=self._heartbeat, args=(job, done), daemon=True)
        heartbeat.start()
        try:
            lake = AttentionLake(self.storage_root)
            batch = lake.ingest_manifest(job['path'], job['sha'])
            with Session(self.engine) as s:
                prepared = prepare_batch(s, batch)
            metrics={}
            topics=list(prepared['topics'].items())
            for start in range(0,len(topics),1000):
                metrics.update(lake.metrics_many({key:value['item_keys'] for key,value in topics[start:start+1000]},
                                                now=prepared['observed_at']))
            self.publish(job, prepared, metrics)
            self.error = ''; self.last_processed = utc_now_iso(); self.state = 'idle'
        except Exception as exc:
            self.error = type(exc).__name__ + ': ' + str(exc)[:400]; self.state = 'error'
            # SQL lock errors are logged only. Writing an error row while the
            # database is busy would amplify contention; the lease can expire.
            from sqlalchemy.exc import SQLAlchemyError
            if not isinstance(exc, SQLAlchemyError):
                with Session(self.engine) as s:
                    lock_news(s, 'attention-inbox')
                    row = s.get(AttentionInbox, job['key'])
                    if row and row.lease == job['token'] and row.state == 'running':
                        row.state = 'error'; row.error = self.error; row.lease = ''; row.lease_until = 0
                        row.next_attempt = time.time() + min(900, 5 * 2 ** min(job['attempts'], 7)); s.add(row); s.commit()
            log.warning('Attention publication deferred: %s', self.error)
        finally:
            done.set(); heartbeat.join(timeout=1)

    def step(self):
        """Do bounded work, including when called explicitly by tests/tools."""
        if not self.enabled or not self._step_lock.acquire(blocking=False): return False
        owner = ReceiverLock(self.storage_root)
        owner.path = Path(self.storage_root).resolve() / 'newsnow' / 'attention-worker.lock'
        try:
            try: owner.acquire()
            except ReceiverBusy: return False
            # Transient backfill/claim/matching failures recover on a healthy
            # tick. Persistent inbox failures are separately stored in SQL.
            self.error='';self.state='idle'
            changed = False
            if time.monotonic() >= self._backfill_at:
                changed = backfill_step(self.engine, self.storage_root)
                self._backfill_at = time.monotonic() + (0.2 if changed else 30)
            job = self._claim()
            if job:
                self.state = 'processing'; self._process(job); changed = True
            from .matching import step
            return step(self.engine) or changed
        finally:
            owner.release(); self._step_lock.release()

    def _loop(self):
        while not self.stop_event.is_set():
            try: changed = self.step()
            except Exception as exc:
                self.error = type(exc).__name__ + ': ' + str(exc)[:300]; self.state = 'error'
                log.warning('Attention worker deferred', exc_info=True); changed = False
            self.stop_event.wait(.2 if changed and not self.error else 3)
