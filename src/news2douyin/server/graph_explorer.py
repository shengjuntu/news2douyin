"""Global activity graph and focus graph pages."""
from typing import Literal
from urllib.parse import urlencode

from fastapi import Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from ..desk.people_view import DAY_OPTIONS
from ..search.dates import calendar_day
from .research import same_origin
from .tasks import task_call


def _graph_scope(view: Literal['global', 'focus'] = 'global',
                 lens: Literal['all', 'people'] = 'all',
                 days: int = Query(30), until: str = Query('', max_length=10)):
    if lens == 'people' and view != 'focus':
        raise HTTPException(422, '人物与组织视图需要指定焦点')
    if days not in DAY_OPTIONS:
        raise HTTPException(422, '事件窗口须为30、90或365天')
    if until:
        task_call(calendar_day, until, 'Asia/Shanghai')
    return view, lens, days, until


def register(app, engine, render):
    from ..desk.graph_explorer import DEFAULT_LIMIT, MAX_LIMIT, explore, focuses
    from ..desk import focus_event_runtime

    @app.get('/graphs', response_class=HTMLResponse, include_in_schema=False)
    def page(focus: str = Query('', max_length=100),
             limit: int = Query(DEFAULT_LIMIT, ge=20, le=MAX_LIMIT),
             investigation: str = Query('', max_length=35), scope=Depends(_graph_scope)):
        view, lens, days, until = scope
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
        window = {}
        if days != 30:
            window['days'] = days
        if until:
            window['until'] = until
        people_params = {**params, 'lens': 'people', **window}
        endpoint = '/api/graphs?' + urlencode(people_params if lens == 'people' else {**params, **window})
        return render('graph_explorer.html', active='graphs', mode=view, choices=choices,
                      selected=selected, endpoint=endpoint if view == 'global' or selected else '',
                      lens=lens, days=days, until=until, day_options=DAY_OPTIONS, limit=limit,
                      all_url='/graphs?' + urlencode({**params, **window}),
                      people_url='/graphs?' + urlencode(people_params),
                      causal_page_root='/graphs?' + urlencode(params) if selected else '',
                      task_key=investigation if selected else '',
                      node_limits=sorted({100, DEFAULT_LIMIT, 480, MAX_LIMIT, limit}))

    @app.get('/api/graphs')
    def data(focus: str = Query('', max_length=100),
             limit: int = Query(DEFAULT_LIMIT, ge=20, le=MAX_LIMIT), scope=Depends(_graph_scope)):
        view, lens, days, until = scope
        if view == 'focus' and not focus:
            raise HTTPException(422, '请为焦点图提供焦点编号')
        if view == 'global' and focus:
            raise HTTPException(422, '焦点编号只用于焦点图')
        if view == 'focus':
            with Session(engine) as session:
                return task_call(focus_event_runtime.read, session, focus, lens=lens, limit=limit)
        return task_call(explore, engine, app.state.desk.graph, view=view, focus=focus, limit=limit)

    @app.get('/api/graphs/focus/{key}/rebuild')
    def rebuild_status(key: str):
        with Session(engine) as session:
            return task_call(focus_event_runtime.status, session, key)

    @app.post('/api/graphs/focus/{key}/rebuild', status_code=202, dependencies=[Depends(same_origin)])
    def rebuild(key: str, days: int = Query(30), until: str = Query('', max_length=10)):
        if days not in DAY_OPTIONS:
            raise HTTPException(422, '事件窗口须为30、90或365天')
        with Session(engine) as session:
            return task_call(focus_event_runtime.request, session, key, days=days, until=until)

