from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Literal
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
    static = Path(__file__).with_name('static')
    focus_script_version = sha256((static / 'focus.js').read_bytes()).hexdigest()[:16]
    focus_style_version = sha256((static / 'focus.css').read_bytes()).hexdigest()[:16]
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

    @app.get('/api/radar/daily')
    def daily_issue(day: str | None = None, summary: bool = False, s=Depends(db)):
        from .daily_radar import issue_data
        return issue_data(s, day, summary=summary)

    @app.post('/api/radar/daily', status_code=202, dependencies=[Depends(same_origin)])
    def request_daily_issue(day: str = Query(..., max_length=10)):
        from ..digest.service import request_issue
        from .daily_radar import resolve_day
        selected_day, _ = resolve_day(day)
        try:
            return task_call(request_issue, engine, selected_day)
        except RuntimeError as exc:
            raise HTTPException(503, '日报后台任务已停用。启用后可生成或更新日报。') from exc

    @app.get('/radar')
    def radar(request:Request,q:str='',country:str='',topic:str='',mode:str='contains',band:str='',
              period:str='auto',channel:str='all',provider:str='all',view:str='list',
              group_by:str='none',sort_by:str='recommended',source:str='',day:str | None=None,cameo:str='',min_sources:int=Query(0,ge=0,le=1000),watch:str='',preset:str='',order:str='recent',
              event:str=Query('',pattern=r'^\d{0,30}$'),
              attention_topic:str=Query('',max_length=128),
              limit:int=Query(30,ge=1,le=500),offset:int=Query(0,ge=0),s=Depends(db)):
        if not source:
            # Existing source-less search, filter and network bookmarks target the local radar.
            source = 'daily' if set(request.query_params).issubset({'day', 'source', 'group_by', 'sort_by'}) else 'local'
        if source == 'daily':
            from .daily_radar import page_context
            return remember(render('daily_radar.html', active='radar', **page_context(s, day, group_by, sort_by)), 'radar')
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
    def focus(request:Request,key:str='',seed:str='',order:str='recent',branch:str='',offset:int=Query(0,ge=0),
              lead_offset:int=Query(0,ge=0,le=100000),video_day:str=Query('',max_length=10),
              video_key:str=Query('',max_length=160),hotspot:str=Query('',max_length=128),day:str=Query('',max_length=10),
              date_basis:Literal['observed','occurred']='observed',
              view:Literal['events','leads','archive']='events',s=Depends(db)):
        from ..desk.focus_lifecycle import visible_watch, trash_items
        from ..desk.focus_leads import recent
        from ..desk.focus_read import checked_dates
        if day:
            task_call(checked_dates,day,day,date_basis)
        if lead_offset and 'view' not in request.query_params:
            view='leads'
        seed_text, seed_name, seed_terms = '', '', []
        if hotspot:
            if key or seed or video_day or video_key:
                raise HTTPException(422, '热点只能用于新建焦点')
            topic = app.state.hotspot_board.get(s, hotspot)
            if topic is None:
                raise HTTPException(404, '这个热点已更新，请从热点页重新打开')
            seed_name = topic['title'][:80]
            seed_terms = list(dict.fromkeys(term.strip() for term in topic.get('focus_terms', ())
                                             if isinstance(term, str) and 2 <= len(term.strip()) <= 160))[:12]
            seed_text = topic.get('focus_description') or f'持续跟踪「{seed_name}」的后续进展，记录重要行动和来源。'
            event_keys = [event[6:] for item in topic['sources']
                          if (event := item.get('event_key', '')).startswith('local:')]
            if event_keys:
                indexed = set(s.exec(select(Event.event_key).where(Event.event_key.in_(event_keys))))
                seed = next((event for event in event_keys if event in indexed), '')
        if video_day or video_key:
            if key or seed or not (video_day and video_key):
                raise HTTPException(422, '视频候选只能用于新建焦点')
            from .daily_radar import resolve_day
            from ..digest.video_service import read_issue
            edition_day, _ = resolve_day(video_day)
            card = next((item for item in read_issue(s, edition_day)['items'] if item['key'] == video_key), None)
            if card is None:
                raise HTTPException(404, '该期视频候选中没有这条材料')
            seed_text = f'持续跟踪「{card["title"]}」的后续进展，记录当事人的行动、具体变化和生活影响。'
        watches=s.exec(select(Watch).where(visible_watch()).order_by(Watch.created,Watch.key)).all()
        w=task_call(desk.get_watch,s,key) if key else None
        events=bool(w and view=='events')
        archive=bool(w and view=='archive')
        leads,more_leads=recent(s,w,offset=lead_offset) if w and view=='leads' else ([],False)
        total,items=desk.focus_entries(s,key,offset,order=order,branch=branch,day=day,date_basis=date_basis) if events else (0,[])
        reviews=s.exec(select(WatchReview,ResearchCase,ResearchRun).outerjoin(ResearchCase,ResearchCase.case_id==WatchReview.case_key)
              .outerjoin(ResearchRun,ResearchRun.run_id==ResearchCase.active_run_id).where(WatchReview.watch==key)
              .order_by(WatchReview.created.desc()).limit(30)).all() if archive else []
        material = select(WatchEvent.event).where(WatchEvent.watch==key,WatchEvent.choice!='exclude',desk.current_membership())
        branches = s.exec(select(WatchEvent.branch).where(WatchEvent.watch==key,WatchEvent.choice!='exclude',desk.current_membership())
            .distinct().order_by(WatchEvent.branch)).all() if events else []
        tags = dict(s.exec(select(WatchEvent.event, WatchEvent.branch).where(WatchEvent.watch==key,
            WatchEvent.event.in_([card.key for card,_ in items]))).all()) if items else {}
        moments=s.exec(select(EventMoment).where(EventMoment.event_key.in_(material),
            EventMoment.deleted==False,EventMoment.time_kind=='occurred',EventMoment.date_start!='')
            .order_by(EventMoment.date_start.desc(),EventMoment.moment_key).limit(100)).all() if archive else []
        excluded=s.exec(select(RadarEntry).join(WatchEvent,WatchEvent.event==RadarEntry.key)
            .where(WatchEvent.watch==key,WatchEvent.choice=='exclude')
            .order_by(RadarEntry.seen.desc()).limit(100)).all() if archive else []
        cases=select(WatchReview.case_key).where(WatchReview.watch==key)
        questions=s.exec(select(ResearchQuestion).where(ResearchQuestion.case_id.in_(cases))
            .order_by(ResearchQuestion.updated_at.desc()).limit(30)).all() if archive else []
        summaries=s.exec(select(ResearchReport).where(ResearchReport.case_id.in_(cases))
            .order_by(ResearchReport.created_at.desc()).limit(12)).all() if archive else []
        seed_event=s.exec(select(Event).where(Event.event_key==seed)).first() if seed else None
        from ..knowledge.models import FocusChange,FocusVisit,PointMaterial
        from ..workbench.service import related
        visit=s.get(FocusVisit,key) if events else None
        changes=s.exec(select(FocusChange).where(FocusChange.watch==key,FocusChange.created>(visit.seen if visit else ''))
            .order_by(FocusChange.created.desc()).limit(5)).all() if events and not day else []
        point_material={}
        if archive:
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
        paging=urlencode(dict(view=view,order=order,branch=branch,day=day,date_basis=date_basis))
        response = render('desk_focus.html',active='focus',w=w,data=w.model_dump() if w else {},watches=watches,
            trashed=trash_items(s),trash_open=request.query_params.get('trash')=='1',
            intent_definition=focus_definition(s,key) if w else None,
            tracking=focus_status(s,key) if w else {},
            total=total,items=items,offset=offset,reviews=reviews,presets=PRESETS,seed=seed,
            leads=leads,lead_offset=lead_offset,more_leads=more_leads,
            changes=changes,point_material=point_material,content_tasks=related(s,watch=key) if archive else [],
            order=order,branch=branch,branches=branches,tags=tags,questions=questions,summaries=summaries,
            candidate_dates=focus_timeline(s,key) if archive else [],action_labels=LABELS,points=outline.points(s,key) if archive else [],point_history=outline.history(s,key) if archive else [],
            timeline=moments,excluded=excluded,paging=paging,focus_view=view,day=day,date_basis=date_basis,
            seed_title=seed_event.event_title if seed_event else '',seed_text=seed_text,
            seed_name=seed_name,seed_terms=seed_terms,
            draft_scope=f'hotspot/{hotspot}' if hotspot else f'{video_day}/{video_key}' if video_key else seed,
            focus_script_version=focus_script_version,focus_style_version=focus_style_version,**context(request,s))
        response.headers['Cache-Control'] = 'private, no-cache'
        return remember(response,'focus')

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
        if p.event and (not member or member.choice=='exclude'):raise HTTPException(409,'请先将事件加入焦点')
        now = datetime.now(timezone.utc)
        day = p.cutoff or now.date().isoformat()
        try:
            end = datetime.strptime(day, '%Y-%m-%d').replace(hour=23,minute=59,second=59,tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(422, '截止日期格式无效')
        if end.date() > now.date() or (p.event and p.cutoff):
            raise HTTPException(422, '截止日期不能晚于今天')
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
