import json
from collections import Counter
from datetime import date
from typing import Annotated
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, StringConstraints
from sqlmodel import Session, select
from ..knowledge import service as knowledge
from ..knowledge.models import FocusVisit, PointMaterial
from ..desk.models import GraphPending, DeskMeta, DeskDirty, Watch
from ..desk import service as desk
from ..extraction.models import VersionReading, FocusPoint, TextMention
from ..storage.models import utc_now_iso
from ..storage.articles import lock_news
from .research import same_origin
from .tasks import task_call


class EmbeddingDateRange(BaseModel):
    start_date: date
    end_date: date


class EmbeddingSearch(EmbeddingDateRange):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]


def register(app,engine,render):
    def db():
        with Session(engine,expire_on_commit=False) as s:yield s

    def info(s):
        from sqlalchemy import func
        counts=dict(s.exec(select(VersionReading.state,func.count()).group_by(VersionReading.state)).all())
        methods=dict(s.exec(select(VersionReading.method,func.count()).group_by(VersionReading.method)).all())
        return dict(**knowledge.status(s,app.state.desk.graph),extraction=counts,methods=methods,
            indexing=s.exec(select(func.count()).select_from(DeskDirty)).one())

    def page_status(s):
        return dict(info(s), embeddings=app.state.event_embeddings.status(s))

    @app.get('/knowledge')
    def page(s=Depends(db)):
        return render('knowledge.html',state=page_status(s),active='knowledge')

    @app.get('/api/knowledge/status')
    def status(s=Depends(db)):
        return page_status(s)

    @app.post('/api/knowledge/embeddings/backfill',status_code=202,dependencies=[Depends(same_origin)])
    def embedding_backfill(bounds:EmbeddingDateRange,s=Depends(db)):
        try:
            return app.state.event_embeddings.request_backfill(s,bounds.start_date,bounds.end_date)
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from None

    @app.post('/api/knowledge/embeddings/search',dependencies=[Depends(same_origin)])
    def embedding_search(filters:EmbeddingSearch):
        try:
            return app.state.event_embeddings.search(filters.query,filters.start_date,filters.end_date)
        except ValueError as exc:
            raise HTTPException(409,str(exc)) from None

    @app.get('/api/knowledge/graph')
    def graph(event:str='',watch:str='',s=Depends(db)):
        state=info(s)
        from ..knowledge.radar import sources
        data=task_call(knowledge.graph_view,s,event=event,watch=watch)
        provenance=sources(s,[node['id'] for node in data['nodes']])
        for node in data['nodes']:
            node['sources']=[{k:v for k,v in group.items() if k!='articles'} for group in provenance.get(node['id'],[])]
        return dict(**data,state=state)

    @app.get('/api/radar/view')
    def radar_view(q:str='',country:str='',topic:str='',mode:str='contains',band:str='',
                   period:str='auto',channel:str='all',provider:str='all',view:str='list',s=Depends(db)):
        from ..knowledge import radar as projection
        from .tasks import task_call
        filters=task_call(projection.checked,q=q,country=country,topic=topic,mode=mode,band=band,
            period=period,channel=channel,provider=provider,view=view)
        return dict(**task_call(projection.view,s,filters),state=info(s))

    @app.get('/api/radar/facets')
    def radar_facets(q:str='',country:str='',topic:str='',mode:str='contains',band:str='',
                     period:str='auto',channel:str='all',provider:str='all',view:str='list',s=Depends(db)):
        from fastapi.responses import JSONResponse
        from ..knowledge import radar as projection
        from .tasks import task_call
        filters=task_call(projection.checked,q=q,country=country,topic=topic,mode=mode,band=band,
            period=period,channel=channel,provider=provider,view=view)
        return JSONResponse(task_call(projection.facets,s,filters),headers={'Cache-Control':'no-store'})

    @app.post('/api/knowledge/rebuild',dependencies=[Depends(same_origin)])
    def rebuild(s=Depends(db)):
        lock_news(s, 'knowledge:rebuild')
        from ..desk.service import enqueue_graph
        from ..desk.graph_focus import force_rebuild
        force_rebuild(s,app.state.desk.graph)
        for row in s.exec(select(GraphPending)):
            enqueue_graph(s, row.key)
        previous=s.get(DeskMeta,'graph_analysis')
        if previous:s.delete(previous)
        app.state.desk.graph.bootstrapped=False
        s.commit();return {'queued':True}

    @app.post('/api/knowledge/retry-extraction',dependencies=[Depends(same_origin)])
    def retry(s=Depends(db)):
        lock_news(s, 'knowledge:rebuild')
        from ..extraction.models import ExtractionAttempt
        rows=s.exec(select(VersionReading).where(VersionReading.state=='error').limit(100)).all()
        for row in rows:
            attempt=s.get(ExtractionAttempt,row.version_id)
            row.state='model_queued' if attempt and attempt.method=='model' else 'baseline'
            row.note='';row.until=0;s.add(row)
            if attempt:s.delete(attempt)
        s.commit();return dict(queued=len(rows))

    @app.post('/api/focus/{key}/seen',dependencies=[Depends(same_origin)])
    def seen(key:str,s=Depends(db)):
        from ..desk.focus_lifecycle import require_active
        lock_news(s,'watch:'+key)
        task_call(require_active,s,key)
        s.merge(FocusVisit(watch=key,seen=utc_now_iso()));s.commit();return {'ok':True}
