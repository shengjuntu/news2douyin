import json
from pathlib import Path
from fastapi import Request, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlmodel import Session, select
from ..topics import service as ts
from ..topics.models import TopicWorkspace, TopicCandidate
from .research import call, same_origin


def register_topic_routes(app,engine):
    env=Environment(loader=FileSystemLoader(str(Path(__file__).parent/'templates')),autoescape=select_autoescape(['html']))
    @app.get('/',response_class=HTMLResponse)
    @app.get('/topics',response_class=HTMLResponse)
    def page():return HTMLResponse(env.get_template('topics.html').render(active='topics'))

    @app.get('/api/topics')
    def listing():
        with Session(engine) as s:
            return [dict(workspace_id=w.workspace_id,title=w.title,updated_at=w.updated_at,revision=w.revision)
                for w in s.exec(select(TopicWorkspace).order_by(TopicWorkspace.updated_at.desc()).limit(100))]

    @app.post('/api/topics',status_code=201)
    def create(payload:ts.Create,request:Request):
        same_origin(request);return call(ts.create,engine,payload.model_dump())

    @app.get('/api/topics/{wid}')
    def detail(wid:str):
        with Session(engine) as s:
            data=call(ts.detail,s,wid)
        data['rundesk_url']=app.state.research_settings.get()['rundesk_url']
        return data

    @app.put('/api/topics/{wid}')
    def edit(wid:str,payload:ts.Edit,request:Request):
        same_origin(request);return call(ts.edit,engine,wid,payload.model_dump())

    from pydantic import BaseModel,Field,ConfigDict
    class Add(BaseModel):
        model_config=ConfigDict(extra='forbid')
        revision:int=Field(ge=1)
        article_key:str=Field(min_length=1,max_length=128)
    @app.post('/api/topics/{wid}/sources')
    def add(wid:str,payload:Add,request:Request):
        same_origin(request);return call(ts.add_article,engine,wid,payload.article_key,payload.revision)

    @app.get('/api/topics/{wid}/sources/{sid}')
    def source(wid:str,sid:str):
        with Session(engine) as s:
            row=call(ts.get_source,s,wid,sid);return ts.source_data(s,row,True)

    @app.get('/api/topics/{wid}/search')
    def search(wid:str,kind:str='articles',scope:str=Query('',max_length=4000),offset:int=Query(0,ge=0),limit:int=Query(20,ge=1,le=30)):
        with Session(engine) as s:
            w=call(ts.require,s,wid)
            def run():return ts.search(s,json.loads(scope) if scope else json.loads(w.scope_json),kind,offset,limit)
            return call(run)

    @app.post('/api/topics/{wid}/messages',status_code=202)
    def message(wid:str,payload:ts.Message,request:Request):
        same_origin(request)
        with Session(engine) as s:w=call(ts.require,s,wid)
        return call(app.state.research.enqueue,w.case_id,topic_request=payload.model_dump())

    @app.post('/api/topics/{wid}/sync')
    def sync(wid:str,request:Request):
        same_origin(request)
        with Session(engine) as s:w=call(ts.require,s,wid);case=ts.rs.require_case(s,w.case_id)
        if case.active_run_id:call(app.state.research.sync,case.active_run_id)
        return detail(wid)

    @app.post('/api/topics/{wid}/stop')
    def stop(wid:str,request:Request):
        same_origin(request)
        with Session(engine) as s:w=call(ts.require,s,wid)
        return call(app.state.research.cancel,w.case_id)

    @app.post('/api/topics/{wid}/handoffs',status_code=202)
    def handoff(wid:str,payload:ts.Handoff,request:Request):
        same_origin(request);return call(ts.handoff,engine,app.state.research,wid,payload.model_dump())

    @app.post('/api/topics/{wid}/handoffs/{hid}/resume',status_code=202)
    def resume(wid:str,hid:str,request:Request):
        same_origin(request);return call(ts.resume_handoff,engine,app.state.research,wid,hid)
