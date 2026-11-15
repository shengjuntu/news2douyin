import logging
import random
import os
import time
from threading import Event, Thread
from datetime import datetime, timezone
from sqlmodel import Session, select
from ..storage.articles import lock_news
from ..storage.models import utc_now_iso
from ..research import service as research
from . import service as desk
from .models import Watch, WatchEvent, WatchReview, RadarEntry
from .graph import GraphMirror
from sqlalchemy.exc import DBAPIError

log=logging.getLogger(__name__)


def _interval(name,default,low,high):
    import math
    raw=os.getenv(name,'').strip()
    try:value=float(raw) if raw else default
    except ValueError:raise ValueError(name+' must be numeric') from None
    if not math.isfinite(value) or not low<=value<=high:
        raise ValueError(name+' is outside the supported range')
    return value


def conflict(exc):
    """Retry transient lock/serialization conflicts, not arbitrary programming errors."""
    if not isinstance(exc,DBAPIError):return False
    original=exc.orig
    code=getattr(original,'sqlstate',None) or getattr(original,'pgcode',None)
    if code in ('40001','40P01','55P03'):return True
    return any(part in str(original).lower() for part in ('database is locked','database table is locked','database is busy'))


def retry_delay(attempt):
    return min(30,.5*2**min(attempt,6)*random.uniform(.8,1.2))

class DeskWorker:
    def __init__(self,engine,research_runtime):
        self.engine=engine
        self.research=research_runtime
        self.graph=GraphMirror()
        self.stop_event=Event()
        self.threads=[]
        self.graph_success_interval=_interval('NEWS2DOUYIN_GRAPH_SUCCESS_INTERVAL',.01,.005,1)
        self.graph_gc_tick=_interval('NEWS2DOUYIN_GRAPH_GC_TICK_SECONDS',2.,.5,60)

    def start(self):
        self.stop_event.clear()
        for target in [self.index_loop,self.read_loop,self.model_loop,self.graph_loop,
                       self.focus_build_loop,self.focus_history_loop,self.focus_judge_loop]:
            thread=Thread(target=target,daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:thread.join(timeout=15)
        self.threads.clear()

    def index_loop(self):
        failures=0
        while not self.stop_event.is_set():
            try:
                count=desk.drain(self.engine,limit=8)
                from ..knowledge.service import update_outline
                update_outline(self.engine)
                self.review_step()
                failures=0
            except Exception as exc:
                failures+=1
                if conflict(exc):log.warning('desk worker database conflict; retrying')
                else:log.exception('desk worker tick failed')
                self.stop_event.wait(retry_delay(failures));continue
            self.stop_event.wait(.5 if count else 3)

    def read_loop(self):
        self.extract_loop(model=False)

    def focus_build_loop(self):
        from .focus_jobs import build_step
        self.focus_loop(build_step)

    def focus_history_loop(self):
        from .focus_jobs import scan_step
        self.focus_loop(scan_step)

    def focus_judge_loop(self):
        from .focus_jobs import judge_step
        self.focus_loop(judge_step)

    def focus_loop(self,step):
        failures=0
        while not self.stop_event.is_set():
            try:
                changed=step(self.engine)
                failures=0
            except Exception as exc:
                failures+=1
                # Retain durable checkpoints. An exception never triggers a
                # second database write merely to log the same failure.
                if conflict(exc):log.warning('topic worker database conflict; retrying')
                else:log.warning('topic worker tick failed (%s)',type(exc).__name__)
                self.stop_event.wait(retry_delay(failures));continue
            self.stop_event.wait(.5 if changed else 3)

    def model_loop(self):
        self.extract_loop(model=True)

    def extract_loop(self, model):
        from ..extraction.service import step
        failures=0
        while not self.stop_event.is_set():
            try:
                changed = step(self.engine, model)
                failures=0
            except Exception as exc:
                failures+=1
                if conflict(exc):log.warning('event reader database conflict; retrying')
                else:log.exception('event reader tick failed')
                self.stop_event.wait(retry_delay(failures));continue
            self.stop_event.wait(.15 if changed else 3)

    def graph_loop(self):
        from ..knowledge.service import refresh
        failures=0
        next_gc=0.
        while not self.stop_event.is_set():
            try:
                if not self.graph.url:
                    self.graph.last_error='未配置 NEWS2DOUYIN_FALKOR_URL'
                    self.stop_event.wait(5)
                    continue
                changed=self.graph.step(self.engine)
                if self.graph.work_state=='indexes':
                    self.stop_event.wait(.5)
                    continue
                if time.monotonic()>=next_gc:
                    # GC has its own progress/error state; maintenance failure
                    # never changes a successfully acknowledged projection.
                    try:
                        self.graph.maintenance_step(self.engine)
                        self.graph._maintenance_error=''
                    except Exception as exc:self.graph._maintenance_error=type(exc).__name__
                    next_gc=time.monotonic()+self.graph_gc_tick
                if not changed:
                    changed=refresh(self.engine,self.graph)
                self.graph.last_error=''
                failures=0
                self.stop_event.wait(self.graph_success_interval if changed else 3)
            except Exception as exc:
                # Do not expose URL/password through error messages.
                self.graph.last_error=type(exc).__name__
                # A reconnected server may have lost an individual graph or
                # index. Re-verify ownership and readiness before more writes.
                self.graph.bootstrapped=False
                from .graph_indexes import reset
                reset(self.graph)
                failures+=1
                self.stop_event.wait(retry_delay(failures) if conflict(exc) else min(30,5*failures))

    def review_step(self):
        now=utc_now_iso()
        with Session(self.engine,expire_on_commit=False) as s:
            due=s.exec(select(Watch.key).where(Watch.enabled==True,Watch.auto_research==True,Watch.next_review<=now).limit(1)).first()
            if due:
                lock_news(s,'watch:'+due)
                w=s.get(Watch,due)
                if w.enabled and w.auto_research and w.next_review<=now:
                    evt=s.exec(select(RadarEntry.key).join(WatchEvent,WatchEvent.event==RadarEntry.key)
                        .where(WatchEvent.watch==due,WatchEvent.choice!='exclude',desk.current_membership(),~WatchEvent.reason.startswith('背景'))
                        .order_by(RadarEntry.seen.desc(),RadarEntry.key).limit(1)).first()
                    if w.last_auto_day!=now[:10]:
                        from ..extraction.models import FocusPoint,PointReview
                        from ..knowledge.models import PointMaterial
                        points=s.exec(select(FocusPoint).where(FocusPoint.watch==w.key)).all()
                        history=s.exec(select(PointReview,WatchReview).join(WatchReview,WatchReview.key==PointReview.ticket)
                            .where(WatchReview.watch==w.key)).all()
                        last={}
                        for link,ticket in history:last[link.point]=max(last.get(link.point,''),ticket.created)
                        covered=set(s.exec(select(PointMaterial.point).where(PointMaterial.point.in_([p.key for p in points]))))
                        point=min(points,key=lambda p:(p.key in covered,last.get(p.key,''),p.created,p.key)) if points else None
                        ticket=desk.make_review(s,w,'' if point else evt or '','due-'+w.next_review[:10])
                        if point:
                            ticket.question=point.text;s.add(ticket)
                            s.merge(PointReview(ticket=ticket.key,point=point.key))
                        w.last_auto_day=now[:10]
                    w.next_review=desk.iso_after(w.review_days);s.add(w);s.commit()
            pending=s.exec(select(WatchReview).where(WatchReview.status=='pending').order_by(WatchReview.created).limit(1)).first()
        if pending:self.dispatch(pending.key)

    def dispatch(self,key):
        from .leases import acquire
        with acquire(self.engine,'watch-review/'+key,120) as lease:
            if lease is None:return
            self._dispatch(key,lease)

    def _dispatch(self,key,lease):
        # A successful removal cannot race a new research submission. This
        # durable lease spans only local case/enqueue work, never agent I/O.
        from .leases import acquire
        with Session(self.engine) as s:
            item=s.get(WatchReview,key)
            if not item or item.status!='pending':return
            watch=item.watch
        with acquire(self.engine,'watch-lifecycle/'+watch,120) as lifecycle:
            if lifecycle is None:return
            self._dispatch_active(key,lease)

    def _dispatch_active(self,key,lease):
        with Session(self.engine,expire_on_commit=False) as s:
            item=s.get(WatchReview,key)
            if not item or item.status!='pending':return
            w=s.get(Watch,item.watch)
            from .focus_lifecycle import is_deleted
            if not w or is_deleted(s,item.watch) or (item.automatic and (not w.enabled or not w.auto_research or w.version!=item.scope_version)):
                item.status='cancelled';item.message='专题设置已改变';s.add(item);s.commit();return
            keys=[]
            frozen=None
            plan=None
            if not item.event:
                from ..extraction import survey
                from ..extraction.models import PointReview
                point=s.get(PointReview,item.key)
                plan=survey.build(s,w,item.cutoff or item.created,point.point if point else '')
                frozen=survey.freeze(s,plan)
                keys=list(plan['selected'])
        try:
            lease.touch(force=True)
            research.create_case(self.engine,{'title':w.name+' · 定向核查','goal':item.question,'event_key':item.event,'article_keys':keys,'cutoff':item.cutoff or item.created},
                                 fixed_case_id=item.case_key,allow_empty=not item.event,prior_watch=w.key,frozen_sources=frozen,survey_packet=plan)
            self.research.enqueue(item.case_key,request_run_id='watchrun_'+key)
            state,message='submitted','已交研究任务处理'
        except (ValueError,KeyError) as exc:
            state,message='blocked',str(exc)[:500]
        with Session(self.engine) as s:
            lock_news(s,'lease/'+lease.key,'watch:'+w.key,'watch-review:'+key)
            lease.confirm(s)
            row=s.get(WatchReview,key)
            if not row or row.status!='pending':return
            current=s.get(Watch,w.key)
            from .focus_lifecycle import is_deleted
            if not current or is_deleted(s,w.key) or (row.automatic and (not current.enabled or not current.auto_research or current.version!=row.scope_version)):
                state,message='cancelled','专题设置已改变'
            row.status,row.message=state,message
            s.add(row);desk.enqueue_graph(s,'watch/'+w.key);s.commit()
