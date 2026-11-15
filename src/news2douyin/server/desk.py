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
from ..storage.queries import all_rows
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

class RemoveFocus(BaseModel):
    version:int=Field(ge=1)


def register_desk(app,engine,render):
    def db():
        with Session(engine,expire_on_commit=False) as s:yield s
    from .extraction import register as register_extraction
    from ..extraction import outline
    from ..extraction.service import focus_timeline
    from ..extraction.schema import LABELS
    from ..extraction.models import FocusPoint
    register_extraction(app,db)
    from .focus_init import register as register_focus_init
    register_focus_init(app,engine)
    from .gdelt_events import register as register_gdelt_events
    register_gdelt_events(app,engine,render)
    def remember(response,view):
        response.set_cookie('n2d_desk',view,max_age=31536000,httponly=True,samesite='lax')
        return response
    def context(request,s):
        mirror=app.state.desk.graph
        from ..knowledge.service import status
        return dict(back=request.url.path+('?' + request.url.query if request.url.query else ''),
            indexing=s.exec(select(func.count()).select_from(DeskDirty)).one(),
            graph_enabled=bool(mirror.url),graph_error=mirror.last_error,knowledge=status(s,mirror))
    @app.get('/')
    def home(request:Request):
        return RedirectResponse('/focus' if request.cookies.get('n2d_desk')=='focus' else '/radar',302)
    @app.get('/events',include_in_schema=False)
    def old_list(request:Request):
        return RedirectResponse('/radar'+('?' + request.url.query if request.url.query else ''),307)

    @app.get('/radar')
    def radar(request:Request,q:str='',country:str='',topic:str='',mode:str='contains',band:str='',
              period:str='auto',channel:str='all',provider:str='all',view:str='list',
              source:str='',cameo:str='',min_sources:int=Query(0,ge=0,le=1000),watch:str='',preset:str='',order:str='recent',
              event:str=Query('',pattern=r'^\d{0,30}$'),
              attention_topic:str=Query('',max_length=128),
              limit:int=Query(30,ge=1,le=500),offset:int=Query(0,ge=0),s=Depends(db)):
        if not source:
            source='local'
            if not request.query_params:
                from ..intake.official_models import OfficialEvent
                if s.exec(select(OfficialEvent.event_id).limit(1).execution_options(
                        n2d_query_name='selection.landing.source')).first() is not None:
                    source='gdelt'
                else:
                    from ..attention.models import AttentionTopic
                    if s.exec(select(AttentionTopic.key).limit(1)).first() is not None:source='attention'
        if source=='attention':
            from .attention import page_context
            result=page_context(s,q=q,period='72h' if period=='auto' else period,
                order='attention' if order=='recent' and 'order' not in request.query_params else order,
                watch=watch,offset=offset,limit=min(limit,100),attention_topic=attention_topic)
            return remember(render('attention_radar.html',active='radar',**result),'radar')
        if source=='gdelt':
            from .gdelt_events import page_context
            result=page_context(s,q=q,country=country,cameo=cameo,period='72h' if period=='auto' else period,
                min_sources=min_sources,watch=watch,preset=preset,order=order,offset=offset,limit=min(limit,100))
            result['open_event']=event
            return remember(render('gdelt_events.html',active='radar',**result),'radar')
        if source!='local':raise HTTPException(422,'事件来源视图无效')
        from ..knowledge import radar as projection
        params=task_call(projection.checked,q=q,country=country,topic=topic,mode=mode,band=band,
            period=period,channel=channel,provider=provider,view=view)
        data=task_call(projection.preview,s,params,offset,limit)
        from ..desk.places import countries
        labels=countries();areas={}
        for mark in s.exec(select(EventPlace).where(EventPlace.event.in_([e.key for e in data['items']]))):
            label=('行动地' if mark.role=='action' else '影响')+' · '+labels.get(mark.iso,mark.iso)
            areas.setdefault(mark.event,set()).add(label)
        params['limit']=limit
        return remember(render('desk_radar.html',active='radar',offset=offset,limit=limit,
            params=params,areas=areas,querystr=urlencode(params),
            facet_url='/api/radar/facets?'+urlencode({k:v for k,v in params.items() if k not in {'view','limit'}}),
            members=desk.memberships_many(s,[c.key for c in data['items']]),
            **data,**context(request,s)),'radar')

    @app.get('/focus')
    @app.get('/focus/{key}')
    def focus(request:Request,key:str='',seed:str='',order:str='recent',branch:str='',offset:int=Query(0,ge=0),s=Depends(db)):
        from ..desk.focus_lifecycle import visible_watch, trash_items
        watches=s.exec(select(Watch).where(visible_watch()).order_by(Watch.created,Watch.key)).all()
        w=task_call(desk.get_watch,s,key) if key else None
        total,items=desk.focus_entries(s,key,offset,order=order,branch=branch) if w else (0,[])
        reviews=s.exec(select(WatchReview,ResearchCase,ResearchRun).outerjoin(ResearchCase,ResearchCase.case_id==WatchReview.case_key)
              .outerjoin(ResearchRun,ResearchRun.run_id==ResearchCase.active_run_id).where(WatchReview.watch==key)
              .order_by(WatchReview.created.desc()).limit(30)).all() if w else []
        counts=dict(s.exec(select(WatchEvent.watch,func.count()).where(WatchEvent.choice!='exclude',desk.current_membership()).group_by(WatchEvent.watch)).all())
        material = select(WatchEvent.event).where(WatchEvent.watch==key,WatchEvent.choice!='exclude',desk.current_membership())
        branches = s.exec(select(WatchEvent.branch).where(WatchEvent.watch==key,WatchEvent.choice!='exclude',desk.current_membership())
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
        from ..knowledge.models import FocusChange,FocusVisit,PointMaterial
        from ..workbench.service import related
        visit=s.get(FocusVisit,key) if w else None
        changes=s.exec(select(FocusChange).where(FocusChange.watch==key,FocusChange.created>(visit.seen if visit else ''))
            .order_by(FocusChange.created.desc()).limit(30)).all() if w else []
        point_material={}
        if w:
            material_rows=all_rows(s,select(PointMaterial.point,RadarEntry.key,RadarEntry.title,PointMaterial.reason)
                .select_from(PointMaterial).join(FocusPoint,FocusPoint.key==PointMaterial.point)
                .join(RadarEntry,RadarEntry.key==PointMaterial.event)
                .join(WatchEvent,(WatchEvent.event==RadarEntry.key)&(WatchEvent.watch==key))
                .where(FocusPoint.watch==key,WatchEvent.choice!='exclude',desk.current_membership())
                .order_by(PointMaterial.point,RadarEntry.key),name='desk.focus_point_materials')
            for point,event,title,reason in material_rows:
                point_material.setdefault(point,[]).append(dict(key=event,title=title,reason=reason))
        if seed and not seed_event:raise HTTPException(404,'事件不存在')
        from ..desk.focus_jobs import definition as focus_definition, status as focus_status
        return remember(render('desk_focus.html',active='focus',w=w,data=w.model_dump() if w else {},watches=watches,
            trashed=trash_items(s),trash_open=request.query_params.get('trash')=='1',
            intent_definition=focus_definition(s,key) if w else None,
            tracking=focus_status(s,key) if w else {},
            counts=counts,total=total,items=items,offset=offset,reviews=reviews,presets=PRESETS,seed=seed,
            changes=changes,point_material=point_material,content_tasks=related(s,watch=key) if w else [],
            order=order,branch=branch,branches=branches,tags=tags,questions=questions,summaries=summaries,
            candidate_dates=focus_timeline(s,key) if w else [],action_labels=LABELS,points=outline.points(s,key) if w else [],point_history=outline.history(s,key) if w else [],
            timeline=moments,excluded=excluded,paging=urlencode(dict(order=order,branch=branch)),
            seed_title=seed_event.event_title if seed_event else '',**context(request,s)),'focus')

    @app.get('/focus/{key}/survey')
    def survey_page(key:str,day:str='',s=Depends(db)):
        from ..extraction import survey
        watch=task_call(desk.get_watch,s,key)
        plan=survey.build(s,watch,task_call(survey.cutoff_date,day))
        return render('focus_survey.html',active='focus',w=watch,plan=plan,day=day)

    @app.post('/api/focus',status_code=201,dependencies=[Depends(same_origin)])
    def create(p:Create,s=Depends(db)):
        return task_call(desk.save_watch,s,Scope(**p.model_dump()),seed=p.seed)
    @app.put('/api/focus/{key}',dependencies=[Depends(same_origin)])
    def edit(key:str,p:Edit,s=Depends(db)):
        return task_call(desk.save_watch,s,Scope(**p.model_dump()),key,p.version)
    @app.get('/api/focus/trash')
    def trash(s=Depends(db)):
        from ..desk.focus_lifecycle import trash_items
        return dict(items=trash_items(s))
    @app.delete('/api/focus/{key}',dependencies=[Depends(same_origin)])
    def remove_focus(key:str,p:RemoveFocus,s=Depends(db)):
        from ..desk.focus_lifecycle import remove
        return task_call(remove,s,key,p.version)
    @app.post('/api/focus/{key}/restore',dependencies=[Depends(same_origin)])
    def restore_focus(key:str,p:RemoveFocus,s=Depends(db)):
        from ..desk.focus_lifecycle import restore
        return task_call(restore,s,key,p.version)
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
        lock_news(s,'watch:'+key)
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
        lock_news(s,'watch:'+key,'watch-review:'+ticket)
        task_call(desk.get_watch,s,key)
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
        lock_news(s,'event:'+key)
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
        lock_news(s,'graph-queue-rebuild')
        from ..desk.graph_focus import force_rebuild
        force_rebuild(s,app.state.desk.graph)
        for row in s.exec(select(GraphPending)):
            desk.enqueue_graph(s,row.key)
        s.commit()
        return {'ok':True}

    @app.post('/api/desk/graph-maintenance/retry',dependencies=[Depends(same_origin)])
    def retry_graph_maintenance():
        mirror=app.state.desk.graph
        if not mirror.url:raise HTTPException(409,'尚未配置 FalkorDB')
        from ..desk.graph_indexes import reset
        from ..desk.graph_gc import retry
        reset(mirror)
        mirror._queue_index_checked=False
        mirror.bootstrapped=False
        mirror._maintenance_error=''
        retry(engine,mirror)
        # This only schedules work. No graph query, index build, node deletion
        # or full projection rebuild occurs in the HTTP request.
        return {'queued':True}

    @app.post('/api/desk/graph-maintenance/pause',dependencies=[Depends(same_origin)])
    def pause_graph_maintenance():
        mirror=app.state.desk.graph
        if not mirror.url:raise HTTPException(409,'尚未配置 FalkorDB')
        from ..desk.graph_gc import set_paused
        # Only the maintenance control changes. The current bounded batch may
        # finish its checkpoint; event/topic projection keeps running.
        return set_paused(engine,mirror,True)

    @app.post('/api/desk/graph-maintenance/resume',dependencies=[Depends(same_origin)])
    def resume_graph_maintenance():
        mirror=app.state.desk.graph
        if not mirror.url:raise HTTPException(409,'尚未配置 FalkorDB')
        from ..desk.graph_gc import enabled, set_paused
        if not enabled(mirror):raise HTTPException(409,'自动清理已在服务配置中关闭，请先启用该配置')
        # Resuming preserves the scan cursor and cycle, rather than pretending
        # that a pending/partially completed scan has finished.
        return set_paused(engine,mirror,False)
