from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit, urlencode
from fastapi import Request, Query, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlmodel import Session, select
from ..desk import service as desk
from ..desk.models import Watch, WatchEvent, DeskDirty, RadarEntry, GraphPending, WatchReview, EventPlace
from ..desk.rules import Scope, PRESETS, match
from ..search.service import event_query
from ..storage.models import Event, EventMoment
from ..storage.articles import lock_news
from ..research.models import ResearchRun, ResearchCase, ResearchQuestion, ResearchReport
from .research import same_origin
from .tasks import task_call
import re


def safe_back(raw):
    if not raw or '\\' in raw or any(ord(c)<32 for c in raw):return '/radar'
    try: u=urlsplit(raw)
    except ValueError:return '/radar'
    if u.scheme or u.netloc or not re.fullmatch(r'/(radar|focus(?:/w_[0-9a-f]{32})?)',u.path):return '/radar'
    return u.path+('?' + u.query if u.query else '')


class PlaceInput(BaseModel):
    role:str
    iso:str=Field(max_length=2)
    article:str=Field(max_length=160)
    revision:int=Field(ge=1)
    quote:str=Field(min_length=1,max_length=1500)

class Edit(Scope):
    version:int=Field(ge=1)

class Check(BaseModel):
    event:str=Field(default='',max_length=160)
    action:str='check'
    cutoff:str=Field(default='',max_length=10)

class Link(BaseModel):
    event:str=Field(min_length=1,max_length=160)
    action:str
class Create(Scope):
    seed:str=Field(default='',max_length=160)


def register_desk(app,engine,render):
    def db():
        with Session(engine,expire_on_commit=False) as s:yield s
    def remember(response,view):
        response.set_cookie('n2d_desk',view,max_age=31536000,httponly=True,samesite='lax')
        return response
    def context(request,s):
        mirror=app.state.desk.graph
        return dict(back=request.url.path+('?' + request.url.query if request.url.query else ''),
            indexing=s.exec(select(func.count()).select_from(DeskDirty)).one(),
            graph_enabled=bool(mirror.url),graph_error=mirror.last_error)
    @app.get('/')
    def home(request:Request):
        return RedirectResponse('/focus' if request.cookies.get('n2d_desk')=='focus' else '/radar',302)
    @app.get('/events',include_in_schema=False)
    def old_list(request:Request):
        return RedirectResponse('/radar'+('?' + request.url.query if request.url.query else ''),307)

    @app.get('/radar')
    def radar(request:Request,q:str='',country:str='',topic:str='',mode:str='contains',band:str='',
              period:str='auto',limit:int=Query(30,ge=1,le=500),offset:int=Query(0,ge=0),s=Depends(db)):
        desk.drain(engine,24)
        base=task_call(event_query,s,query=q,country=country,topic=topic,mode=mode)
        stmt=select(RadarEntry).where(RadarEntry.key.in_(base.with_only_columns(Event.event_key)))
        if band:stmt=stmt.where(RadarEntry.band==band)
        historical = False
        if period == 'auto':
            since=(datetime.now(timezone.utc)-timedelta(days=7)).isoformat(timespec='seconds').replace('+00:00','Z')
            recent=stmt.where(RadarEntry.seen>=since)
            if s.exec(recent.limit(1)).first():
                stmt=recent
            else:
                historical=True
        if period in ('1','7','30'):
            since=(datetime.now(timezone.utc)-timedelta(days=int(period))).isoformat(timespec='seconds').replace('+00:00','Z')
            stmt=stmt.where(RadarEntry.seen>=since)
        total=s.exec(select(func.count()).select_from(stmt.subquery())).one()
        items=s.exec(stmt.order_by(RadarEntry.priority.desc(),RadarEntry.seen.desc(),RadarEntry.key).offset(offset).limit(limit)).all()
        from ..desk.places import countries
        labels=countries()
        areas={}
        for mark in s.exec(select(EventPlace).where(EventPlace.event.in_([e.key for e in items]))):
            label=('行动地' if mark.role=='action' else '影响')+' · '+labels.get(mark.iso,mark.iso)
            areas.setdefault(mark.event,set()).add(label)
        params=dict(q=q,country=country,topic=topic,mode=mode,band=band,period=period,limit=limit)
        return remember(render('desk_radar.html',active='radar',items=items,total=total,offset=offset,limit=limit,
            params=params,areas=areas,historical=historical,querystr=urlencode(params),members={c.key:desk.memberships(s,c.key) for c in items},
            **context(request,s)),'radar')

    @app.get('/focus')
    @app.get('/focus/{key}')
    def focus(request:Request,key:str='',seed:str='',order:str='recent',branch:str='',offset:int=Query(0,ge=0),s=Depends(db)):
        desk.drain(engine,24)
        watches=s.exec(select(Watch).order_by(Watch.created,Watch.key)).all()
        w=task_call(desk.get_watch,s,key) if key else None
        total,items=desk.focus_entries(s,key,offset,order=order,branch=branch) if w else (0,[])
        reviews=s.exec(select(WatchReview,ResearchCase,ResearchRun).outerjoin(ResearchCase,ResearchCase.case_id==WatchReview.case_key)
              .outerjoin(ResearchRun,ResearchRun.run_id==ResearchCase.active_run_id).where(WatchReview.watch==key)
              .order_by(WatchReview.created.desc()).limit(30)).all() if w else []
        counts=dict(s.exec(select(WatchEvent.watch,func.count()).where(WatchEvent.choice!='exclude').group_by(WatchEvent.watch)).all())
        material = select(WatchEvent.event).where(WatchEvent.watch==key,WatchEvent.choice!='exclude')
        branches = s.exec(select(WatchEvent.branch).where(WatchEvent.watch==key,WatchEvent.choice!='exclude')
            .distinct().order_by(WatchEvent.branch)).all() if w else []
        tags = dict(s.exec(select(WatchEvent.event, WatchEvent.branch).where(WatchEvent.watch==key,
            WatchEvent.event.in_([card.key for card,_ in items]))).all()) if items else {}
        moments=s.exec(select(EventMoment).where(EventMoment.event_key.in_(material),
            EventMoment.deleted==False,EventMoment.time_kind=='occurred',EventMoment.date_start!='')
            .order_by(EventMoment.date_start.desc(),EventMoment.moment_key).limit(100)).all() if w else []
        excluded=s.exec(select(RadarEntry).join(WatchEvent,WatchEvent.event==RadarEntry.key)
            .where(WatchEvent.watch==key,WatchEvent.choice=='exclude')
            .order_by(RadarEntry.seen.desc()).limit(100)).all() if w else []
        cases=select(WatchReview.case_key).where(WatchReview.watch==key)
        questions=s.exec(select(ResearchQuestion).where(ResearchQuestion.case_id.in_(cases))
            .order_by(ResearchQuestion.updated_at.desc()).limit(30)).all() if w else []
        summaries=s.exec(select(ResearchReport).where(ResearchReport.case_id.in_(cases))
            .order_by(ResearchReport.created_at.desc()).limit(12)).all() if w else []
        seed_event=s.exec(select(Event).where(Event.event_key==seed)).first() if seed else None
        if seed and not seed_event:raise HTTPException(404,'事件不存在')
        return remember(render('desk_focus.html',active='focus',w=w,data=w.model_dump() if w else {},watches=watches,
            counts=counts,total=total,items=items,offset=offset,reviews=reviews,presets=PRESETS,seed=seed,
            order=order,branch=branch,branches=branches,tags=tags,questions=questions,summaries=summaries,
            timeline=moments,excluded=excluded,paging=urlencode(dict(order=order,branch=branch)),
            seed_title=seed_event.event_title if seed_event else '',**context(request,s)),'focus')

    @app.post('/api/focus',status_code=201,dependencies=[Depends(same_origin)])
    def create(p:Create,s=Depends(db)):
        return task_call(desk.save_watch,s,Scope(**p.model_dump()),seed=p.seed)
    @app.put('/api/focus/{key}',dependencies=[Depends(same_origin)])
    def edit(key:str,p:Edit,s=Depends(db)):
        return task_call(desk.save_watch,s,Scope(**p.model_dump()),key,p.version)
    @app.post('/api/focus/preview',dependencies=[Depends(same_origin)])
    def preview(p:Scope,s=Depends(db)):
        stmt=select(RadarEntry).order_by(RadarEntry.seen.desc(),RadarEntry.key).limit(250)
        checked=s.exec(stmt).all()
        matches=[{'key':c.key,'title':c.title,'reason':why} for c in checked if (why:=match(p,c))]
        included={row['key'] for row in matches}
        misses=[c.title for c in checked if c.key not in included][:3]
        return dict(checked=len(checked),matched=len(matches),examples=matches[:6],excluded=misses)
    @app.post('/api/focus/{key}/links',dependencies=[Depends(same_origin)])
    def add(key:str,p:Link,s=Depends(db)):
        task_call(desk.link_event,s,key,p.event,p.action)
        return {'ok':True}
    @app.post('/api/focus/{key}/reviews',dependencies=[Depends(same_origin)])
    def check(key:str,p:Check,s=Depends(db)):
        lock_news(s)
        w=task_call(desk.get_watch,s,key)
        member=s.get(WatchEvent,(key,p.event))
        if p.event and (not member or member.choice=='exclude'):raise HTTPException(409,'请先将事件归入专题')
        now = datetime.now(timezone.utc)
        day = p.cutoff or now.date().isoformat()
        try:
            end = datetime.strptime(day, '%Y-%m-%d').replace(hour=23,minute=59,second=59,tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(422, '截止日期格式无效')
        if end.date() > now.date() or (p.event and p.cutoff):
            raise HTTPException(422, '请选择不晚于今天的专题综述截止日期')
        cutoff = min(now,end).isoformat(timespec='seconds').replace('+00:00','Z')
        row=desk.make_review(s,w,p.event,('manual-' if p.event else 'summary-')+day,cutoff=cutoff)
        if row.status=='blocked':row.status='pending';s.add(row)
        s.commit()
        return dict(key=row.key,status=row.status)
    @app.post('/api/focus/{key}/reviews/{ticket}/retry',dependencies=[Depends(same_origin)])
    def retry(key:str,ticket:str,s=Depends(db)):
        lock_news(s)
        row=s.get(WatchReview,ticket)
        if not row or row.watch!=key:raise HTTPException(404,'补查不存在')
        if row.status=='blocked':
            row.status='pending';row.message='';s.add(row);s.commit()
        return {'status':row.status}
    @app.get('/api/desk/map-shapes')
    def shapes():
        from fastapi.responses import FileResponse
        from pathlib import Path
        return FileResponse(Path(__file__).parent/'data/desk-map.json',media_type='application/json')
    @app.post('/api/desk/events/{key}/places',dependencies=[Depends(same_origin)])
    def place(key:str,p:PlaceInput,s=Depends(db)):
        from ..desk.places import add
        return task_call(add,s,key,**p.model_dump())
    @app.delete('/api/desk/events/{key}/places/{place_id}',dependencies=[Depends(same_origin)])
    def remove_place(key:str,place_id:str,s=Depends(db)):
        from ..desk.models import EventPlace
        lock_news(s)
        row=s.get(EventPlace,place_id)
        if not row or row.event!=key:raise HTTPException(404,'标注不存在')
        s.delete(row);s.commit();return {'ok':True}
    @app.get('/api/desk/events/{key}/neighbors')
    def neighbors(key:str,s=Depends(db)):
        mirror=app.state.desk.graph
        if not mirror.url:return {'state':'未配置 FalkorDB','items':[]}
        try:matched=mirror.neighbors(key)
        except Exception:return {'state':'图未就绪或不可达','items':[]}
        result=[]
        for target,entity in matched:
            entry=s.get(RadarEntry,target)
            if entry:result.append({'key':target,'title':entry.title,'entity':entity})
        return {'state':'同实体线索，事件关系待核','items':result}
    @app.get('/api/desk/status')
    def status(request:Request,s=Depends(db)):
        return dict(**context(request,s),graph_pending=s.exec(select(func.count()).select_from(GraphPending)
             .where(GraphPending.written!=GraphPending.token)).one())
    @app.post('/api/desk/rebuild-graphs',dependencies=[Depends(same_origin)])
    def rebuild(s=Depends(db)):
        if not app.state.desk.graph.url:raise HTTPException(409,'尚未配置 FalkorDB')
        lock_news(s)
        for row in s.exec(select(GraphPending)):
            row.written='';s.add(row)
        s.commit()
        return {'ok':True}
