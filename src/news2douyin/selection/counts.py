"""Explicit, bounded catalogue counts; never on the radar's first paint.

The total is an observation of the currently indexed rows, not a fixed snapshot
of a continuously arriving feed. Cache scope is one application process.
"""
from collections import OrderedDict
from contextlib import contextmanager
import json
import math
import sqlite3
from threading import Lock
from time import monotonic

from sqlalchemy import func, text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session

from ..storage.db import is_lock_error
from ..storage.models import utc_now_iso
from . import service


@contextmanager
def query_budget(session, milliseconds=2500):
    """A per-transaction PG deadline and a temporary SQLite progress deadline."""
    connection=session.connection()
    if connection.dialect.name=='postgresql':
        session.execute(text("SELECT set_config('statement_timeout', :value, true)"),
                        {'value':str(milliseconds)})
        session.execute(text("SELECT set_config('lock_timeout', '250', true)"))
        yield
    else:
        db=connection.connection.driver_connection
        deadline=monotonic()+milliseconds/1000
        previous=db.execute('PRAGMA busy_timeout').fetchone()[0]
        db.execute('PRAGMA busy_timeout=250')
        db.set_progress_handler(lambda: int(monotonic()>=deadline),1000)
        try:yield
        finally:
            db.set_progress_handler(None,0)
            db.execute(f'PRAGMA busy_timeout={int(previous)}')


class CandidateCounter:
    def __init__(self,engine,*,ttl=30,budget_ms=2500,capacity=128):
        self.engine=engine
        self.ttl=ttl
        self.budget_ms=budget_ms
        self.capacity=capacity
        self.lock=Lock()
        self.cache=OrderedDict()
        self.running=set()

    def count(self,filters,limit=30):
        filters=service.checked(**filters)
        if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=100:
            raise ValueError('每页数量须为1–100的整数')
        if filters['watch']:
            from ..desk.focus_lifecycle import require_active
            with Session(self.engine) as session:require_active(session,filters['watch'])
        key=json.dumps({k:v for k,v in filters.items() if k!='preset'},sort_keys=True,ensure_ascii=False)
        with self.lock:
            old=self.cache.get(key)
            if old and monotonic()-old[0]<self.ttl:
                self.cache.move_to_end(key)
                return self._result(old[1],old[2],limit,cached=True)
            if key in self.running or len(self.running)>=4:
                return self._unavailable('busy','统计正在进行，请稍后重试。')
            self.running.add(key)
        try:
            with Session(self.engine) as session, session.begin():
                with query_budget(session,self.budget_ms):
                    query=service._filtered(session,filters).with_only_columns(
                        func.count(),maintain_column_froms=True).order_by(None)
                    total=int(session.exec(query.execution_options(
                        n2d_query_name='selection.candidates.count')).one())
            computed=utc_now_iso()
            with self.lock:
                self.cache[key]=(monotonic(),total,computed)
                self.cache.move_to_end(key)
                while len(self.cache)>self.capacity:self.cache.popitem(last=False)
            return self._result(total,computed,limit,cached=False)
        except DBAPIError as exc:
            original=getattr(exc,'orig',exc)
            code=getattr(original,'sqlstate',None) or getattr(original,'pgcode',None)
            if code=='57014' or getattr(original,'sqlite_errorcode',None)==sqlite3.SQLITE_INTERRUPT:
                return self._unavailable('timeout','统计超过时限，可缩小范围后重试；事件列表仍可浏览。')
            if is_lock_error(exc):
                return self._unavailable('busy','数据库正忙，稍后可重试统计；事件列表仍可浏览。')
            raise
        finally:
            with self.lock:self.running.discard(key)

    def _result(self,total,computed,limit,*,cached):
        return dict(status='complete',total=total,pages=math.ceil(total/limit),
            computed_at=computed,cached=cached,cache_seconds=self.ttl,
            note='当前筛选下已索引的来源事件记录；持续采集会改变总量。')

    def _unavailable(self,status,note):
        return dict(status=status,total=None,pages=None,computed_at='',cached=False,
                    cache_seconds=self.ttl,note=note)
