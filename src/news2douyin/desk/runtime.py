import logging
from threading import Event, Thread
from datetime import datetime, timezone
from sqlmodel import Session, select
from ..storage.articles import lock_news
from ..storage.models import utc_now_iso
from ..research import service as research
from . import service as desk
from .models import Watch, WatchEvent, WatchReview, RadarEntry
from .graph import GraphMirror

log=logging.getLogger(__name__)

class DeskWorker:
    def __init__(self,engine,research_runtime):
        self.engine=engine
        self.research=research_runtime
        self.graph=GraphMirror()
        self.stop_event=Event()
        self.threads=[]

    def start(self):
        self.stop_event.clear()
        for target in ([self.index_loop,self.graph_loop] if self.graph.url else [self.index_loop]):
            thread=Thread(target=target,daemon=True)
            thread.start()
            self.threads.append(thread)

    def stop(self):
        self.stop_event.set()
        for thread in self.threads:thread.join(timeout=15)
        self.threads.clear()

    def index_loop(self):
        while not self.stop_event.is_set():
            try:
                count=desk.drain(self.engine)
                self.review_step()
            except Exception:
                log.exception('desk worker tick failed')
                count=0
            self.stop_event.wait(.2 if count else 3)

    def graph_loop(self):
        while not self.stop_event.is_set():
            try:
                changed=self.graph.step(self.engine)
                self.stop_event.wait(.02 if changed else 3)
            except Exception as exc:
                # Do not expose URL/password through error messages.
                self.graph.last_error=type(exc).__name__
                self.stop_event.wait(5)

    def review_step(self):
        now=utc_now_iso()
        with Session(self.engine,expire_on_commit=False) as s:
            due=s.exec(select(Watch.key).where(Watch.enabled==True,Watch.auto_research==True,Watch.next_review<=now).limit(1)).first()
            if due:
                lock_news(s)
                w=s.get(Watch,due)
                if w.enabled and w.auto_research and w.next_review<=now:
                    evt=s.exec(select(RadarEntry.key).join(WatchEvent,WatchEvent.event==RadarEntry.key)
                        .where(WatchEvent.watch==due,WatchEvent.choice!='exclude',~WatchEvent.reason.startswith('背景'))
                        .order_by(RadarEntry.seen.desc(),RadarEntry.key).limit(1)).first()
                    if w.last_auto_day!=now[:10]:
                        desk.make_review(s,w,evt or '','due-'+w.next_review[:10]);w.last_auto_day=now[:10]
                    w.next_review=desk.iso_after(w.review_days);s.add(w);s.commit()
            pending=s.exec(select(WatchReview).where(WatchReview.status=='pending').order_by(WatchReview.created).limit(1)).first()
        if pending:self.dispatch(pending.key)

    def dispatch(self,key):
        with Session(self.engine,expire_on_commit=False) as s:
            item=s.get(WatchReview,key)
            if not item or item.status!='pending':return
            w=s.get(Watch,item.watch)
            if item.automatic and (not w.enabled or not w.auto_research or w.version!=item.scope_version):
                item.status='cancelled';item.message='专题设置已改变';s.add(item);s.commit();return
            keys=[]
            frozen=None
            if not item.event:
                from ..storage.utils import loads
                from ..storage.models import ArticleVersion
                frozen=[]
                cutoff=item.cutoff or item.created
                cards = s.exec(select(RadarEntry).join(WatchEvent, WatchEvent.event == RadarEntry.key)
                    .where(WatchEvent.watch == w.key, WatchEvent.choice != 'exclude')
                    .order_by(RadarEntry.seen.desc(), RadarEntry.key).execution_options(yield_per=30))
                for card in cards:
                    sources = [src for src in loads(card.sources, []) if src['when'] <= (item.cutoff or item.created)]
                    for source in sources:
                        article=source['key']
                        if article in keys:
                            continue
                        version=s.exec(select(ArticleVersion).where(ArticleVersion.article_key==article,
                            ArticleVersion.observed_at<=cutoff).order_by(ArticleVersion.revision.desc()).limit(1)).first()
                        if version:
                            keys.append(article)
                            frozen.append(dict(loads(version.payload_json,{}),article_key=article,
                                revision=version.revision,duplicate_of_article_key=source.get('duplicate_of','')))
                            break
                    if len(keys)==12:break
        try:
            research.create_case(self.engine,{'title':w.name+' · 定向核查','goal':item.question,'event_key':item.event,'article_keys':keys,'cutoff':item.cutoff or item.created},
                                 fixed_case_id=item.case_key,allow_empty=not item.event,prior_watch=w.key,frozen_sources=frozen)
            self.research.enqueue(item.case_key,request_run_id='watchrun_'+key)
            state,message='submitted','已交研究任务处理'
        except (ValueError,KeyError) as exc:
            state,message='blocked',str(exc)[:500]
        with Session(self.engine) as s:
            lock_news(s)
            row=s.get(WatchReview,key)
            row.status,row.message=state,message
            s.add(row);desk.enqueue_graph(s,'watch/'+w.key);s.commit()
