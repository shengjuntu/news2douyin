"""Web and machine entry points for the same investigation queue."""
from datetime import date
from urllib.parse import urlencode
from typing import Annotated, Literal

from fastapi import Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, StringConstraints

from ..causal import agent, rounds, service
from ..causal.model import CausalModelError, configure
from ..causal.models import CausalAgentLink
from ..storage.db import session_scope
from .research import same_origin


class InvestigationInput(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    event_at: Annotated[str, StringConstraints(strip_whitespace=True, max_length=48)] = ''
    event_key: Annotated[str, StringConstraints(strip_whitespace=True, max_length=160)] = ''
    focus_key: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] = ''
    progress_key: Annotated[str, StringConstraints(strip_whitespace=True, max_length=256)] = ''
    automatic: bool = False
    runner: Literal['local', 'program', 'agent'] = 'local'
    date_from: Annotated[str, StringConstraints(strip_whitespace=True, max_length=10)] = ''


class AgentInput(BaseModel):
    date_from: Annotated[str, StringConstraints(strip_whitespace=True, max_length=10)] = ''


class ExpansionInput(BaseModel):
    continue_history: bool = False
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
             progress_key: Annotated[str, Query(max_length=256)] = '',
             investigation: Annotated[str, Query(max_length=35)] = ''):
        try:
            with session_scope(engine) as session:
                if progress_key:
                    from ..world_radar.relation_candidates import progress_seed
                    initial = progress_seed(session, progress_key)
                else:
                    initial = service.seed(session, event_key) if event_key else {}
        except KeyError:
            raise HTTPException(404, '未找到当前有效的事件或进展') from None
        page_root = '/radar/causes' + ('?' + urlencode({'progress_key': progress_key}) if progress_key else '')
        return render('causal.html', active='radar', seed=initial, task_key=investigation, causal_page_root=page_root)

    @app.get('/api/causal/investigations')
    def investigations(focus_key: Annotated[str, Query(max_length=100)] = ''):
        try:
            with session_scope(engine) as session:
                return _reply({'investigations': service.recent(session, focus_key)})
        except KeyError as exc:
            raise HTTPException(404, exc.args[0]) from None

    @app.post('/api/causal/investigations', status_code=202, dependencies=[Depends(same_origin)])
    def create(payload: InvestigationInput):
        local = payload.runner != 'agent'
        if local:
            _configured()
        try:
            if not local:
                agent.check_config(app.state.research)
            with session_scope(engine) as session:
                key = service.create(session, payload.text, payload.event_at, payload.event_key,
                                     payload.automatic, focus_key=payload.focus_key, queue_local=local,
                                     program=payload.runner == 'program', date_from=payload.date_from, progress_key=payload.progress_key)
                if local:
                    result = service.view(session, key)
                else:
                    binding = agent.bind(session, key, payload.date_from)
                    session.commit()
        except KeyError as exc:
            raise HTTPException(404, exc.args[0]) from None
        except service.CausalConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if local:
            app.state.causal.wake()
        else:
            result = agent.submit(engine, app.state.research, key, *binding)
        return _reply(result, 202)

    @app.get('/api/causal/investigations/{case_key}')
    def get(case_key: str, focus_key: Annotated[str, Query(max_length=100)] = ''):
        try:
            with session_scope(engine) as session:
                return _reply(service.view(session, case_key, focus_key=focus_key))
        except KeyError as exc:
            raise HTTPException(404, exc.args[0]) from None

    @app.get('/radar/causes/{case_key}/revisions/{revision_id}', response_class=HTMLResponse)
    def previous_results(case_key: str, revision_id: int):
        try:
            with session_scope(engine) as session:
                revision = rounds.read(session, case_key, revision_id)
        except KeyError as exc:
            raise HTTPException(404, exc.args[0]) from None
        return render('causal_revision.html', active='radar', revision=revision)

    @app.post('/api/causal/investigations/{case_key}/expand', status_code=202, dependencies=[Depends(same_origin)])
    def expand(case_key: str, payload: ExpansionInput):
        with session_scope(engine) as session:
            is_agent_investigation = session.get(CausalAgentLink, case_key) is not None
        if not is_agent_investigation:
            _configured()
        try:
            with session_scope(engine) as session:
                service.expand(session, case_key, payload.node_key, payload.automatic,
                               continue_history=payload.continue_history)
                result = service.view(session, case_key)
        except KeyError as exc:
            raise HTTPException(404, exc.args[0]) from None
        except service.CausalConflict as exc:
            raise HTTPException(409, str(exc)) from None
        app.state.causal.wake()
        return _reply(result, 202)

    @app.post('/api/causal/investigations/{case_key}/agent', status_code=202, dependencies=[Depends(same_origin)])
    def handoff(case_key: str, payload: AgentInput):
        try:
            agent.check_config(app.state.research)
            with session_scope(engine) as session:
                binding = agent.bind(session, case_key, payload.date_from)
                session.commit()
        except KeyError as exc:
            raise HTTPException(404, exc.args[0]) from None
        except service.CausalConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return _reply(agent.submit(engine, app.state.research, case_key, *binding), 202)

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
