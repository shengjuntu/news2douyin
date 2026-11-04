import json
from collections import Counter
from fastapi import Depends, HTTPException, Request
from sqlmodel import Session, select
from ..knowledge import service as knowledge
from ..knowledge.models import FocusVisit, PointMaterial
from ..desk.models import GraphPending, DeskMeta, DeskDirty, Watch
from ..desk import service as desk
from ..extraction.models import VersionReading, FocusPoint, TextMention
from ..storage.models import utc_now_iso
from ..storage.articles import lock_news
from .research import same_origin


def register(app,engine,render):
    def db():
        with Session(engine,expire_on_commit=False) as s:yield s

    def info(s):
        from sqlalchemy import func
        counts=dict(s.exec(select(VersionReading.state,func.count()).group_by(VersionReading.state)).all())
        methods=dict(s.exec(select(VersionReading.method,func.count()).group_by(VersionReading.method)).all())
        return dict(**knowledge.status(s,app.state.desk.graph),extraction=counts,methods=methods,
            indexing=s.exec(select(func.count()).select_from(DeskDirty)).one())

    @app.get('/knowledge')
    def page(s=Depends(db)):
        return render('knowledge.html',state=info(s),active='knowledge')

    @app.get('/api/knowledge/status')
    def status(s=Depends(db)):
        return info(s)

    @app.get('/api/knowledge/graph')
    def graph(event:str='',watch:str='',s=Depends(db)):
        state=info(s)
        from ..knowledge.radar import sources
        data=knowledge.graph_view(s,event=event,watch=watch)
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

    @app.post('/api/knowledge/rebuild',dependencies=[Depends(same_origin)])
    def rebuild(s=Depends(db)):
        lock_news(s)
        for row in s.exec(select(GraphPending)):
            row.written='';s.add(row)
        previous=s.get(DeskMeta,'graph_analysis')
        if previous:s.delete(previous)
        app.state.desk.graph.bootstrapped=False
        s.commit();return {'queued':True}

    @app.post('/api/knowledge/retry-extraction',dependencies=[Depends(same_origin)])
    def retry(s=Depends(db)):
        lock_news(s)
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
        if not s.get(Watch,key):raise HTTPException(404,'专题不存在')
        s.merge(FocusVisit(watch=key,seen=utc_now_iso()));s.commit();return {'ok':True}
