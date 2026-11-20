"""Bounded background projection from raw GKG into daily SQL summaries."""
from collections import Counter, deque
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from threading import Event, Thread
import time

import duckdb
from sqlmodel import Session

from ..digest.calendar import BEIJING, day_bounds_utc, days_before
from ..intake.lake import Lake
from ..intake.runtime import ReceiverBusy, ReceiverLock
from ..storage.articles import lock_news
from ..storage.utils import dumps
from .config import enabled, replay_days, settings
from .derived import INSERT_DOCS, SCHEMA, summarize, transaction
from .models import GeoriskCoverage, GeoriskDay
from .service import expected_through, stamp
from .source import candidates, manifests, pages

MAX_BATCHES_PER_TICK = 4
MANIFEST_SECONDS = 30
log = logging.getLogger(__name__)


class _Stopped(Exception):
    pass


class GeoriskWorker:
    def __init__(self, engine, storage_root):
        self.engine = engine
        self.lake = Lake(storage_root)
        self.root = Path(storage_root).resolve() / 'georisk'
        self.storage_root = storage_root
        self.stop_event = Event()
        self.thread = None
        self.last_error = ''
        self.status_error = ''
        self._config = None
        self._refresh_at = 0.
        self._archived = Counter()
        self._pending = deque()
        self._retained_from = ''

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    @property
    def enabled(self):
        return enabled()

    def start(self):
        if self.running:
            return True
        if not self.enabled:
            return False
        self.stop_event.clear()
        self.thread = Thread(target=self.loop, name='gdelt-georisk', daemon=True)
        self.thread.start()
        return True

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def loop(self):
        owner = ReceiverLock(self.storage_root)
        owner.path = self.root / 'worker.lock'
        try:
            owner.acquire()
        except (ReceiverBusy, OSError):
            self.status_error = '地缘风险分析工作目录不可用或已被占用'
            return
        self.status_error = ''
        try:
            while not self.stop_event.is_set():
                try:
                    changed = self.step()
                except _Stopped:
                    return
                except Exception as exc:
                    log.exception('georisk background projection failed')
                    self.last_error = type(exc).__name__
                    changed = False
                else:
                    self.last_error = ''
                self.stop_event.wait(2 if changed else MANIFEST_SECONDS)
        finally:
            owner.release()

    def _refresh(self, conn, cfg, now, first):
        first_day = first.isoformat()
        start, _ = day_bounds_utc(first)
        batches = manifests(self.lake, cfg, stamp(start), stamp(expected_through(now)))
        retention_changed = first_day != self._retained_from
        if retention_changed:
            # Dirty days have committed local work awaiting SQL publication,
            # including work interrupted just as the replay window advanced.
            expired = ' WHERE day<? AND day NOT IN (SELECT day FROM risk_dirty)'
            with transaction(conn):
                conn.execute('DELETE FROM risk_doc' + expired, [first_day])
                conn.execute('DELETE FROM risk_batch' + expired, [first_day])
        complete = {(feed, batch_stamp): sha for feed, batch_stamp, sha in conn.execute(
            'SELECT feed,stamp,sha FROM risk_batch').fetchall()}
        if any(complete.get((batch.feed, batch.stamp), batch.sha) != batch.sha for batch in batches):
            raise ValueError('已归档的 GKG 批次摘要已改变，请检查原始数据完整性')
        # Include confirmed zero-count dates. Reads can then distinguish a gap
        # in this scan from a date for which no scan has been persisted.
        last = now.astimezone(BEIJING).date()
        archived = Counter({(first + timedelta(days=offset)).isoformat(): 0
                            for offset in range((last - first).days + 1)})
        archived.update(batch.day for batch in batches)
        if retention_changed or archived != self._archived:
            with Session(self.engine) as session:
                lock_news(session, 'georisk-publish')
                session.merge(GeoriskCoverage(config_key=cfg.key, archived_by_day_json=dumps(archived)))
                session.commit()
        self._archived = archived
        self._pending = deque(
            batch for batch in batches if (batch.feed, batch.stamp) not in complete)
        self._retained_from = first_day
        self._refresh_at = time.monotonic() + MANIFEST_SECONDS

    def _process(self, conn, cfg, batch):
        day = batch.day
        rejected = 0
        with transaction(conn):
            for rows in pages(self.lake, batch, cfg):
                if self.stop_event.is_set():
                    raise _Stopped()
                documents, errors = candidates(batch, rows, cfg)
                rejected += errors
                if documents:
                    conn.execute(INSERT_DOCS, [day, documents])
            conn.execute('INSERT INTO risk_batch VALUES (?,?,?,?,?)',
                         [batch.feed, batch.stamp, batch.sha, day, rejected])
            conn.execute('INSERT INTO risk_dirty VALUES (?) ON CONFLICT DO NOTHING', [day])

    def _publish(self, conn, cfg):
        days = [day for (day,) in conn.execute(
            'SELECT day FROM risk_dirty ORDER BY day DESC LIMIT ?', [MAX_BATCHES_PER_TICK]
        ).fetchall()]
        if not days:
            return False
        results = [(day, *summarize(conn, day)) for day in days]
        with Session(self.engine) as session:
            lock_news(session, 'georisk-publish')
            for day, available, rejected, groups, evidence in results:
                # A longer replay can rebuild details for an already published day.
                published = session.get(GeoriskDay, (cfg.key, day))
                if published is not None and published.available_batches > available:
                    continue
                session.merge(GeoriskDay(config_key=cfg.key, day=day, available_batches=available,
                    archived_batches=max(available, self._archived[day]), rejected_rows=rejected,
                    groups_json=dumps(groups), evidence_json=dumps(evidence)))
            session.commit()
        conn.execute('DELETE FROM risk_dirty WHERE day=ANY(?)', [days])
        return True

    def step(self, now=None, *, refresh=False):
        if not self.lake.path.exists():
            return False
        cfg = settings()
        now = now or datetime.now(timezone.utc)
        first = days_before(now.astimezone(BEIJING).date(), replay_days() - 1)
        if self._config != cfg.key:
            self._config = cfg.key
            self._retained_from = ''
            self._refresh_at = 0.
        self.root.mkdir(parents=True, exist_ok=True)
        with duckdb.connect(str(self.root / f'daily-{cfg.key}.duckdb')) as conn:
            conn.execute(SCHEMA)
            if refresh or not self._refresh_at or time.monotonic() >= self._refresh_at:
                self._refresh(conn, cfg, now, first)
            for _ in range(min(len(self._pending), MAX_BATCHES_PER_TICK)):
                if self.stop_event.is_set():
                    raise _Stopped()
                self._process(conn, cfg, self._pending[0])
                self._pending.popleft()
            return self._publish(conn, cfg)
