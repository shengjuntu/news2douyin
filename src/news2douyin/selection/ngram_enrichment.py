"""Targeted evidence from GDELT minute files, distinct from original-source previews.

Discovery runs throughout the observation day independently of publishing a digest.
GETs use the materialized working records only. Network and reconstruction run after
the claim transaction has closed. Finished observations retain the UTC completion
time and the immutable upstream archive reference used to reconstruct the excerpt.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from importlib import metadata
from importlib.util import find_spec
import json
import logging
import os
import re
import time
from threading import Event, Thread
from uuid import uuid4

from sqlalchemy import case, func, or_
from sqlmodel import Session, select

from ..digest.calendar import BEIJING
from ..digest.focus_hints import read_focus_hints
from ..intake.official_models import OfficialEvent, OfficialMention
from ..storage.articles import lock_news
from .ngram_actions import REASON_LABELS, action_fields_reason, diagnose_excerpt, match_excerpt
from .ngram_models import NgramBudget, NgramTarget
from .service import CAMEO_LABELS
from .source_content import resolve_previews, source_url

log = logging.getLogger(__name__)
MAX_ACTIVE = 200
MAX_GROUP = 24
READY_STATES = ('ready', 'document_only')
QUEUE_STATES = ('pending', 'running', 'failed')

def _setting(name, default, minimum, maximum):
    try:
        return max(minimum, min(maximum, int(os.environ.get(name, default))))
    except ValueError:
        return default


@lru_cache(maxsize=1)
def dependency_available():
    # Package installation changes are picked up on service restart. Avoid
    # importing the data processing libraries into every read-only web request.
    try:
        return find_spec('gdeltnews') is not None and metadata.version('gdeltnews') == '1.1.0'
    except (metadata.PackageNotFoundError, ValueError, ImportError):
        return False


def _disabled_reason():
    flag = os.getenv('NEWS2DOUYIN_NGRAMS_ENABLED', 'auto').strip().lower()
    if flag not in ('', 'auto', '1', 'true', 'on', 'yes'):
        return '当前配置已关闭 NGrams 补证。'
    if not dependency_available():
        return '缺少兼容的 gdeltnews 依赖；安装 ngrams 可选依赖并重启服务后可补证。'
    return ''


def enabled():
    return not _disabled_reason()


def _allow_download():
    return os.getenv('NEWS2DOUYIN_NGRAMS_ALLOW_DOWNLOAD', '1').strip().lower() not in (
        '0', 'false', 'no', 'off')


def _public_budget(session, moment):
    day = moment.date().isoformat()
    row = session.get(NgramBudget, day)
    # Never count historical evidence. The queue limit caps the counted index entries.
    waiting = select(NgramTarget.key).where(NgramTarget.state.in_(QUEUE_STATES)).limit(MAX_ACTIVE).subquery()
    queued = session.exec(select(func.count()).select_from(waiting)).one()
    return dict(utc_day=day, automatic_used=row.automatic if row else 0,
                automatic_limit=_setting('NEWS2DOUYIN_NGRAMS_AUTO_TARGETS_PER_DAY', 64, 0, 2000),
                scans_used=row.scans if row else 0, scans_limit=_setting('NEWS2DOUYIN_NGRAMS_BATCHES_PER_DAY', 96, 1, 1440),
                queued_active=queued, queued_limit=MAX_ACTIVE, cache_only=not _allow_download(),
                dependency_available=dependency_available())


def _utc():
    return datetime.now(timezone.utc)


def _iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def _receipt_minute(stamp):
    try:
        if len(stamp) == 14 and stamp.isascii() and stamp.isdigit():
            return datetime.strptime(stamp, '%Y%m%d%H%M%S').replace(second=0, tzinfo=timezone.utc)
    except ValueError:
        pass
    return None


def _budget(session, moment):
    day = moment.date().isoformat()
    row = session.get(NgramBudget, day)
    if row is None:
        row = NgramBudget(day=day)
        session.add(row)
    return row


def _enqueue(session, pairs, now, number, *, manual=False, retry=False, originals=None, focus_ids=()):
    """Caller owns the queue lock. One receipt yields one pinned search window."""
    if not pairs or number <= 0:
        return 0, 0
    known = {row.key: row for row in session.exec(select(NgramTarget).where(
        NgramTarget.key.in_([mention.key for mention, _ in pairs])))}
    if originals is None:
        originals = resolve_previews(session, [mention.doc_identifier for mention, _ in pairs])
    active = session.exec(select(NgramTarget.key).where(NgramTarget.state.in_(QUEUE_STATES)).limit(MAX_ACTIVE)).all()
    capacity = min(max(0, MAX_ACTIVE - len(active)), number)
    minutes = _setting('NEWS2DOUYIN_NGRAMS_MINUTES', 15, 1, 15)
    queued = existing = 0
    for mention, event in pairs:
        url = source_url(mention.doc_identifier)
        stamp = _receipt_minute(mention.first_stamp)
        if not url or not stamp or stamp > now:
            continue
        if originals[url]['has_excerpt']:
            existing += 1
            continue
        row = known.get(mention.key)
        priority = 3 if manual else 2 if event.event_id in focus_ids else 1
        if row is not None:
            if row.state in QUEUE_STATES:
                row.priority = max(priority, row.priority)
            if not retry or row.state != 'unavailable':
                existing += 1
                continue
        if queued >= capacity:
            break
        if row is None:
            row = NgramTarget(key=mention.key, event_id=event.event_id, url=url, actor1=event.actor1_name,
                              actor2=event.actor2_name, root_code=event.event_root_code,
                              reference_feed=mention.first_feed, reference_stamp=mention.first_stamp,
                              reference_row=mention.first_row_index, requested_at=_iso(now),
                              minute='', minutes_left=minutes)
        row.minute = stamp.strftime('%Y%m%d%H%M00')
        row.minutes_left = minutes
        row.state = 'pending'
        row.note = ''
        row.retry_at = 0
        row.failures = 0
        row.priority = priority
        session.add(row)
        queued += 1
    return queued, existing


def enqueue_event(engine, event_id, retry=False):
    if not isinstance(event_id, str) or not event_id.isascii() or not event_id.isdigit() or len(event_id) > 30:
        raise ValueError('官方事件编号无效')
    with Session(engine) as s:
        event = s.get(OfficialEvent, event_id)
        if event is None:
            raise KeyError(event_id)
        now = _utc()
        disabled = _disabled_reason()
        if disabled:
            return dict(state='disabled', queued=0, existing=0, note=disabled, budget=_public_budget(s, now))
        lock_news(s, 'ngram-queue')
        mentions = s.exec(select(OfficialMention).where(OfficialMention.event_id == event_id)
                          .order_by(OfficialMention.last_seen.desc()).limit(4)).all()
        queued, existing = _enqueue(s, [(m, event) for m in mentions], now, 4, manual=True, retry=retry)
        s.commit()
        budget = _public_budget(s, now)
    if queued and budget['scans_used'] >= budget['scans_limit']:
        return dict(state='queued', queued=queued, existing=existing, budget=budget, note='已排队，UTC 当日分钟预算已用尽，将在预算恢复后处理。')
    if queued:
        return dict(state='queued', queued=queued, existing=existing, budget=budget, note='已排队，后台按报道地址及关联批次查找片段。')
    if existing:
        return dict(state='existing', queued=0, existing=existing, budget=budget, note='已有任务或可读材料；找到的片段可在此查看。')
    if budget['queued_active'] >= budget['queued_limit']:
        return dict(state='throttled', queued=0, existing=0, budget=budget, note='补证队列已满，请稍后再试。')
    return dict(state='unavailable', queued=0, existing=0, budget=budget, note='暂无带有效报道地址和批次时间的官方提及。')


def _public(row):
    archives = ([dict(stamp=row.minute, url=row.archives_url, sha256=row.archive_sha256)]
                if row.archive_sha256 else [])
    return dict(event_id=row.event_id, url=row.url, state=row.state, language=row.language,
                excerpt=row.excerpt, observed_at=row.observed_at, method=row.method,
                source_stamps=[a['stamp'] for a in archives], archives=archives,
                mention_ref=dict(feed=row.reference_feed, stamp=row.reference_stamp, row_index=row.reference_row),
                fragments=row.fragments, anchors=json.loads(row.anchors_json), note=row.note,
                actor1=row.actor1, actor2=row.actor2, cameo_root=row.root_code,
                provenance='gdelt_web_ngrams')


def event_status(session, event_id):
    order = case({'ready': 0, 'document_only': 1, 'running': 2, 'pending': 3, 'failed': 4},
                 value=NgramTarget.state, else_=5)
    rows = session.exec(select(NgramTarget).where(NgramTarget.event_id == event_id)
                        .order_by(order, NgramTarget.requested_at.desc(), NgramTarget.key).limit(10)).all()
    disabled = _disabled_reason()
    return dict(enabled=not disabled, disabled_reason=disabled, budget=_public_budget(session, _utc()),
                state=rows[0].state if rows else 'none', items=[_public(row) for row in rows],
                note=rows[0].note if rows else disabled or '可按已记录的报道链接检索 NGrams 片段。')


def iter_recovered(session, start, end, checkpoint=None, *, event_ids=None):
    query = select(NgramTarget).where(NgramTarget.state == 'ready', NgramTarget.observed_at >= start,
                                      NgramTarget.observed_at < end)
    if event_ids is not None:
        query = query.where(NgramTarget.event_id.in_(event_ids))
    for batch in session.exec(query.execution_options(yield_per=128)).partitions(128):
        if checkpoint is not None:
            checkpoint()
        for row in batch:
            yield _public(row)


def qualified_for_events(session, event_ids, cutoff=None):
    ids = list(dict.fromkeys(event_ids))
    if len(ids) > 100:
        raise ValueError('单页最多查询100条事件的补证')
    if not ids:
        return {}
    query = select(NgramTarget.key).where(NgramTarget.event_id == OfficialEvent.event_id, NgramTarget.state == 'ready')
    if cutoff is None:
        query = query.where(NgramTarget.observed_at <= _iso(_utc()))
    else:
        instant = datetime.fromisoformat(cutoff.replace('Z', '+00:00')) if isinstance(cutoff, str) else cutoff
        if instant.tzinfo is None:
            raise ValueError('截止时间需要时区')
        end = _iso(instant)
        query = query.where(NgramTarget.observed_at <= end if instant.microsecond else NgramTarget.observed_at < end)
    latest = query.order_by(NgramTarget.observed_at.desc(), NgramTarget.key.desc()).limit(1).correlate(OfficialEvent)
    keys = select(latest.scalar_subquery()).select_from(OfficialEvent).where(OfficialEvent.event_id.in_(ids))
    return {row.event_id: _public(row) for row in session.exec(select(NgramTarget).where(NgramTarget.key.in_(keys)))}



def _eligible(now):
    return or_((NgramTarget.state.in_(('pending', 'failed'))) & (NgramTarget.retry_at <= now),
               (NgramTarget.state == 'running') & (NgramTarget.retry_at <= now))


class NgramWorker:
    def __init__(self, engine, storage_root):
        self.engine = engine
        self.storage_root = storage_root
        self.stop_event = Event()
        self.thread = None
        self._discover_at = 0.
        self._budget_wait = 0.
        self.last_error = ''

    @property
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self):
        if not self.running and enabled():
            self.stop_event.clear()
            self.thread = Thread(target=self._run, name='gdelt-ngram-enrichment', daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(5)

    def _discover(self, now):
        maximum = _setting('NEWS2DOUYIN_NGRAMS_AUTO_TARGETS_PER_DAY', 64, 0, 2000)
        if maximum == 0 or time.monotonic() < self._discover_at:
            return
        self._discover_at = time.monotonic() + 900
        # Spread the allowance over a UTC day instead of spending it all at midnight.
        allowance = min(maximum, 1 + (now.hour * 60 + now.minute) * maximum // 1440)
        with Session(self.engine) as s:
            # Read one bounded page of recently reported mentions via the date index.
            # The optional task does not page the whole official feed or archive.
            budget = s.get(NgramBudget, now.date().isoformat())
            if budget and budget.automatic >= allowance:
                return
            recent = s.exec(select(OfficialMention).where(
                OfficialMention.last_seen >= _iso(now - timedelta(hours=2)), OfficialMention.last_seen <= _iso(now)
            ).order_by(OfficialMention.last_seen.desc()).limit(96)).all()
            if not recent:
                return
            keys = {mention.event_id for mention in recent}
            events = {e.event_id: e for e in s.exec(select(OfficialEvent).where(OfficialEvent.event_id.in_(keys)))}
            pairs = []
            seen = set()
            for m in recent:
                ev = events.get(m.event_id)
                if (not ev or action_fields_reason(ev.actor1_name, ev.actor2_name, ev.event_root_code)
                        or ev.event_id in seen or not source_url(m.doc_identifier)):
                    continue
                seen.add(ev.event_id)
                pairs.append((m, ev))
            if not pairs:
                return
            # Rank the bounded shortlist before spending the day's allowance. A
            # literal scope hit affects queue priority only; it never links a WatchEvent.
            matcher = read_focus_hints(s, now.astimezone(BEIJING).date())
            originals = resolve_previews(s, [m.doc_identifier for m, _ in pairs])
            focus_ids = set()
            if matcher:
                for mention, ev in pairs:
                    words = (ev.actor1_name, ev.actor2_name, ev.action_place, CAMEO_LABELS.get(ev.event_root_code, ''),
                             originals[mention.doc_identifier]['title'])
                    if matcher(' '.join(words)):
                        focus_ids.add(ev.event_id)
            pairs.sort(key=lambda pair: (pair[1].event_id in focus_ids, pair[1].initial_articles or 0,
                                         pair[0].confidence or 0), reverse=True)
            # Preserve the already read immutable argument/receipt values. Taking
            # the queue lock normally expires ORM rows; reloading up to 96 pairs
            # one by one would turn this bounded discovery page into N+1 reads.
            s.expunge_all()
            lock_news(s, 'ngram-queue')
            budget = _budget(s, now)
            queued, _ = _enqueue(s, pairs, now, max(0, allowance - budget.automatic),
                                 originals=originals, focus_ids=focus_ids)
            budget.automatic += queued
            s.commit()

    def _claim(self, now):
        if now.timestamp() < self._budget_wait:
            return None
        schedule = _eligible(now.timestamp())
        with Session(self.engine) as s:
            if not s.exec(select(NgramTarget.key).where(schedule).limit(1)).first():
                return None
            lock_news(s, 'ngram-queue')
            budget = _budget(s, now)
            if budget.scans >= _setting('NEWS2DOUYIN_NGRAMS_BATCHES_PER_DAY', 96, 1, 1440):
                tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                self._budget_wait = tomorrow.timestamp()
                return None
            minute = s.exec(select(NgramTarget.minute).where(schedule).order_by(
                NgramTarget.priority.desc(), NgramTarget.requested_at, NgramTarget.key).limit(1)).first()
            if not minute:
                return None
            group = s.exec(select(NgramTarget).where(schedule, NgramTarget.minute == minute)
                           .order_by(NgramTarget.priority.desc(), NgramTarget.requested_at, NgramTarget.key)
                           .limit(MAX_GROUP)).all()
            token = uuid4().hex
            targets = {}
            for row in group:
                row.lease = token
                row.retry_at = now.timestamp() + 900
                row.state = 'running'
                targets[row.key] = row.url
            budget.scans += 1
            s.commit()
        return token, minute, targets

    def _finish(self, token, minute, keys, results, archive_url, now):
        with Session(self.engine) as s:
            lock_news(s, 'ngram-queue')
            rows = s.exec(select(NgramTarget).where(NgramTarget.key.in_(keys),
                          NgramTarget.state == 'running', NgramTarget.lease == token)).all()
            for row in rows:
                result = results.get(row.url)
                status = result.status if result else 'failed'
                row.lease = ''
                row.retry_at = 0
                if status == 'ready':
                    if (result.stamp != minute or not re.fullmatch(r'[0-9a-fA-F]{64}', result.archive_sha256)
                            or not result.text.strip() or not result.fragments):
                        status = 'failed'
                    else:
                        decision = diagnose_excerpt(result.text, result.language, row.actor1, row.actor2, row.root_code)
                        row.excerpt, row.language = decision.excerpt, result.language
                        row.method, row.archive_sha256 = result.method, result.archive_sha256
                        row.fragments, row.archives_url = result.fragments, archive_url
                        row.observed_at = _iso(now)
                        row.anchors_json = json.dumps(decision.anchors, ensure_ascii=False)
                        row.state = 'ready' if decision.anchors else 'document_only'
                        row.note = REASON_LABELS[decision.reason]
                        continue
                if status == 'failed':
                    row.failures += 1
                    row.state = 'failed' if row.failures < 3 else 'unavailable'
                    row.retry_at = now.timestamp() + 120 * row.failures if row.state == 'failed' else 0
                    row.note = ('批次读取或重建失败，等待重试。' if row.state == 'failed'
                                else '批次读取或重建失败，自动重试已用尽。')
                    continue
                if status in ('missing_batch', 'not_found'):
                    row.minutes_left -= 1
                    if row.minutes_left > 0:
                        previous = _receipt_minute(minute) - timedelta(minutes=1)
                        row.minute = previous.strftime('%Y%m%d%H%M00')
                        row.state, row.failures = 'pending', 0
                        row.note = '本分钟文件未找到可用片段，继续检查同一提及的有限时间窗口。'
                        continue
                row.state = 'unavailable'
                row.note = {'unsupported_language': '该报道的语言或分词类型暂不支持。',
                            'limit': '分钟文件或片段超出处理上限，覆盖不完整。',
                            'unavailable': '缺少可用的 gdeltnews 依赖或重建能力。',
                            'not_found': '有限时间窗口未找到可重建的对应报道。',
                            'missing_batch': '有限时间窗口内的分钟文件缺失或未匹配。'}.get(
                                status, '未取得可用的重建片段。')
            s.commit()

    def step(self):
        if not enabled():
            return False
        now = _utc()
        self._discover(now)
        job = self._claim(now)
        if job is None:
            return False
        token, minute, targets = job
        from ..intake.ngrams import batch_url, reconstruct_minute
        results = {}
        try:
            results = reconstruct_minute(self.storage_root, minute, set(targets.values()), allow_download=_allow_download())
        except Exception:
            log.warning('Deferred NGrams minute %s', minute, exc_info=True)
        self._finish(token, minute, tuple(targets), results, batch_url(minute), _utc())
        return True

    def _run(self):
        wait = 2
        while not self.stop_event.wait(wait):
            try:
                wait = 1 if self.step() else 60
                self.last_error = ''
            except Exception as exc:
                self.last_error = type(exc).__name__
                log.warning('Deferred NGrams enrichment worker', exc_info=True)
                wait = 60
