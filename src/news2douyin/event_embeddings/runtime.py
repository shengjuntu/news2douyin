"""A dedicated event-vector worker. Database writes and remote calls never overlap."""
from dataclasses import dataclass
import json
from math import hypot, isfinite
from threading import BoundedSemaphore, Event, Thread
from time import time
from uuid import uuid4

from sqlalchemy import update
from sqlmodel import Session, select

from ..llm.client.embedding import EmbeddingClient, EmbeddingConfigurationError, EmbeddingError
from ..storage.articles import lock_news
from ..storage.models import utc_now_iso
from .documents import digest, document, profile
from .models import EventEmbeddingBackfill, EventEmbeddingJob, EventEmbeddingRecord
from .queue import ACTIVE, bounds, counts, describe_backfill, enabled, enqueue_enabled
from .queue import request_backfill, save_record, scan_step

BATCH_SIZE = 16
LEASE_SECONDS = 120
RETRY_DELAYS = (5, 30, 120)


@dataclass(frozen=True)
class Claim:
    key: str
    lease: str


def vector_client():
    if not enabled():
        raise EmbeddingConfigurationError('事件向量后台已暂停')
    client = EmbeddingClient.from_env()
    client.dimensions = client.dimensions or 1024
    if client.dimensions > 16_000:
        raise EmbeddingConfigurationError('事件向量的维度不能超过 16000')
    return client


def normalized(vector):
    norm = hypot(*vector)
    if not isfinite(norm) or norm == 0:
        raise EmbeddingError('向量服务返回了无法用于相似度计算的向量')
    return [component / norm for component in vector]


def _due(now):
    return (EventEmbeddingJob.state.in_(ACTIVE), EventEmbeddingJob.available_at <= now)


def claim_jobs(engine):
    now = time()
    eligible = select(EventEmbeddingJob.event_key).where(*_due(now)).limit(1)
    with Session(engine) as session:
        if session.exec(eligible).first() is None:
            return []
    with Session(engine) as session:
        if engine.dialect.name == 'sqlite':
            lock_news(session)
        statement = (select(EventEmbeddingJob).where(*_due(now))
                     .order_by(EventEmbeddingJob.available_at, EventEmbeddingJob.event_key)
                     .limit(BATCH_SIZE).with_for_update(skip_locked=True))
        rows = session.exec(statement).all()
        claims = []
        for row in rows:
            row.state, row.lease, row.available_at = 'running', uuid4().hex, now + LEASE_SECONDS
            claims.append(Claim(row.event_key, row.lease))
        session.commit()
    return claims


def _prepare(engine, claims, current_profile):
    documents, skipped, reused = {}, set(), set()
    with Session(engine) as session:
        for claim in claims:
            key = claim.key
            value = document(session, key)
            if not value:
                skipped.add(key)
                continue
            fingerprint = digest(value)
            present = session.exec(select(EventEmbeddingRecord.profile, EventEmbeddingRecord.content_hash)
                                   .where(EventEmbeddingRecord.event_key == key)).first()
            if present and tuple(present) == (current_profile, fingerprint):
                reused.add(key)
            else:
                documents[key] = (fingerprint, value)
    return documents, skipped, reused


def _finish(engine, claims, *, model='', current_profile='', encoded=None,
            reused=(), skipped=(), error=''):
    encoded = encoded or {}
    with Session(engine) as session:
        if engine.dialect.name == 'sqlite':
            lock_news(session)
        for claim in sorted(claims, key=lambda claim: claim.key):
            row = session.exec(select(EventEmbeddingJob).where(EventEmbeddingJob.event_key == claim.key)
                               .with_for_update()).first()
            if not row or row.lease != claim.lease:
                # A newer edit or a restarted worker owns this event now.
                continue
            row.lease, row.available_at = '', 0
            if claim.key in skipped:
                row.state = 'skipped'
            elif claim.key in reused:
                row.state = 'ready'
            elif claim.key in encoded:
                fingerprint, vector = encoded[claim.key]
                save_record(session, claim.key, current_profile, model, fingerprint,
                            json.dumps(vector, separators=(',', ':')), utc_now_iso())
                row.state = 'ready'
            elif error:
                row.error = error
                row.attempts += 1
                row.state = 'failed'
                if row.attempts <= len(RETRY_DELAYS):
                    row.state = 'pending'
                    row.available_at = time() + RETRY_DELAYS[row.attempts - 1]
                continue
            else:
                continue
            row.attempts, row.error = 0, ''
        session.commit()


class EventEmbeddingWorker:
    def __init__(self, engine):
        self.engine = engine
        self.stop_event = Event()
        self.thread = None
        self.client = None
        self.search_slot = BoundedSemaphore(1)
        self.last_error = ''

    def status(self, session):
        active = enabled()
        try:
            vector_client()
            ready, message = True, '已配置向量服务'
        except EmbeddingConfigurationError as exc:
            ready, message = False, str(exc)
        summary = counts(session)
        if ready and self.last_error:
            message = self.last_error
        elif ready and summary['failed']:
            message = '部分事件生成失败，可按日期补算重试'
        batch = describe_backfill(session, session.get(EventEmbeddingBackfill, 1))
        return dict(enabled=active, ready=ready, message=message, counts=summary, backfill=batch)

    def request_backfill(self, session, start_date, end_date):
        try:
            vector_client()
        except EmbeddingConfigurationError as exc:
            raise ValueError(str(exc)) from None
        return request_backfill(session, start_date, end_date)

    def step(self):
        if not enqueue_enabled():
            return False
        if self.client is None:
            self.client = vector_client()
        current_profile = profile(self.client) if self.client.model else None
        scanning = scan_step(self.engine, current_profile)
        claims = claim_jobs(self.engine)
        if not claims:
            return scanning
        documents, skipped, reused, encoded = {}, (), (), {}
        error = ''
        try:
            model = self.client.resolve_model()
            current_profile = profile(self.client)
            documents, skipped, reused = _prepare(self.engine, claims, current_profile)
            if documents:
                vectors = self.client.get_embedding([value for _, value in documents.values()])
                encoded = {
                    key: (fingerprint, normalized(vector))
                    for (key, (fingerprint, _)), vector in zip(documents.items(), vectors, strict=True)
                }
        except EmbeddingError as exc:
            model = ''
            error = str(exc)
        _finish(self.engine, claims, model=model, current_profile=current_profile or '', encoded=encoded,
                skipped=skipped, reused=reused, error=error)
        self.last_error = error
        return True

    def search(self, query, start_date, end_date):
        from .search import find_similar
        lower, upper = bounds(start_date, end_date)
        query = query.strip()
        if not query or len(query) > 2_000:
            raise ValueError('请输入 1 至 2000 字的事件描述')
        if not self.search_slot.acquire(blocking=False):
            raise ValueError('正在检索，请稍后再试')
        try:
            client = vector_client()
            vector = normalized(client.get_embedding(query)[0])
            with Session(self.engine) as session:
                events = find_similar(session, profile=profile(client), vector=vector, lower=lower, upper=upper)
            return dict(events=events, model=client.model)
        except EmbeddingError as exc:
            raise ValueError(str(exc)) from None
        finally:
            self.search_slot.release()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                work = self.step()
                delay = 5 if self.last_error else 0.25 if work else 3
            except EmbeddingConfigurationError as exc:
                self.last_error, delay = str(exc), 10
            except Exception:
                self.last_error, delay = '事件向量后台处理失败，等待重试', 10
            self.stop_event.wait(delay)

    def start(self):
        if enqueue_enabled() and not (self.thread and self.thread.is_alive()):
            self.stop_event.clear()
            self.thread = Thread(target=self._loop, name='event-embeddings', daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(5)
