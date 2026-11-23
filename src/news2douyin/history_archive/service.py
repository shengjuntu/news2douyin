"""Resumable backfill manager. HTTP only schedules work and reads its ledger."""
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import logging
from math import isfinite
from shutil import disk_usage
from threading import Event, Thread
from time import monotonic, time
from uuid import uuid4

from sqlalchemy import case, func, update
from sqlmodel import Session, select

from ..intake.runtime import ReceiverBusy, ReceiverLock
from ..intake.service import IntakeHTTPError
from ..storage.articles import lock_news
from . import catalog, convert
from .paths import archive_root
from .models import (HistoryControl, HistoryGroup, HistoryJob, HistoryPart,
                     HistoryProviderCooldown, HistoryPublication)

log = logging.getLogger(__name__)
LOCK = 'history-archive'
FIRST = date(1979, 1, 1)
GKG_FIRST = date(2013, 4, 1)
V2_FIRST = date(2015, 2, 18)
GIB = 1024 ** 3
ACTIVE_GROUP = ('pending', 'running')
MAIN = 'main'
PROVIDER = 'gdelt'
CATALOG_PROVIDER = 'gdelt-catalog'
RETRY_SECONDS = 60
PUBLISH_INTERVAL = 300
RATE_LIMIT_MESSAGE = 'GDELT 限流，整个历史下载队列将在等待结束后继续。'
CATALOG_RETRY_MESSAGE = 'GDELT 官方清单暂时无法下载，将自动重试。'
DOWNLOAD_RETRY_MESSAGE = 'GDELT 下载暂时中断，将自动重试。'


def _today():
    return datetime.now(timezone.utc).date()


def _kinds(mentions):
    return ('events', 'gkg', 'mentions') if mentions else ('events', 'gkg')


def _ranges(start, end, mentions=True):
    return (HistoryGroup.date_from <= end, HistoryGroup.date_to >= start,
            HistoryGroup.kind.in_(_kinds(mentions)))


def _day_range(first, last):
    for offset in range((last - first).days + 1):
        yield first + timedelta(days=offset)


def _group(version, kind, period, first, last, *, missing=0):
    return HistoryGroup(key=f'{version}/{kind}/{period}', kind=kind, version=version, period=period,
                        date_from=first, date_to=last, archive_missing=missing)


def _group_data(group):
    return dict(date_from=group.date_from.isoformat(), date_to=group.date_to.isoformat(),
                kind=group.kind, version=group.version, state=group.state, phase=group.phase,
                total_files=group.total_files, ready_files=group.ready_files, failed_files=group.failed_files,
                archive_missing=group.archive_missing, invalid_rows=group.invalid_rows, error=group.error,
                total_bytes=group.total_bytes, ready_bytes=group.ready_bytes, retry_at=group.retry_at)


def query_groups(engine, start, end):
    """Read only committed Parquet files. The requested dates are inclusive."""
    with Session(engine) as session:
        rows = session.exec(select(HistoryGroup, HistoryPublication.path)
                            .join(HistoryPublication, HistoryPublication.group_key == HistoryGroup.key)
                            .where(*_ranges(start, end))
                            .order_by(HistoryGroup.date_to.desc(), HistoryPublication.path)).all()
        return [{**_group_data(group), 'path': path} for group, path in rows]


def requests_for_case(session, case_key):
    rows = session.exec(select(HistoryJob).where(HistoryJob.case_key == case_key)).all()
    return {job.node_key: dict(state={'planning': 'queued', 'running': 'waiting'}.get(job.state, job.state),
                               date_from=job.date_from.isoformat(), date_to=job.date_to.isoformat(),
                               mentions=job.mentions, error=job.error) for job in rows}


class ArchiveWorker:
    def __init__(self, engine, storage_root):
        self.engine = engine
        self.root = archive_root(storage_root)
        self.stop_event = Event()
        self.wake_event = Event()
        self.pause_event = Event()
        self.thread = None
        self.status_error = ''
        self.transfer = None
        self.waiting_transfer = None

    @contextmanager
    def _write(self):
        with Session(self.engine) as session:
            lock_news(session, LOCK)
            yield session
            session.commit()

    def is_set(self):
        return self.stop_event.is_set() or self.pause_event.is_set()

    def _job(self, session, key, start, end, mentions, *, case_key='', node_key=''):
        if start < FIRST or end > _today() or end < start:
            raise ValueError('日期范围须在 1979-01-01 到今天之间。')
        previous = session.get(HistoryJob, key)
        if previous and previous.state in ('planning', 'running'):
            if (previous.date_from, previous.date_to, previous.mentions) == (start, end, mentions):
                return
            raise ValueError('已有进行中的任务，请等当前任务完成后再改范围。')
        session.merge(HistoryJob(key=key, date_from=start, date_to=end, mentions=mentions,
                                 case_key=case_key, node_key=node_key, serial=time()))

    def start(self, date_from, date_to, mentions=False):
        with self._write() as session:
            self._job(session, MAIN, date_from, date_to, mentions)
        self.wake_event.set()
        return self.status()

    def pause(self):
        with self._write() as session:
            session.merge(HistoryControl(paused=True))
        self.pause_event.set()
        return self.status()

    def resume(self):
        with self._write() as session:
            session.merge(HistoryControl(paused=False))
        self.pause_event.clear()
        self.wake_event.set()
        return self.status()

    def retry(self):
        with self._write() as session:
            self._retry_failed(session)
            session.exec(update(HistoryJob).where(HistoryJob.state == 'failed').values(state='planning', error=''))
            session.exec(update(HistoryGroup).where(HistoryGroup.state == 'pending', HistoryGroup.retry_at > 0)
                         .values(retry_at=0, error=''))
            catalog_wait = session.get(HistoryProviderCooldown, CATALOG_PROVIDER)
            if catalog_wait:
                session.delete(catalog_wait)
            session.exec(update(HistoryJob).where(HistoryJob.key == MAIN, HistoryJob.state == 'partial')
                         .values(state='planning', error=''))
        self.wake_event.set()
        return self.status()

    @staticmethod
    def _retry_failed(session, *conditions):
        pending = select(HistoryPart.key).where(HistoryPart.group_key == HistoryGroup.key,
                                               HistoryPart.state.in_(('pending', 'downloaded'))
                                               ).correlate(HistoryGroup).exists()
        # Older workers could finish a group after a retry had already reset a
        # failed part. Include those stranded parts even when the error count is zero.
        stranded = HistoryGroup.state.in_(('partial', 'failed')) & pending
        groups = select(HistoryGroup.key).where((HistoryGroup.failed_files > 0) | stranded, *conditions)
        session.exec(update(HistoryPart).where(HistoryPart.group_key.in_(groups), HistoryPart.state == 'failed')
                     .values(state='pending', error=''))
        running = HistoryGroup.state == 'running'
        session.exec(update(HistoryGroup).where(HistoryGroup.key.in_(groups)).values(
            state=case((running, 'running'), else_='pending'),
            phase=case((running, HistoryGroup.phase), else_=''),
            failed_files=0, error='', retry_at=0))

    def request_window(self, date_from, date_to, mentions=False, case_key='', node_key=''):
        if bool(case_key) != bool(node_key):
            raise ValueError('调查与节点必须同时指定。')
        if (date_to - date_from).days >= 366:
            raise ValueError('每次调查补档最多指定 366 天。')
        if case_key:
            from ..causal.models import CausalNode, CausalProgram, CausalProgramRetrieval
            from ..storage.utils import loads
            with Session(self.engine) as session:
                node = session.get(CausalNode, node_key)
                report = session.get(CausalProgramRetrieval, node_key)
                if not node or node.case_key != case_key or not session.get(CausalProgram, case_key):
                    raise KeyError('在该调查中找不到程序查找节点。')
                history = loads(report.report_json, {}).get('history', {}) if report else {}
                # The queued range comes from the node's checked search window.
                # The browser may narrow it but cannot extend it into the future.
                try:
                    floor, ceiling = (date.fromisoformat(history[field]) for field in ('date_from', 'date_to'))
                except (ValueError, KeyError):
                    raise ValueError('该节点尚无可补档的历史范围，请先运行查找。') from None
                if date_from < floor or date_to > ceiling:
                    raise ValueError('补档日期超出该节点实际查找范围。')
        key = 'node:' + node_key if node_key else 'window:' + uuid4().hex
        with self._write() as session:
            previous = session.get(HistoryJob, key)
            if previous and previous.state in ('failed', 'partial'):
                self._retry_failed(session, *_ranges(date_from, date_to, mentions))
            self._job(session, key, date_from, date_to, mentions, case_key=case_key, node_key=node_key)
        self.wake_event.set()
        return {'state': 'queued', 'date_from': date_from.isoformat(), 'date_to': date_to.isoformat()}

    def _prepare(self, job):
        track = self._track_transfer(phase='catalog')
        try:
            packages = catalog.list_packages(job.date_from, job.date_to, _kinds(job.mentions),
                                             cache_dir=self.root / 'catalog', stop=self, progress=track)
        except (catalog.ArchiveTransferError, IntakeHTTPError):
            self.waiting_transfer = self.transfer
            raise
        finally:
            self.transfer = None
        if self.is_set():
            raise InterruptedError()
        by_group = defaultdict(list)
        specs = {}
        for package in packages:
            period = package.stamp[:8]
            group = _group(package.version, package.kind, period, package.date_from, package.date_to)
            by_group[group.key].append(package)
            specs[group.key] = group
        # The v2 catalogue can omit hours or an entire day. Record that separately
        # from download failures and from days when a dataset did not exist.
        for day in _day_range(max(job.date_from, V2_FIRST + timedelta(days=1)), min(job.date_to, _today())):
            for kind in _kinds(job.mentions):
                group = _group(2, kind, day.strftime('%Y%m%d'), day, day)
                specs.setdefault(group.key, group)
        for day in _day_range(max(job.date_from, GKG_FIRST), min(job.date_to, V2_FIRST)):
            for kind in _kinds(False):
                group = _group(1, kind, day.strftime('%Y%m%d'), day, day)
                specs.setdefault(group.key, group)
        priority = int(job.key != MAIN)
        # Keep transactions and connection ownership short during long-range planning.
        keys = sorted(specs, key=lambda key: specs[key].date_to, reverse=True)
        for offset in range(0, len(keys), 50):
            if self.is_set():
                raise InterruptedError()
            segment = keys[offset:offset + 50]
            with self._write() as session:
                groups = {group.key: group for group in session.exec(
                    select(HistoryGroup).where(HistoryGroup.key.in_(segment)))}
                known = set(session.exec(select(HistoryPart.key).where(HistoryPart.group_key.in_(segment))))
                for key in segment:
                    group = groups.get(key, specs[key])
                    files = by_group[key]
                    fresh = [item for item in files if item.name not in known]
                    if fresh:
                        session.add_all(HistoryPart(key=item.name, group_key=key, stamp=item.stamp,
                                                    url=item.url, size=item.size, md5=item.md5) for item in fresh)
                        group.total_files += len(fresh)
                        group.total_bytes += sum(item.size for item in fresh)
                        group.state, group.retry_at = 'pending', 0
                    group.archive_missing = (
                        max(0, 96 - group.total_files) if group.version == 2
                        and V2_FIRST < group.date_to < _today() else
                        int(group.version == 1 and not group.total_files))
                    if not group.total_files:
                        group.state = 'missing'
                    elif group.ready_files == group.total_files:
                        self._finish(group)
                    group.priority = max(group.priority, priority)
                    session.add(group)
        with self._write() as session:
            current = session.get(HistoryJob, job.key)
            if current and current.serial == job.serial:
                current.state = 'running'
                session.add(current)

    def _staged(self, part):
        return self.root / 'staging' / part.key

    def _record_part(self, key, state, error=''):
        with self._write() as session:
            part = session.get(HistoryPart, key)
            group = session.get(HistoryGroup, part.group_key)
            if part.state == 'downloaded' and state != 'downloaded':
                group.downloaded_files -= 1
                group.downloaded_bytes -= part.size
            if part.state == 'failed':
                group.failed_files -= 1
            if state == 'downloaded' and part.state != 'downloaded':
                group.downloaded_files += 1
                group.downloaded_bytes += part.size
            if state == 'failed':
                group.failed_files += 1
            part.state, part.error = state, error
            session.add(part)
            session.add(group)

    def _track_transfer(self, *, phase, group_key='', stamp=''):
        origin, previous_name = 0, ''
        started = monotonic()

        def progress(filename, received, total):
            nonlocal started, origin, previous_name
            now = monotonic()
            if previous_name != filename or self.transfer and received < self.transfer['received_bytes']:
                started, origin, previous_name = now, received, filename
            elapsed = now - started
            self.transfer = dict(group_key=group_key, filename=filename, stamp=stamp,
                                 phase=phase, updated_at=time(), received_bytes=received,
                                 expected_bytes=total,
                                 speed_bps=round((received - origin) / elapsed) if elapsed >= 1 else None)

        return progress

    def _download(self, group, part):
        package = catalog.ArchivePackage(name=part.key, kind=group.kind, version=group.version,
            date_from=group.date_from, date_to=group.date_to, stamp=part.stamp,
            url=part.url, size=part.size, md5=part.md5)
        track = self._track_transfer(phase='downloading', group_key=group.key, stamp=part.stamp)
        self.transfer = dict(group_key=group.key, filename=part.key, stamp=part.stamp,
                             phase='downloading', updated_at=time(), received_bytes=0,
                             expected_bytes=part.size, speed_bps=None)
        try:
            catalog.download_package(package, self._staged(part), stop=self,
                                     progress=lambda received, total: track(part.key, received, total))
        except (catalog.ArchiveTransferError, IntakeHTTPError):
            self.waiting_transfer = self.transfer
            raise
        finally:
            self.transfer = None
        self._record_part(part.key, 'downloaded')

    def _process(self, group):
        from ..intake.lake import hashes

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            if disk_usage(self.root).free < max(2 * GIB, (group.total_bytes - group.ready_bytes) * 7):
                raise OSError('space')
        except OSError:
            self.pause()
            raise InterruptedError('可用空间不足或归档目录不可写。') from None
        with Session(self.engine) as session:
            parts = list(session.exec(select(HistoryPart).where(
                HistoryPart.group_key == group.key, HistoryPart.state != 'ready').order_by(HistoryPart.stamp.desc())))
        sources, selected = [], []
        retry_at, message = 0, ''
        # A slowly filling day may already have useful verified files. Cap the
        # time they wait for publication without interrupting an active ZIP.
        publish_deadline = monotonic() + PUBLISH_INTERVAL
        for part in parts:
            if self.is_set():
                raise InterruptedError()
            if part.state == 'failed':
                continue
            path = self._staged(part)
            # Reuse a verified ZIP left in staging after restart or completed
            # downloading before the ledger committed.
            present = hashes(path) if path.is_file() else None
            if not present or present[1] != part.md5 or present[2] != part.size:
                if part.state == 'downloaded':
                    self._record_part(part.key, 'pending')
                try:
                    self._download(group, part)
                    present = hashes(path)
                except InterruptedError:
                    raise
                except IntakeHTTPError as exc:
                    if exc.status == 429:
                        retry_at = self._backoff(PROVIDER, exc.retry_after, minimum=30)
                        message = RATE_LIMIT_MESSAGE
                        break
                    if exc.status == 408 or exc.status >= 500:
                        retry_at = self._retry_at(exc.retry_after)
                        message = DOWNLOAD_RETRY_MESSAGE
                        break
                    self._record_part(part.key, 'failed', 'HTTP ' + str(exc.status))
                    continue
                except catalog.ArchiveTransferError:
                    retry_at, message = self._retry_at(), DOWNLOAD_RETRY_MESSAGE
                    break
                except OSError:
                    self.pause()
                    raise InterruptedError('归档暂存目录无法写入。') from None
                except Exception as exc:
                    self._record_part(part.key, 'failed', '下载或校验失败 · ' + type(exc).__name__)
                    continue
            elif part.state != 'downloaded':
                self._record_part(part.key, 'downloaded')
            sources.append(convert.SourceZip(path, part.key, part.stamp, present[0]))
            selected.append(part.key)
            if monotonic() >= publish_deadline:
                break
        published = None
        if sources:
            with self._write() as session:
                current = session.get(HistoryGroup, group.key)
                current.phase, current.error = 'converting', ''
                session.add(current)
            published = convert.convert_group(self.root, group.kind, group.version, group.period,
                                               sources, stop=self.is_set)
        # Durable files are visible to readers only after this transaction.
        with self._write() as session:
            current = session.get(HistoryGroup, group.key)
            if published:
                relative = published.parquet.relative_to(self.root).as_posix()
                if not session.get(HistoryPublication, relative):
                    session.add(HistoryPublication(path=relative, group_key=group.key))
                ready = list(session.exec(select(HistoryPart).where(HistoryPart.key.in_(selected))))
                for part in ready:
                    part.state = 'ready'
                    part.publication_path = relative
                    session.add(part)
                current.ready_files += len(ready)
                current.ready_bytes += sum(part.size for part in ready)
                current.invalid_rows += published.invalid_rows
            self._finish(current, pending=self._has_pending(session, group.key))
            if current.state == 'pending' and retry_at:
                current.retry_at, current.error = retry_at, message
            session.add(current)
        for source in sources:
            # Only paths owned by this module are eligible for cleanup.
            try:
                source.path.unlink(missing_ok=True)
            except OSError:
                log.warning('Could not clear a committed historical staging ZIP')
        # Let the worker's poll interval space requests during a hard network
        # outage. The offending group also has its own longer retry deadline.
        return message != DOWNLOAD_RETRY_MESSAGE

    @staticmethod
    def _finish(group, *, pending=False):
        if pending:
            group.state = 'pending'
        elif group.ready_files == group.total_files and not group.archive_missing:
            group.state = 'ready'
        elif group.ready_files:
            group.state = 'partial'
        else:
            group.state = 'failed'
        group.phase, group.error, group.retry_at = '', '', 0

    @staticmethod
    def _has_pending(session, key):
        return session.exec(select(HistoryPart.key).where(HistoryPart.group_key == key,
            HistoryPart.state.in_(('pending', 'downloaded'))).limit(1)).first() is not None

    def _defer(self, key, seconds, message):
        with self._write() as session:
            group = session.get(HistoryGroup, key)
            group.state, group.retry_at = 'pending', time() + max(0, seconds)
            group.phase, group.error = '', message
            session.add(group)

    @staticmethod
    def _retry_at(seconds=0, *, minimum=RETRY_SECONDS):
        now = time()
        return now + max(minimum, seconds if isfinite(seconds) else 0)

    def _backoff(self, scope, seconds=0, *, minimum=RETRY_SECONDS):
        resume_at = self._retry_at(seconds, minimum=minimum)
        with self._write() as session:
            active = session.get(HistoryProviderCooldown, scope)
            if active:
                active.resume_at = max(active.resume_at, resume_at)
                resume_at = active.resume_at
                session.add(active)
            else:
                session.add(HistoryProviderCooldown(provider=scope, resume_at=resume_at))
        return resume_at

    def _finalize(self):
        from ..causal import service as causal
        from ..causal.models import CausalJob

        with Session(self.engine) as session:
            jobs = [(job.key, job.serial) for job in session.exec(
                select(HistoryJob).where(HistoryJob.state == 'running').order_by(HistoryJob.serial))]
        for key, serial in jobs:
            with self._write() as session:
                job = session.get(HistoryJob, key)
                if not job or job.serial != serial or job.state != 'running':
                    continue
                conditions = _ranges(job.date_from, job.date_to, job.mentions)
                if session.exec(select(HistoryGroup.key).where(
                        *conditions, HistoryGroup.state.in_(ACTIVE_GROUP)).limit(1)).first():
                    continue
                groups = list(session.exec(select(HistoryGroup.state, HistoryGroup.ready_files).where(*conditions)))
                has_data = any(count for _, count in groups)
                all_ready = bool(groups) and all(state == 'ready' for state, _ in groups)
                if job.case_key and has_data:
                    if session.exec(select(CausalJob.node_key).where(
                        CausalJob.case_key == job.case_key, CausalJob.state.in_(causal.ACTIVE)).limit(1)).first():
                        continue
                    try:
                        causal.expand(session, job.case_key, job.node_key, False, commit=False)
                    except causal.CausalConflict:
                        # An unrelated request may just have started a case job.
                        if session.exec(select(CausalJob.node_key).where(
                            CausalJob.case_key == job.case_key, CausalJob.state.in_(causal.ACTIVE)).limit(1)).first():
                            continue
                        job.state, job.error = 'partial', '归档可查，原节点暂时无法自动重查。'
                        session.add(job)
                        continue
                    except KeyError:
                        job.state, job.error = 'failed', '原调查节点不存在。'
                        session.add(job)
                        continue
                job.state = 'ready' if all_ready else 'partial' if has_data else 'failed'
                if not has_data:
                    job.error = '此时间窗没有可用归档。'
                session.add(job)

    def step(self):
        self._finalize()
        if self.is_set():
            return False
        with Session(self.engine) as session:
            cooldown = session.get(HistoryProviderCooldown, PROVIDER)
            now = time()
            if cooldown and cooldown.resume_at > now:
                return False
            catalog_wait = session.get(HistoryProviderCooldown, CATALOG_PROVIDER)
            planned = None
            if not catalog_wait or catalog_wait.resume_at <= now:
                planned = session.exec(select(HistoryJob).where(HistoryJob.state == 'planning')
                                       .order_by((HistoryJob.key == MAIN), HistoryJob.serial).limit(1)).first()
        if planned:
            try:
                self._prepare(planned)
            except InterruptedError:
                return False
            except IntakeHTTPError as exc:
                if exc.status == 429:
                    self._backoff(PROVIDER, exc.retry_after, minimum=30)
                    return False
                if exc.status == 408 or exc.status >= 500:
                    self._backoff(CATALOG_PROVIDER, exc.retry_after)
                    return False
                self._plan_failed(planned, exc)
            except catalog.ArchiveTransferError:
                self._backoff(CATALOG_PROVIDER)
                return False
            except Exception as exc:
                self._plan_failed(planned, exc)
            return True
        due = (HistoryGroup.state == 'pending', HistoryGroup.retry_at <= time())
        with Session(self.engine) as session:
            if not session.exec(select(HistoryGroup.key).where(*due).limit(1)).first():
                return False
        with self._write() as session:
            group = session.exec(select(HistoryGroup).where(*due).order_by(
                    HistoryGroup.priority.desc(), HistoryGroup.date_to.desc(), HistoryGroup.key).limit(1)).first()
            if not group:
                return False
            group.state, group.phase, group.error = 'running', 'downloading', ''
            session.add(group)
            group_key = group.key
            session.flush()
            session.expunge(group)
        try:
            return self._process(group)
        except InterruptedError as exc:
            self._defer(group_key, 0, str(exc)[:160])
            return False
        except Exception as exc:
            log.warning('Historical conversion failed (%s)', type(exc).__name__)
            with self._write() as session:
                current = session.get(HistoryGroup, group_key)
                parts = list(session.exec(select(HistoryPart).where(HistoryPart.group_key == group_key,
                                                                    HistoryPart.state == 'downloaded')))
                current.state, current.phase, current.error = 'failed', '', '转换或落盘失败，可重试。'
                for part in parts:
                    part.error = current.error
                current.failed_files += len(parts)
                session.add_all(parts)
                session.add(current)
        return True

    def _plan_failed(self, planned, exc):
        log.warning('Historical catalogue planning failed (%s)', type(exc).__name__)
        with self._write() as session:
            job = session.get(HistoryJob, planned.key)
            if job and job.serial == planned.serial:
                job.state, job.error = 'failed', '无法读取官方归档清单，可重试。'
                session.add(job)

    def launch(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = Thread(target=self._loop, name='historical-archive', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def _loop(self):
        ownership = ReceiverLock(self.root.parent)
        ownership.path = self.root / 'worker.lock'
        try:
            ownership.acquire()
            self.status_error = ''
            with self._write() as session:
                control = session.get(HistoryControl, 'archive')
                if control and control.paused:
                    self.pause_event.set()
                session.exec(update(HistoryGroup).where(HistoryGroup.state == 'running')
                             .values(state='pending', phase='', retry_at=0))
            self._clean_committed()
            convert.discard_staging(self.root)
            while not self.stop_event.is_set():
                self.wake_event.clear()
                try:
                    worked = self.step()
                except Exception as exc:
                    log.warning('Historical archive worker waiting to retry (%s)', type(exc).__name__)
                    worked = False
                if not worked and not self.stop_event.is_set():
                    self.wake_event.wait(4)
        except (ReceiverBusy, OSError):
            self.status_error = '归档目录不可用，或已由另一个运行实例管理。'
        finally:
            ownership.release()

    def _clean_committed(self):
        for path in (self.root / 'staging').glob('*.zip'):
            with Session(self.engine) as session:
                part = session.get(HistoryPart, path.name)
                if (not part or part.state != 'ready' or not part.publication_path
                        or not (self.root / part.publication_path).is_file()):
                    continue
            try:
                path.unlink()
            except OSError:
                log.warning('Could not clear a committed historical staging ZIP')

    @staticmethod
    def _through(session, main):
        if not main:
            return None
        # Newest contiguous coverage is an Events/GKG frontier. Mentions can
        # be deliberately deferred, so display their coverage independently.
        interval_groups = session.exec(select(HistoryGroup.date_from, HistoryGroup.date_to, HistoryGroup.kind)
            .where(*_ranges(main.date_from, main.date_to, False), HistoryGroup.state == 'ready')).all()
        ready = defaultdict(set)
        for first, last, kind in interval_groups:
            ready[kind].update(_day_range(max(first, main.date_from), min(last, main.date_to)))
        last = None
        day = main.date_to
        while day >= main.date_from:
            if day not in ready['events'] or day >= GKG_FIRST and day not in ready['gkg']:
                break
            last = day
            day -= timedelta(days=1)
        return last.isoformat() if last else None

    def status(self, *, coverage_limit=30, coverage_offset=0):
        with Session(self.engine) as session:
            main = session.get(HistoryJob, MAIN)
            control = session.get(HistoryControl, 'archive')
            now = time()
            retry_at, retry_scope, message, delayed_group = self._retry_status(session, now)
            groups = select(HistoryGroup)
            if main:
                groups = groups.where(*_ranges(main.date_from, main.date_to, main.mentions))
            summary = groups.subquery().c
            fields = ('total_files', 'downloaded_files', 'ready_files', 'total_bytes', 'downloaded_bytes',
                      'ready_bytes', 'failed_files')
            totals = session.exec(select(*(func.coalesce(func.sum(summary[field]), 0) for field in fields))).one()
            progress = dict(zip(fields, map(int, totals)))
            progress['errors'] = progress.pop('failed_files')
            coverage_total = session.exec(select(func.count()).select_from(groups.subquery())).one()
            coverage = list(session.exec(groups.order_by(HistoryGroup.date_to.desc(), HistoryGroup.key)
                                         .offset(coverage_offset).limit(coverage_limit)))
            current = session.exec(select(HistoryGroup).where(HistoryGroup.state == 'running').limit(1)).first()
            transfer = self.transfer
            active = _group_data(current) if current else {}
            if transfer and (transfer['phase'] == 'catalog' or current and transfer['group_key'] == current.key):
                active.update(transfer)
                if now - transfer['updated_at'] > 15:
                    active['speed_bps'] = 0
            waiting = self.waiting_transfer
            if not active and waiting and (retry_scope == 'provider'
                    or retry_scope == 'catalog' and waiting['phase'] == 'catalog'
                    or delayed_group and waiting['group_key'] == delayed_group.key):
                active = _group_data(delayed_group) if delayed_group else {}
                active.update(waiting, phase='retry_wait', speed_bps=None)
            failures = list(session.exec(select(HistoryGroup).where(
                (HistoryGroup.failed_files > 0) | (HistoryGroup.error != '') | (HistoryGroup.state == 'failed'))
                .order_by(HistoryGroup.date_to.desc()).limit(12)))
            requests = session.exec(select(HistoryJob).where(HistoryJob.key != MAIN)
                                    .order_by(HistoryJob.serial.desc()).limit(10)).all()
            planning = session.exec(select(HistoryJob.key).where(HistoryJob.state == 'planning').limit(1)).first()
            running = session.exec(select(HistoryJob.key).where(HistoryJob.state == 'running').limit(1)).first()
            state = ('paused' if control and control.paused else 'planning' if planning else
                     'running' if running or current else main.state if main else 'idle')
            parent = next(path for path in (self.root, *self.root.parents) if path.is_dir())
            disk = disk_usage(parent)
            return dict(state=state, date_from=main.date_from.isoformat() if main else '',
                        date_to=main.date_to.isoformat() if main else '', mentions=main.mentions if main else False,
                        progress=progress, disk=dict(free_bytes=disk.free, total_bytes=disk.total),
                        current=active or None, through=self._through(session, main),
                        coverage_total=coverage_total, coverage=[_group_data(item) for item in coverage],
                        failures=[_group_data(item) for item in failures],
                        requests=[dict(state=item.state, date_from=item.date_from.isoformat(),
                                       date_to=item.date_to.isoformat(), mentions=item.mentions,
                                       error=item.error) for item in requests],
                        retry_at=retry_at, retry_scope=retry_scope, message=message,
                        error=self.status_error or (message if retry_scope == 'provider' else '')
                        or (main.error if main else ''))

    @staticmethod
    def _retry_status(session, now):
        cooldown = session.get(HistoryProviderCooldown, PROVIDER)
        if cooldown and cooldown.resume_at > now:
            return cooldown.resume_at, 'provider', RATE_LIMIT_MESSAGE, None
        catalog_wait = session.get(HistoryProviderCooldown, CATALOG_PROVIDER)
        retry_group = session.exec(select(HistoryGroup).where(
            HistoryGroup.state == 'pending', HistoryGroup.retry_at > now)
            .order_by(HistoryGroup.retry_at).limit(1)).first()
        if (catalog_wait and catalog_wait.resume_at > now
                and (not retry_group or catalog_wait.resume_at <= retry_group.retry_at)):
            return catalog_wait.resume_at, 'catalog', CATALOG_RETRY_MESSAGE, None
        if retry_group:
            return retry_group.retry_at, 'group', retry_group.error, retry_group
        return None, '', '', None
