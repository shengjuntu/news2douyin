from fastapi import Depends
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from .research import same_origin
from .tasks import task_call


class TranslationRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    kind: Literal['article','gdelt_event','topic_source','source_preview']
    key: str=Field(min_length=1,max_length=250)
    target: Literal['zh-CN']='zh-CN'
    scope: Literal['title','content','document']='document'
    revision: int | None=Field(default=None,ge=1)
    retry: bool=False


def register(app,engine):
    from ..translations import service
    @app.post('/api/translations',dependencies=[Depends(same_origin)])
    def translate(payload:TranslationRequest):
        return task_call(service.submit,engine,**payload.model_dump())

    @app.get('/api/translations/{ident}')
    def translation(ident:str):
        return task_call(service.get,engine,ident)
