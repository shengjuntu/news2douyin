import json
from pathlib import Path
from types import SimpleNamespace
from pydantic import BaseModel, Field, ConfigDict
from typing import Literal
from sqlmodel import Session, select
from sqlalchemy import func
from ..topics import service as topics
from ..topics.models import TopicWorkspace, TopicHandoff
from ..research import service as research
from ..research.models import ResearchCase, ResearchReport, ResearchClaim, ResearchSource, ShortScriptBrief
from ..storage.models import Article, ArticleEventLink, Event, ScriptPackage, ScriptState, ScriptExport, VideoProduction, TaskRecord, utc_now_iso
from ..storage.articles import lock_news
from ..desk.models import Watch, WatchEvent
from ..extraction.schema import stable_id
from ..tasks.control import TaskConflict
from .models import ContentTask, ContentBinding, ContentRequest, ContentTaskTrash

PHASES={'selection':'选题','research':'研究','creation':'创作','works':'作品'}


class Start(BaseModel):
    model_config=ConfigDict(extra='forbid')
    request_id: str=Field(min_length=8,max_length=100)
    title: str=Field(default='',max_length=250)
    angle: str=Field(default='',max_length=1500)
    event: str=Field(default='',max_length=160)
    watch: str=Field(default='',max_length=80)
    digest_day: str=Field(default='',max_length=10)
    progress: str=Field(default='',max_length=160)
    digest_token: str=Field(default='',pattern=r'^(?:[a-f0-9]{64})?$')
    articles: list[str]=Field(default_factory=list,max_length=12)
    date_from: str=''
    date_to: str=''


def bind(s, task, kind, ref):
    key=stable_id(task,kind,ref)
    if not s.get(ContentBinding,key):
        s.add(ContentBinding(key=key,task=task,kind=kind,ref=ref))


def ensure(s,w):
    row=s.get(ContentTask,w.workspace_id)
    if not row:
        row=ContentTask(key=w.workspace_id,event=json.loads(w.scope_json).get('event_key',''),created=w.created_at,updated=w.updated_at)
        if s.exec(select(TopicHandoff.handoff_id).where(TopicHandoff.workspace_id==w.workspace_id).limit(1)).first():
            row.phase='research'
        s.add(row)
    return row


def initialize(engine):
    with Session(engine) as s:
        lock_news(s, 'maintenance:workbench')
        for w in s.exec(select(TopicWorkspace)):
            ensure(s,w)
        sync(s)
        s.commit()


def sync(s, task=''):
    query=select(TopicHandoff)
    if task:query=query.where(TopicHandoff.workspace_id==task)
    for h in s.exec(query):
        bind(s,h.workspace_id,'case',h.case_id)
        brief=s.get(ShortScriptBrief,h.case_id)
        if brief and brief.package_key:
            fresh=not s.get(ContentBinding,stable_id(h.workspace_id,'script',brief.package_key))
            bind(s,h.workspace_id,'script',brief.package_key)
            task=s.get(ContentTask,h.workspace_id)
            if fresh and task:
                task.phase='creation';task.version+=1;task.updated=utc_now_iso();s.add(task)


def require(s,key):
    w=topics.require(s,key)
    task=s.get(ContentTask,key)
    if not task:raise KeyError('内容任务尚未初始化，请完成数据库升级后重试')
    return task,w


def visible_task(key):
    return ~select(ContentTaskTrash.task).where(
        ContentTaskTrash.task == key, ContentTaskTrash.deleted_at != ''
    ).correlate_except(ContentTaskTrash).exists()


def is_trashed(s, key):
    state = s.get(ContentTaskTrash, key)
    return bool(state and state.deleted_at)


def require_visible(s, key):
    if is_trashed(s, key):
        raise TaskConflict('选题已移入回收站，请先恢复')


def set_trashed(engine, key, version, *, trashed):
    with Session(engine) as s:
        lock_news(s, 'content:' + key)
        # Serialize with topic edits and durable handoff submission.
        research.locked(s)
        task, _ = require(s, key)
        state = s.get(ContentTaskTrash, key) or ContentTaskTrash(task=key)
        if state.version != version:
            raise TaskConflict('选题的回收状态已更新，请刷新后重试')
        if bool(state.deleted_at) == trashed:
            return dict(key=key, trashed=trashed)
        if trashed:
            pending_handoff = s.exec(select(TopicHandoff.handoff_id).where(
                TopicHandoff.workspace_id == key, TopicHandoff.run_id == ''
            ).limit(1)).first()
            if task.phase != 'selection' or pending_handoff:
                raise TaskConflict('只能删除处于选题阶段且没有待提交研究的任务')
        state.deleted_at = utc_now_iso() if trashed else ''
        state.version += 1
        s.add(state)
        s.commit()
        return dict(key=key, trashed=trashed)


def start(engine,payload):
    value=Start.model_validate(payload)
    scope=topics.Scope(period='custom' if value.date_from or value.date_to else 'all',
                       date_from=value.date_from,date_to=value.date_to,event_key=value.event).checked()
    key='topic_'+research.digest(['create',value.request_id])[:24]
    fields = value.model_dump()
    if not value.digest_token:
        fields.pop('digest_token')  # Preserve creation request hashes from earlier releases.
    from_digest = bool(value.digest_day or value.progress or value.digest_token)
    if from_digest:
        if not value.digest_day or not value.progress or value.event or value.watch or value.articles or value.date_from or value.date_to:
            raise ValueError('请单独选择一期日报中的进展')
    else:
        # Existing creation requests keep their pre-digest idempotency fingerprint.
        fields.pop('digest_day')
        fields.pop('progress')
    fingerprint=research.digest(fields)
    with Session(engine,expire_on_commit=False) as s:
        lock_news(s, 'content:' + key)
        old=s.get(ContentTask,key)
        if old:
            if old.origin_hash!=fingerprint:raise TaskConflict('创建编号已经用于不同内容')
            require_visible(s,key)
            return detail(s,key)
        if s.get(TopicWorkspace,key):raise TaskConflict('创建编号已存在，请使用新的编号')
        event=s.exec(select(Event).where(Event.event_key==value.event)).first() if value.event else None
        if value.watch:
            from ..desk.focus_lifecycle import require_active
            watch=require_active(s,value.watch)
        else:watch=None
        if value.event and not event:raise KeyError('事件不存在')
        if value.watch and not watch:raise KeyError('焦点不存在')
        if event and watch:
            member=s.get(WatchEvent,(watch.key,event.event_key))
            if not member or member.choice=='exclude':raise ValueError('事件不属于所选焦点')
        card = None
        if from_digest:
            from ..digest.handoff import selected_card
            card = selected_card(s, value.digest_day, value.progress, value.digest_token)
        title=value.title.strip() or (card['title'][:250] if card else event.event_title if event else watch.name+' · 新选题' if watch else '未命名选题')
        cid='topiccase_'+research.digest(key)[:24]
        w=TopicWorkspace(workspace_id=key,request_hash=fingerprint,case_id=cid,title=title,
            angle=value.angle,scope_json=research.dump(scope))
        s.add(ResearchCase(case_id=cid,title=title,goal='内容任务中的选题讨论',
            strategy_json=research.dump(dict(purpose='topic_selection',max_minutes=10,max_searches=0,max_reads=30)),cutoff=utc_now_iso()))
        s.add(w);s.flush()
        selected=list(dict.fromkeys(value.articles))
        if not selected and (event or watch):
            query=select(Article).join(ArticleEventLink,ArticleEventLink.article_key==Article.article_key)
            if event:query=query.where(ArticleEventLink.event_key==event.event_key)
            else:
                query=query.join(WatchEvent,WatchEvent.event==ArticleEventLink.event_key).where(WatchEvent.watch==watch.key,WatchEvent.choice!='exclude')
            from ..search.dates import date_range
            from ..search.service import _time_predicate
            begin,end=date_range(scope['period'],scope['date_from'],scope['date_to'],scope['timezone'])
            if begin or end:query=query.where(_time_predicate(Article.published_at,begin,end))
            selected=[a.article_key for a in s.exec(query.order_by(Article.published_at.desc(),Article.id.desc()).distinct().limit(6))]
        if card:
            from ..digest.handoff import freeze_sources
            source_ids = freeze_sources(s, key, value.digest_day, card)
            bind(s, key, 'radar_daily', value.digest_day + '/' + value.progress)
        else:
            source_ids = [topics.freeze(s,key,a).source_id for a in selected]
        w.selected_json=research.dump(source_ids)
        s.add(w)
        s.add(ContentTask(key=key,origin_hash=fingerprint,event=value.event,watch=value.watch))
        s.commit()
        return detail(s,key)


def detail(s,key):
    task,w=require(s,key)
    bindings=s.exec(select(ContentBinding).where(ContentBinding.task==key).order_by(ContentBinding.created,ContentBinding.key)).all()
    cases=[];scripts=[]
    for b in bindings:
        if b.kind=='case':
            case=s.get(ResearchCase,b.ref)
            if case:cases.append(dict(id=b.ref,title=case.title,cutoff=case.cutoff,
                short=bool(s.get(ShortScriptBrief,b.ref))))
        elif b.kind=='script':
            from ..video.workbench import list_scripts, script_detail
            package=s.exec(select(ScriptPackage).where(ScriptPackage.package_key==b.ref)).first()
            if package:
                state=s.get(ScriptState,b.ref)
                scripts.append(dict(id=b.ref,title=json.loads(package.script_json or '{}').get('document',{}).get('title',w.title),
                    status=state.status if state else 'legacy',revision=state.current_revision if state else 0))
    case_ids=[c['id'] for c in cases]
    reports=s.exec(select(ResearchReport).where(ResearchReport.case_id.in_(case_ids)).order_by(ResearchReport.created_at.desc(),ResearchReport.version.desc())).all() if case_ids else []
    keys=[p['id'] for p in scripts]
    exports=s.exec(select(ScriptExport).where(ScriptExport.package_key.in_(keys)).order_by(ScriptExport.created_at.desc())).all() if keys else []
    videos=s.exec(select(VideoProduction,TaskRecord).join(TaskRecord,TaskRecord.task_id==VideoProduction.task_id)
        .where(VideoProduction.package_key.in_(keys)).order_by(VideoProduction.created_at.desc())).all() if keys else []
    watch=s.get(Watch,task.watch) if task.watch else None
    from ..desk.focus_lifecycle import is_deleted
    watch_deleted=bool(watch and is_deleted(s,watch.key))
    selection=topics.detail(s,key)
    materials=dict(selection['materials'])
    materials['research_readable']=s.exec(select(func.count()).select_from(ResearchSource).where(
        ResearchSource.case_id.in_(case_ids), ResearchSource.kind.notin_(('event_metadata','search_snippet')),
        ResearchSource.paragraphs_json.notin_(('', '[]')))).one() if case_ids else 0
    if reports:
        materials.update(stage='report',label='已有研究报告',next_step='选择报告形成可编辑初稿，核对引用后继续创作。')
    elif materials['research_readable']:
        materials.update(stage='readable',label='研究中已读取原文',next_step='在研究页核对已读来源与判断，整理研究报告。')
    trash=s.get(ContentTaskTrash,key)
    result=dict(key=key,title=w.title,angle=w.angle,phase=task.phase,version=task.version,topic_revision=w.revision,
        trash_version=trash.version if trash else 0,
        event=task.event,watch=task.watch,watch_name=watch.name if watch else '',watch_deleted=watch_deleted,
        sources=selection['sources'],materials=materials,
        pending_handoffs=[t for t in selection['tasks'] if not t['started']],
        updated=max(task.updated,w.updated_at),cases=cases,scripts=scripts,
        reports=[dict(id=r.report_id,case=r.case_id,title=r.title,version=r.version,cutoff=r.cutoff,completeness=r.completeness) for r in reports],
        exports=[dict(id=e.export_key,package=e.package_key,revision=e.revision,
            url=f'/api/scripts/{e.package_key}/exports/{e.export_key}/download') for e in exports],
        videos=[dict(id=v.task_id,status=t.status,package=v.package_key) for v,t in videos])
    return result


def move(engine,key,phase,version):
    if phase not in PHASES:raise ValueError('阶段无效')
    with Session(engine) as s:
        lock_news(s, 'content:' + key)
        task,w=require(s,key)
        require_visible(s,key)
        if task.version!=version:raise TaskConflict('任务已更新，请刷新')
        if phase=='works':
            data=detail(s,key)
            if not data['reports'] and not data['exports'] and not any(v['status']=='succeeded' for v in data['videos']):
                raise ValueError('尚无可交付的研究报告、导出稿件或视频')
        task.phase=phase;task.version+=1;task.updated=utc_now_iso();s.add(task);s.commit()
        return detail(s,key)


def from_report(engine,storage,key,report_id,request_id,version):
    ident=stable_id(key,request_id)
    fingerprint=stable_id(report_id,version)
    with Session(engine,expire_on_commit=False) as s:
        lock_news(s, 'content:' + key)
        task,w=require(s,key)
        require_visible(s,key)
        receipt=s.get(ContentRequest,ident)
        if receipt:
            if receipt.fingerprint!=fingerprint:raise TaskConflict('请求编号已用于其他内容')
            return json.loads(receipt.result)
        if task.version!=version:raise TaskConflict('任务已更新，请刷新')
        sync(s,key);s.flush()
        report=s.get(ResearchReport,report_id)
        if not report or not s.get(ContentBinding,stable_id(key,'case',report.case_id)):
            raise ValueError('研究报告不属于当前任务')
        case=s.get(ResearchCase,report.case_id)
        ordered=list(dict.fromkeys(cid for section in json.loads(report.sections_json) for cid in section.get('claim_ids',[])))
        segments=[];omitted=[]
        for cid in ordered:
            claim=s.get(ResearchClaim,cid)
            if not claim or claim.case_id!=case.case_id:raise ValueError('报告的判断引用不完整')
            if claim.kind not in ('fact','attributed','analysis') or not any(e.get('relation')=='supports' for e in json.loads(claim.evidence_json)):
                omitted.append(cid);continue
            segments.append(dict(kind='analysis' if claim.kind=='analysis' else 'fact',text=claim.text,claim_ids=[cid]))
        if not segments:raise ValueError('报告尚无带支持证据的判断，请先补充研究')
        if len(segments)>12:omitted.extend(ordered[12:]);segments=segments[:12]
        from ..editorial.short_scripts import ShortDraft, _freeze
        from ..video import workbench as wb
        draft=ShortDraft(title=w.title,segments=segments,notes='由研究判断形成的待编辑初稿；请调整叙述并核对引用。',
            gaps=json.loads(report.gaps_json)+(['部分判断未带入稿件，请查看原报告。'] if omitted else []))
        opts=dict(output_language='zh-CN',duration_sec=180,find_images=False,angle=w.angle,source_only=True)
        document,editorial,_=_freeze(s,case,SimpleNamespace(options_json=json.dumps(opts)),draft)
        editorial['research']=dict(case_id=case.case_id,report_id=report.report_id,version=report.version,content_hash=report.content_hash)
        editorial['generation']=dict(mode='report_outline',label='研究判断初稿 · 待编辑')
        package_key='pkg_work_'+ident[:24]
        folder=Path(storage)/'packages'/package_key
        package=ScriptPackage(package_key=package_key,event_key=task.event or case.event_key,
            profile_name='content_workbench',output_dir=str(folder),script_text=document['script_text'])
        wb.initialize_script(s,package,editorial,document=document)
        bind(s,key,'script',package_key)
        task.phase='creation';task.version+=1;task.updated=utc_now_iso();s.add(task)
        result=dict(package=package_key,url=f'/workbench/{key}/creation?script={package_key}')
        s.add(ContentRequest(key=ident,task=key,fingerprint=fingerprint,result=json.dumps(result)))
        s.commit()
        return result


def related(s,*,event='',watch=''):
    stmt=select(ContentTask,TopicWorkspace).join(TopicWorkspace,TopicWorkspace.workspace_id==ContentTask.key)
    stmt=stmt.where(visible_task(ContentTask.key))
    if event:stmt=stmt.where(ContentTask.event==event)
    if watch:stmt=stmt.where(ContentTask.watch==watch)
    return [dict(key=t.key,title=w.title,phase=PHASES[t.phase]) for t,w in s.exec(stmt.order_by(ContentTask.updated.desc()).limit(20))]


def adopt(engine,kind,ref):
    if kind not in ('case','script'):raise ValueError('不支持的历史成果类型')
    with Session(engine) as s:
        previous=s.exec(select(ContentBinding.task).where(ContentBinding.kind==kind,ContentBinding.ref==ref)).first()
        if previous:return {'key':previous}
        if kind=='case':
            row=s.get(ResearchCase,ref)
            if not row:raise KeyError('研究不存在')
            title,event=row.title,row.event_key
        else:
            row=s.exec(select(ScriptPackage).where(ScriptPackage.package_key==ref)).first()
            if not row:raise KeyError('稿件不存在')
            title=json.loads(row.script_json or '{}').get('document',{}).get('title') or '历史稿件'
            event=row.event_key
    data=start(engine,dict(request_id='adopt-'+stable_id(kind,ref),title=title,angle='由历史成果继续；原研究和稿件保留原证据版本。'))
    with Session(engine) as s:
        lock_news(s, 'content:' + data['key'])
        task,w=require(s,data['key'])
        require_visible(s,task.key)
        bind(s,task.key,kind,ref)
        task.event=event
        task.phase='research' if kind=='case' else 'creation'
        if kind=='case':
            from ..desk.models import WatchReview
            ticket=s.exec(select(WatchReview).where(WatchReview.case_key==ref)).first()
            if ticket:task.watch=ticket.watch
        s.add(task);s.commit()
    return {'key':data['key']}
