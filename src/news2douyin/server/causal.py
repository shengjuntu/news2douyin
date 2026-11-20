"""Web and machine entry points for the same investigation queue."""
from datetime import date
from typing import Annotated

from fastapi import Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, StringConstraints

from ..causal import service
from ..causal.model import CausalModelError, configure
from ..storage.db import session_scope
from .research import same_origin


class InvestigationInput(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    event_at: Annotated[str, StringConstraints(strip_whitespace=True, max_length=48)] = ''
    event_key: Annotated[str, StringConstraints(strip_whitespace=True, max_length=160)] = ''
    automatic: bool = False


class ExpansionInput(BaseModel):
    node_key: Annotated[str, StringConstraints(pattern=r'^cn_[0-9a-f]{32}$')]
    automatic: bool = False


class ArchiveInput(BaseModel):
    day: date
    retry: bool = False


def _configured():
    try:
        configure()
    except CausalModelError as exc:
        raise HTTPException(503, str(exc)) from None


def _reply(payload, status=200):
    return JSONResponse(payload, status_code=status, headers={'Cache-Control': 'no-store'})


def register(app, engine, render):
    @app.get('/radar/causes', response_class=HTMLResponse)
    def page(event_key: Annotated[str, Query(max_length=160)] = '',
             investigation: Annotated[str, Query(max_length=35)] = ''):
        try:
            with session_scope(engine) as session:
                initial = service.seed(session, event_key) if event_key else {}
        except KeyError:
            raise HTTPException(404, '未找到当前有效的整理事件') from None
        return render('causal.html', active='radar', seed=initial, task_key=investigation)

    @app.get('/api/causal/investigations')
    def investigations():
        with session_scope(engine) as session:
            return _reply({'investigations': service.recent(session)})

    @app.post('/api/causal/investigations', status_code=202, dependencies=[Depends(same_origin)])
    def create(payload: InvestigationInput):
        _configured()
        try:
            with session_scope(engine) as session:
                key = service.create(session, payload.text, payload.event_at, payload.event_key, payload.automatic)
                result = service.view(session, key)
        except KeyError:
            raise HTTPException(404, '未找到当前有效的整理事件') from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        app.state.causal.wake()
        return _reply(result, 202)

    @app.get('/api/causal/investigations/{case_key}')
    def get(case_key: str):
        try:
            with session_scope(engine) as session:
                return _reply(service.view(session, case_key))
        except KeyError:
            raise HTTPException(404, '调查不存在') from None

    @app.post('/api/causal/investigations/{case_key}/expand', status_code=202, dependencies=[Depends(same_origin)])
    def expand(case_key: str, payload: ExpansionInput):
        _configured()
        try:
            with session_scope(engine) as session:
                service.expand(session, case_key, payload.node_key, payload.automatic)
                result = service.view(session, case_key)
        except KeyError as exc:
            raise HTTPException(404, exc.args[0]) from None
        except service.CausalConflict as exc:
            raise HTTPException(409, str(exc)) from None
        app.state.causal.wake()
        return _reply(result, 202)

    @app.post('/api/causal/investigations/{case_key}/archive', status_code=202, dependencies=[Depends(same_origin)])
    def request_archive(case_key: str, payload: ArchiveInput):
        try:
            return _reply(service.request_archive(engine, case_key, payload.day, payload.retry), 202)
        except KeyError:
            raise HTTPException(404, '调查不存在') from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from None
