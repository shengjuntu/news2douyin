"""Official source-event candidates: read-only discovery, explicit topic creation."""
from datetime import date, datetime, timezone
import re
from zoneinfo import ZoneInfo
from fastapi import Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel import Session, select
from ..desk.models import Watch
from .research import same_origin
from .tasks import task_call


class SelectCandidate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(min_length=8, max_length=100)
    title: str = Field(default='', max_length=240)
    angle: str = Field(default='', max_length=1500)


class CandidateFilters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    q: str = Field(default='', max_length=200)
    country: str = Field(default='', max_length=3)
    cameo: str = Field(default='', max_length=4)
    period: str = Field(default='72h', max_length=10)
    min_sources: int = Field(default=0, ge=0, le=1000)
    watch: str = Field(default='', max_length=160)
    order: str = Field(default='recent', max_length=30)


class SaveFilter(BaseModel):
    model_config = ConfigDict(extra='forbid')
    name: str = Field(min_length=1, max_length=80)
    filters: CandidateFilters
    key: str | None = Field(default=None, max_length=160)


class NgramRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    retry: bool = False


def checked_filters(**values):
    from ..selection import service
    return task_call(service.checked, **values)


def display_time(value):
    if not value:return '未知'
    try:
        moment=datetime.fromisoformat(value.replace('Z','+00:00'))
        if moment.tzinfo is None:moment=moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')
    except (ValueError,TypeError):return str(value)


def page_context(session, **values):
    from ..selection import service
    filters = checked_filters(**{name: values.get(name, default) for name, default in (
        ('q',''), ('country',''), ('cameo',''), ('period','72h'), ('min_sources',0), ('watch',''), ('preset',''), ('order','recent'))})
    from ..desk.focus_lifecycle import require_active,visible_watch
    unavailable_watch=None
    if filters['watch']:
        try:require_active(session,filters['watch'])
        except KeyError:
            from urllib.parse import urlencode
            old=session.get(Watch,filters['watch'])
            unavailable_watch=dict(key=filters['watch'],name=old.name if old else '原焦点',
                clear_url='/radar?'+urlencode(dict(filters,source='gdelt',watch='',preset='',limit=values.get('limit',30))))
    data = (dict(items=[],total=None,has_more=False,coverage={},pagination=dict(
                page=1,limit=values.get('limit',30))) if unavailable_watch else
            task_call(service.list_candidates, session, filters,
                      offset=values.get('offset',0), limit=values.get('limit',30)))
    from ..desk.places import countries
    return dict(data=data, filters=filters, offset=values.get('offset',0), limit=values.get('limit',30),
                unavailable_watch=unavailable_watch,
                countries=countries(), cameos=service.CAMEO_LABELS, gdelt_time=display_time,
                watches=session.exec(select(Watch.key,Watch.name).where(visible_watch()).order_by(Watch.name)).all(),
                presets=task_call(service.list_filters,session))


def register(app,engine,render):
    from ..selection.counts import CandidateCounter
    counter=CandidateCounter(engine)
    def db():
        with Session(engine,expire_on_commit=False) as session:
            yield session

    @app.get('/api/gdelt/events')
    def events(q:str=Query('',max_length=200),country:str='',cameo:str='',period:str='72h',
               min_sources:int=Query(0,ge=0,le=1000),watch:str='',preset:str='',order:str='recent',
               offset:int=Query(0,ge=0),limit:int=Query(30,ge=1,le=100),session=Depends(db)):
        from ..selection import service
        filters=checked_filters(q=q,country=country,cameo=cameo,period=period,
            min_sources=min_sources,watch=watch,preset=preset,order=order)
        return task_call(service.list_candidates,session,filters,offset=offset,limit=limit)

    @app.get('/api/gdelt/events/count')
    def count(q:str=Query('',max_length=200),country:str='',cameo:str='',period:str='72h',
              min_sources:int=Query(0,ge=0,le=1000),watch:str='',preset:str='',order:str='recent',
              limit:int=Query(30,ge=1,le=100)):
        filters=checked_filters(q=q,country=country,cameo=cameo,period=period,
            min_sources=min_sources,watch=watch,preset=preset,order=order)
        return task_call(counter.count,filters,limit=limit)

    @app.get('/api/gdelt/overview')
    def overview(session=Depends(db)):
        from ..selection.overview import overview as source_overview
        return task_call(source_overview,session)

    @app.get('/api/gdelt/events/{event_id}')
    def event(event_id:str,session=Depends(db)):
        from ..selection import service
        return task_call(service.detail,session,event_id)

    @app.get('/api/gdelt/events/{event_id}/evidence')
    def evidence(event_id: str, day: date | None = None, session=Depends(db)):
        from ..selection.official_evidence import official_evidence
        if day and day > datetime.now(ZoneInfo('Asia/Shanghai')).date():
            raise HTTPException(422, '请选择今天或更早的北京时间日期')
        result = task_call(official_evidence, session, event_id, day)
        if result is None:
            raise HTTPException(404, '截至所选日期没有可用的官方编码记录')
        return result

    def check_record(session, event_id):
        from ..intake.official_models import OfficialEvent
        if not re.fullmatch(r'\d{1,30}', event_id):
            raise HTTPException(422, 'GDELT事件编号无效')
        if session.get(OfficialEvent, event_id) is None:
            raise HTTPException(404, '未找到这条官方事件')

    @app.get('/api/gdelt/events/{event_id}/ngrams')
    def ngram_status(event_id: str, session=Depends(db)):
        from ..selection.ngram_enrichment import event_status
        check_record(session, event_id)
        return task_call(event_status, session, event_id)

    @app.post('/api/gdelt/events/{event_id}/ngrams',dependencies=[Depends(same_origin)])
    def queue_ngrams(event_id: str, payload: NgramRequest):
        from ..selection.ngram_enrichment import enqueue_event
        return task_call(enqueue_event, engine, event_id, retry=payload.retry)

    def graph_context(result):
        mirror = app.state.desk.graph
        return dict(result, configured=bool(mirror.url), graph_name=mirror.prefix+'_active_global')

    @app.get('/api/gdelt/events/{event_id}/graph')
    def graph_status(event_id: str, session=Depends(db)):
        from ..desk.official_graph import status
        check_record(session, event_id)
        return graph_context(task_call(status, session, event_id))

    @app.post('/api/gdelt/events/{event_id}/graph',dependencies=[Depends(same_origin)])
    def queue_graph(event_id: str, session=Depends(db)):
        from ..desk.official_graph import request
        if not app.state.desk.graph.url:
            raise HTTPException(409, '尚未配置 FalkorDB')
        state = task_call(request, session, event_id)
        session.commit()
        return graph_context(state)

    @app.post('/api/gdelt/events/{event_id}/select',dependencies=[Depends(same_origin)])
    def select(event_id:str,payload:SelectCandidate):
        from ..selection import service
        return task_call(service.select_candidate,engine,event_id,**payload.model_dump())

    @app.get('/api/gdelt/filters')
    def filters(session=Depends(db)):
        from ..selection import service
        return dict(items=task_call(service.list_filters,session))

    @app.post('/api/gdelt/filters',dependencies=[Depends(same_origin)])
    def save_filter(payload:SaveFilter,session=Depends(db)):
        from ..selection import service
        return task_call(service.save_filters,session,**payload.model_dump())

    @app.delete('/api/gdelt/filters/{key}',dependencies=[Depends(same_origin)])
    def remove_filter(key:str,session=Depends(db)):
        from ..selection import service
        return task_call(service.delete_filter,session,key)
