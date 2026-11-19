import json
from pathlib import Path
from fastapi import Depends, HTTPException, Request, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlmodel import Session, select
from ..workbench import service as work
from ..workbench.models import ContentTask, ContentBinding, ContentTaskTrash
from ..topics.models import TopicWorkspace
from ..research.models import ResearchCase, ShortScriptBrief
from ..extraction.models import SurveyPacket
from ..desk.models import Watch
from ..storage.models import Event, ScriptPackage
from .research import same_origin
from .tasks import task_call


class Phase(BaseModel):
    phase: str
    version: int=Field(ge=1)


class ReportDraft(BaseModel):
    report_id: str
    request_id: str=Field(min_length=8,max_length=100)
    version: int=Field(ge=1)

class Adopt(BaseModel):
    kind: str
    ref: str


def owner(s,kind,ref):
    return s.exec(select(ContentBinding.task).where(ContentBinding.kind==kind,ContentBinding.ref==ref)
        .order_by(ContentBinding.created,ContentBinding.task)).first()


def register(app,engine,storage,render):
    def db():
        with Session(engine,expire_on_commit=False) as s:yield s

    @app.get('/workbench')
    def listing(view:str='',q:str='',offset:int=Query(0,ge=0),s=Depends(db)):
        stmt=(select(ContentTask,TopicWorkspace,ContentTaskTrash)
            .join(TopicWorkspace,TopicWorkspace.workspace_id==ContentTask.key)
            .outerjoin(ContentTaskTrash,ContentTaskTrash.task==ContentTask.key))
        if view=='trash':
            stmt=stmt.where(ContentTaskTrash.deleted_at!='').order_by(ContentTaskTrash.deleted_at.desc())
        else:
            stmt=stmt.where((ContentTaskTrash.task==None) | (ContentTaskTrash.deleted_at==''))
        if view in work.PHASES:stmt=stmt.where(ContentTask.phase==view)
        if q:stmt=stmt.where(TopicWorkspace.title.contains(q[:200]))
        rows=s.exec(stmt.order_by(ContentTask.updated.desc(),ContentTask.key).offset(offset).limit(40)).all()
        historical=[]
        if view=='history':
            from ..video.workbench import list_scripts
            scripts=list_scripts(s,limit=100)
            for item in scripts:
                if not owner(s,'script',item['package_key']):
                    historical.append(dict(kind='script',id=item['package_key'],title=item['title'] or item['package_key'],url='/scripts/'+item['package_key']))
            for c in s.exec(select(ResearchCase).where(~ResearchCase.case_id.in_(select(TopicWorkspace.case_id)))
                    .order_by(ResearchCase.created_at.desc()).limit(100)):
                if not owner(s,'case',c.case_id):
                    historical.append(dict(kind='case',id=c.case_id,title=c.title,url='/research/'+c.case_id))
        return render('workbench.html',active='workbench',rows=rows,phases=work.PHASES,view=view,q=q,
            offset=offset,historical=historical)

    @app.get('/workbench/new')
    def new(event:str='',watch:str='',article:str='',digest_day:str=Query('',max_length=10),
            progress:str=Query('',max_length=160),digest_token:str=Query('',pattern=r'^(?:[a-f0-9]{64})?$'),s=Depends(db)):
        if digest_day or progress or digest_token:
            if event or watch or article:
                raise HTTPException(422, '请单独选择日报中的进展')
            from ..digest.handoff import card_token, selected_card
            from ..tasks.control import TaskConflict
            try:
                card = selected_card(s, digest_day, progress, digest_token)
            except TaskConflict as exc:
                response = render('workbench_daily_changed.html', active='radar', day=digest_day, message=str(exc))
                response.status_code = 409
                return response
            except KeyError as exc:
                raise HTTPException(404, '这期日报没有这条进展') from exc
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            return render('workbench_new.html',active='workbench',event='',watch='',article='',
                title=card['title'][:250],watch_name='',digest_day=digest_day,progress=progress,digest_token=card_token(card))
        evt=s.exec(select(Event).where(Event.event_key==event)).first() if event else None
        topic=s.get(Watch,watch) if watch else None
        if watch:
            from ..desk.focus_lifecycle import require_active
            topic=task_call(require_active,s,watch)
        if event and not evt:raise HTTPException(404,'事件不存在')
        if watch and not topic:raise HTTPException(404,'焦点不存在')
        return render('workbench_new.html',active='workbench',event=event,watch=watch,article=article,
            title=evt.event_title if evt else topic.name+' · 新选题' if topic else '',watch_name=topic.name if topic else '')

    @app.post('/api/workbench',dependencies=[Depends(same_origin)],status_code=201)
    def create(payload:work.Start):
        return task_call(work.start,engine,payload.model_dump())

    @app.post('/api/workbench/import',dependencies=[Depends(same_origin)])
    def adopt(p:Adopt):
        return task_call(work.adopt,engine,p.kind,p.ref)

    @app.get('/api/workbench/{key}')
    def detail(key:str,s=Depends(db)):
        data=task_call(work.detail,s,key);s.commit();return data

    @app.delete('/api/workbench/{key}',dependencies=[Depends(same_origin)])
    def remove(key:str,version:int=Query(ge=0)):
        return task_call(work.set_trashed,engine,key,version,trashed=True)

    @app.post('/api/workbench/{key}/restore',dependencies=[Depends(same_origin)])
    def restore(key:str,version:int=Query(ge=0)):
        return task_call(work.set_trashed,engine,key,version,trashed=False)

    @app.post('/api/workbench/{key}/phase',dependencies=[Depends(same_origin)])
    def phase(key:str,p:Phase):
        return task_call(work.move,engine,key,p.phase,p.version)

    @app.post('/api/workbench/{key}/from-report',dependencies=[Depends(same_origin)])
    def draft(key:str,p:ReportDraft):
        return task_call(work.from_report,engine,storage,key,p.report_id,p.request_id,p.version)

    @app.get('/workbench/{key}')
    def resume(key:str,s=Depends(db)):
        if work.is_trashed(s,key):
            return RedirectResponse('/workbench?view=trash',303)
        t,w=task_call(work.require,s,key);s.commit()
        return RedirectResponse(f'/workbench/{key}/{t.phase}',303)

    @app.get('/workbench/{key}/{stage}')
    def workspace(key:str,stage:str,case:str='',script:str='',video:str='',mode:str='',s=Depends(db)):
        if stage not in work.PHASES:raise HTTPException(404,'阶段不存在')
        if work.is_trashed(s,key):
            return RedirectResponse('/workbench?view=trash',303)
        data=task_call(work.detail,s,key);s.commit()
        context=dict(active='workbench',work=data,work_stage=stage,phases=work.PHASES)
        if stage=='selection':
            return render('topics.html',**context)
        if stage=='research' and data['cases']:
            cid=case or data['cases'][-1]['id']
            if cid not in {c['id'] for c in data['cases']}:raise HTTPException(404,'研究不属于当前任务')
            item=s.get(ResearchCase,cid)
            packet=s.get(SurveyPacket,cid)
            if s.get(ShortScriptBrief,cid) and mode!='evidence':
                return render('short_scripts.html',case_id=cid,**context)
            return render('research_case.html',case_id=cid,watch=s.get(Watch,data['watch']) if data['watch'] else None,
                event_key=item.event_key,plan=json.loads(packet.data) if packet else None,**context)
        if stage=='creation' and data['scripts']:
            chosen=script or data['scripts'][-1]['id']
            if chosen not in {p['id'] for p in data['scripts']}:raise HTTPException(404,'稿件不属于当前任务')
            from ..video.workbench import script_detail
            doc=task_call(script_detail,s,chosen)
            if mode=='video':
                from ..video.templates import catalog,scene_plan
                from ..video.production import VideoOptions
                return render('video_setup.html',script=doc,previous=None,
                    scenes=scene_plan(doc,VideoOptions().model_dump()) if doc.get('document') else [],templates=catalog(),**context)
            return render('script_editor.html',script=doc,**context)
        if stage=='works' and video:
            if video not in {v['id'] for v in data['videos']}:raise HTTPException(404,'视频不属于当前任务')
            from ..video.production import production_detail
            return render('video_detail.html',video=task_call(production_detail,engine,video),**context)
        return render('workbench_stage.html',**context)
