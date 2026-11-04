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
        return dict(**knowledge.graph_view(s,event=event,watch=watch),state=state)

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
