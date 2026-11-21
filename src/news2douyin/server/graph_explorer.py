"""Read-only views of the global activity graph and each visible Focus graph."""
from typing import Literal
from urllib.parse import urlencode

from fastapi import HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from .tasks import task_call


def register(app, engine, render):
    from ..desk.graph_explorer import DEFAULT_LIMIT, MAX_LIMIT, explore, focuses

    @app.get('/graphs', response_class=HTMLResponse, include_in_schema=False)
    def page(view: Literal['global', 'focus'] = 'global', focus: str = Query('', max_length=100),
             limit: int = Query(DEFAULT_LIMIT, ge=20, le=MAX_LIMIT),
             investigation: str = Query('', max_length=35)):
        choices = []
        if view == 'focus':
            with Session(engine) as session:
                choices = task_call(focuses, session)
        if focus and (view != 'focus' or not any(option['key'] == focus for option in choices)):
            raise HTTPException(404, '没有找到这个焦点')
        selected = next((option for option in choices if not focus or option['key'] == focus), None)
        params = {'view': view}
        if selected:
            params['focus'] = selected['key']
        if limit != DEFAULT_LIMIT:
            params['limit'] = limit
        endpoint = '/api/graphs?' + urlencode(params) if view == 'global' or selected else ''
        return render('graph_explorer.html', active='graphs', mode=view, choices=choices,
                      selected=selected, endpoint=endpoint, limit=limit,
                      causal_page_root='/graphs?' + urlencode(params) if selected else '',
                      task_key=investigation if selected else '',
                      node_limits=sorted({100, DEFAULT_LIMIT, 480, MAX_LIMIT, limit}))

    @app.get('/api/graphs')
    def data(view: Literal['global', 'focus'] = 'global', focus: str = Query('', max_length=100),
             limit: int = Query(DEFAULT_LIMIT, ge=20, le=MAX_LIMIT)):
        if view == 'focus' and not focus:
            raise HTTPException(422, '请为焦点图提供焦点编号')
        if view == 'global' and focus:
            raise HTTPException(422, '焦点编号只用于焦点图')
        return task_call(explore, engine, app.state.desk.graph, view=view, focus=focus, limit=limit)
