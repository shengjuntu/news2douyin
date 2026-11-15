"""Explicit background source-content enrichment and bounded SQL polling."""
from fastapi import Depends,Query
from pydantic import BaseModel,ConfigDict,Field
from sqlmodel import Session

from .research import same_origin
from .tasks import task_call


class EnrichContent(BaseModel):
    model_config=ConfigDict(extra='forbid')
    event_ids:list[str]=Field(min_length=1,max_length=30)
    retry:bool=False


def register(app,engine):
    @app.post('/api/gdelt/content/enrich',dependencies=[Depends(same_origin)],status_code=202)
    def enrich(payload:EnrichContent):
        from ..selection.source_runtime import enqueue_events
        result=task_call(enqueue_events,engine,payload.event_ids,retry=payload.retry)
        worker=app.state.source_content
        if not worker.enabled:
            result.update(enabled=False,state='disabled',message='报道补充后台尚未启用，已保留请求；请在运行配置中启用后继续。')
        else:result['enabled']=True
        return result

    @app.get('/api/gdelt/content/cards')
    def cards(event_ids:str=Query('',max_length=1000)):
        from ..selection.service import content_cards
        keys=event_ids.split(',') if event_ids else []
        with Session(engine) as session:return task_call(content_cards,session,keys)

    @app.get('/api/gdelt/content/status')
    def status():
        from ..selection.source_runtime import status
        return task_call(status,engine,app.state.source_content)
