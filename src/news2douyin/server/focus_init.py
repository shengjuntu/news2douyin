"""Endpoints for local scope drafts and asynchronous interpretation."""
from typing import Annotated, Literal

from fastapi import Depends
from pydantic import BaseModel, Field
from sqlmodel import Session

from ..desk import focus_jobs
from .research import same_origin
from .tasks import task_call


Anchor = Annotated[str, Field(min_length=2, max_length=160)]
Anchors = Annotated[list[Anchor], Field(min_length=1, max_length=12)]


class InterpretInput(BaseModel):
    description: str = Field(min_length=1, max_length=4000)
    method: Literal['local', 'model'] = 'model'


class CreateInput(BaseModel):
    draft: str = Field(min_length=1, max_length=80)
    spec: dict | None = None
    seed: str = Field(default='', max_length=160)
    anchors: Anchors | None = None


class DefinitionInput(BaseModel):
    version: int = Field(ge=1)
    description: str = Field(default='', max_length=4000)
    spec: dict | None = None
    draft: str = Field(default='', max_length=80)
    anchors: Anchors | None = None


def register(app, engine):
    def db():
        with Session(engine, expire_on_commit=False) as session:
            yield session

    @app.post('/api/focus/interpret', status_code=202, dependencies=[Depends(same_origin)])
    def interpret(payload: InterpretInput, s=Depends(db)):
        return task_call(focus_jobs.submit_build, s, payload.description, method=payload.method)

    @app.get('/api/focus/interpret/{key}')
    def get_interpretation(key: str, s=Depends(db)):
        return task_call(focus_jobs.get_build, s, key)

    @app.post('/api/focus/from-description', status_code=201, dependencies=[Depends(same_origin)])
    def create(payload: CreateInput, s=Depends(db)):
        return task_call(focus_jobs.create_from_build, s, payload.draft, payload.spec, payload.seed,
                         anchors=payload.anchors)

    @app.put('/api/focus/{key}/definition', dependencies=[Depends(same_origin)])
    def edit(key: str, payload: DefinitionInput, s=Depends(db)):
        return task_call(focus_jobs.update_definition, s, key, payload.version,
                         payload.description, payload.spec, payload.draft, anchors=payload.anchors)

    @app.get('/api/focus/{key}/tracking')
    def tracking(key: str, s=Depends(db)):
        return task_call(focus_jobs.status, s, key)

    @app.post('/api/focus/{key}/tracking/retry', dependencies=[Depends(same_origin)])
    def retry(key: str, s=Depends(db)):
        return task_call(focus_jobs.retry, s, key)

    @app.get('/api/focus/{key}/decisions/{event}')
    def decisions(key: str, event: str, s=Depends(db)):
        return task_call(focus_jobs.decisions, s, key, event)
