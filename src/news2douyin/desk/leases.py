"""Durable leases; no SQL transaction stays open while the caller performs I/O."""
from contextlib import contextmanager
import time
from uuid import uuid4
from sqlalchemy import update
from sqlmodel import Session
from ..storage.articles import lock_news
from .models import GraphLease


class LeaseLost(RuntimeError):
    pass


class Lease:
    def __init__(self, engine, key, owner, seconds):
        self.engine, self.key, self.owner = engine, key, owner
        self.seconds = seconds
        self.next_renewal = time.monotonic() + seconds / 3

    def touch(self, force=False):
        if not force and time.monotonic() < self.next_renewal:
            return
        now = time.time()
        with Session(self.engine) as s:
            result = s.exec(update(GraphLease).where(GraphLease.key == self.key,
                GraphLease.owner == self.owner, GraphLease.until > now)
                .values(until=now + self.seconds).execution_options(synchronize_session=False))
            if result.rowcount != 1:
                raise LeaseLost('background_work_lease_lost')
            s.commit()
        self.next_renewal = time.monotonic() + self.seconds / 3

    def release(self):
        with Session(self.engine) as s:
            s.exec(update(GraphLease).where(GraphLease.key == self.key, GraphLease.owner == self.owner)
                .values(owner='', until=0).execution_options(synchronize_session=False))
            s.commit()

    def confirm(self, session):
        """Caller holds lease/<key> while committing an acknowledgement/publication."""
        current=session.get(GraphLease,self.key)
        if not current or current.owner!=self.owner or current.until<=time.time():
            raise LeaseLost('background_work_lease_lost')


@contextmanager
def acquire(engine, key, seconds=120):
    owner = uuid4().hex
    lease = None
    with Session(engine) as s:
        lock_news(s, 'lease/' + key)
        now = time.time()
        current = s.get(GraphLease, key)
        if not current or current.until <= now:
            if not current:
                current = GraphLease(key=key)
            current.owner, current.until = owner, now + seconds
            s.add(current)
            s.commit()
            lease = Lease(engine, key, owner, seconds)
    try:
        yield lease
    finally:
        if lease:
            lease.release()
