"""Human curation. Every mutation is serialized, versioned and audited."""
from uuid import uuid4
from sqlalchemy import func, or_
from sqlmodel import select

from ..storage.models import (Article, ArticleVersion, ArticleEventLink, Event, EventState,
    EventMoment, EventActivity, EventAssignment, EventRelation, EventResearch, utc_now_iso)
from ..storage.articles import lock_news, latest_version, record_version
from ..storage.utils import loads, dumps
from ..tasks.control import TaskConflict
from .service import refresh_event, evidence_counts


def require_event(session, key, *, active=False):
    event = session.exec(select(Event).where(Event.event_key == key)).first()
    if not event: raise KeyError('事件专题不存在')
    state = session.get(EventState, key)
    if active and state and state.merged_into:
        raise TaskConflict('此专题已并入 ' + state.merged_into + '，请打开目标专题')
    return event


def canonical_event(session, key):
    visited = set()
    while key not in visited:
        visited.add(key)
        state = session.get(EventState, key)
        if not state or not state.merged_into: return key
        key = state.merged_into
    raise TaskConflict('专题合并关系存在循环，请检查历史数据')


def state_for(session, key):
    state = session.get(EventState, key)
    if state is None:
        state = EventState(event_key=key)
        session.add(state)
        session.flush()
    return state


def lock_event(session, key, expected):
    lock_news(session)
    require_event(session, key, active=True)
    state = state_for(session, key)
    if state.version != expected:
        raise TaskConflict('专题已更新，请刷新后再操作；尚未保存的内容请先复制保留')
    return state


def audit(session, key, action, payload, operation_key=None):
    session.add(EventActivity(event_key=key, operation_key=operation_key or uuid4().hex,
                             action=action, payload_json=dumps(payload)))


def touch(session, state):
    state.version += 1
    state.updated_at = utc_now_iso()
    session.add(state)


def finish(session, key, state):
    refresh_event(session, key, touch_version=False)
    touch(session, state)
    session.commit()
    return workspace(session, key)


def create_event(session, data):
    lock_news(session)
    key = 'evt_' + uuid4().hex[:20]
    event = Event(event_key=key, event_title=data['title'], summary=data.get('summary',''), topic=data.get('topic',''))
    session.add(event)
    session.add(EventState(event_key=key, title_override=event.event_title, summary_override=event.summary))
    audit(session, key, 'create', {'event': event.model_dump()})
    session.commit()
    return workspace(session, key)


def edit_event(session, key, data):
    state = lock_event(session, key, data['expected_version'])
    event = require_event(session, key)
    before = dict(title=event.event_title, summary=event.summary, topic=event.topic)
    state.title_override, state.summary_override = data['title'], data['summary']
    event.event_title, event.summary, event.topic = data['title'], data['summary'], data['topic']
    session.add(event)
    audit(session, key, 'edit', {'before':before, 'after':{k:data[k] for k in before}})
    return finish(session, key, state)


def linked_keys(session, key):
    return set(session.exec(select(ArticleEventLink.article_key).where(ArticleEventLink.event_key == key)))


def ensure_version(session, article):
    version = latest_version(session, article.article_key)
    if version is None:
        record_version(session, article, origin='legacy_baseline')
        session.flush()
        version = latest_version(session, article.article_key)
    return version


def add_sources(session, key, data):
    state = lock_event(session, key, data['expected_version'])
    added = _add_sources(session, key, state, data['article_keys'], data.get('note',''))
    if not added:
        session.commit()
        return workspace(session, key)
    audit(session, key, 'add_sources', {'articles':added,'note':data.get('note','')})
    return finish(session, key, state)


def _add_sources(session, key, state, keys, note='', origin='manual'):
    existing = linked_keys(session, key)
    notes, added = loads(state.source_notes_json, {}), []
    for article_key in dict.fromkeys(keys):
        article = session.exec(select(Article).where(Article.article_key == article_key)).first()
        if not article: raise KeyError('新闻不存在：' + article_key)
        if article_key in existing: continue
        version = ensure_version(session, article)
        session.add(ArticleEventLink(article_key=article_key, event_key=key,
                                   relation_type='duplicate' if article.is_duplicate else 'primary'))
        notes[article_key] = dict(origin=origin, note=note, revision=version.revision, added_at=utc_now_iso())
        assignment = session.get(EventAssignment, article_key)
        if not assignment:
            session.add(EventAssignment(article_key=article_key, event_key=key, reason=origin))
        added.append(article_key)
        existing.add(article_key)
    state.source_notes_json = dumps(notes)
    session.add(state)
    return added


def active_moments(session, key):
    return list(session.exec(select(EventMoment).where(EventMoment.event_key == key, EventMoment.deleted == False)))


def remove_source(session, key, article_key, expected):
    state = lock_event(session, key, expected)
    references = [m.title for m in active_moments(session,key) if article_key in {s['article_key'] for s in loads(m.sources_json,[])}]
    if references:
        raise TaskConflict('这篇报道正被脉络节点引用，请先修改或删除节点：' + '、'.join(references[:5]))
    links = session.exec(select(ArticleEventLink).where(ArticleEventLink.event_key == key, ArticleEventLink.article_key == article_key)).all()
    if not links: raise KeyError('该报道不在此专题中')
    for link in links: session.delete(link)
    _repair_assignment(session, article_key, key)
    notes = loads(state.source_notes_json,{})
    old_note = notes.pop(article_key,None)
    state.source_notes_json=dumps(notes)
    audit(session,key,'remove_source',{'article_key':article_key,'origin':old_note})
    return finish(session,key,state)


def _repair_assignment(session, article_key, old_key, preferred=None):
    session.flush()
    assignment = session.get(EventAssignment, article_key)
    if assignment and assignment.event_key == old_key:
        target = preferred or session.exec(select(ArticleEventLink.event_key).where(ArticleEventLink.article_key == article_key).order_by(ArticleEventLink.id)).first()
        if target:
            assignment.event_key, assignment.reason = target, 'manual'
            session.add(assignment)
        else: session.delete(assignment)


def moment_data(row):
    result = row.model_dump(exclude={'sources_json'})
    result['sources'] = loads(row.sources_json,[])
    return result


def save_moment(session, key, data, moment_key=None):
    state = lock_event(session,key,data['expected_version'])
    current = session.get(EventMoment,moment_key) if moment_key else None
    if moment_key and (not current or current.event_key != key or current.deleted):
        raise KeyError('脉络节点不存在')
    allowed, evidence = linked_keys(session,key), []
    for source in data['sources']:
        if source['article_key'] not in allowed: raise ValueError('证据报道必须先加入当前专题')
        version = session.exec(select(ArticleVersion).where(ArticleVersion.article_key == source['article_key'],ArticleVersion.revision == source['revision'])).first()
        if not version: raise ValueError('所选文章版本不存在，请重新选择来源')
        if version.content_hash != source['content_hash']: raise TaskConflict('来源版本指纹不匹配，请重新加载证据')
        document = loads(version.payload_json,{})
        excerpt = source['excerpt'].strip()
        if not excerpt or not (excerpt in document.get('content','') or excerpt in document.get('title','')):
            raise ValueError('证据摘录必须是该版本原文中的连续片段：'+document.get('title',''))
        evidence.append(dict(source,excerpt=excerpt,title=document.get('title',''),url=document.get('url',''),
                             published_at=document.get('published_at',''),source_domain=document.get('source_domain','')))
    before = moment_data(current) if current else None
    row = current or EventMoment(moment_key='mom_'+uuid4().hex[:20],event_key=key,title=data['title'])
    for field in ('title','description','time_kind','date_start','date_end','certainty','time_note','reviewed'):
        setattr(row,field,data[field])
    row.sources_json, row.updated_at = dumps(evidence), utc_now_iso()
    session.add(row)
    audit(session,key,'edit_moment' if current else 'add_moment',{'before':before,'after':moment_data(row)})
    return finish(session,key,state)


def delete_moment(session,key,moment_key,expected):
    state=lock_event(session,key,expected)
    row=session.get(EventMoment,moment_key)
    if not row or row.event_key != key or row.deleted: raise KeyError('脉络节点不存在')
    audit(session,key,'delete_moment',{'before':moment_data(row)})
    row.deleted=True
    row.updated_at=utc_now_iso()
    session.add(row)
    return finish(session,key,state)


def relate(session,key,data):
    state=lock_event(session,key,data['expected_version'])
    target=data['target_event_key']
    require_event(session,target,active=True)
    if key==target: raise ValueError('不能关联专题自身')
    rows=session.exec(select(EventRelation).where(EventRelation.kind==data['kind'])).all()
    for r in rows:
        a,b=canonical_event(session,r.from_event),canonical_event(session,r.to_event)
        if (a,b)==(key,target) or (data['kind']=='related' and (a,b)==(target,key)):
            raise TaskConflict('这个关联已存在')
    row=EventRelation(relation_key='rel_'+uuid4().hex[:20],from_event=key,to_event=target,kind=data['kind'],note=data['note'])
    session.add(row)
    audit(session,key,'add_relation',row.model_dump())
    touch(session,state_for(session,target))
    return finish(session,key,state)


def unrelate(session,key,relation_key,expected):
    state=lock_event(session,key,expected)
    row=session.get(EventRelation,relation_key)
    if not row: raise KeyError('关联不存在')
    a,b=canonical_event(session,row.from_event),canonical_event(session,row.to_event)
    if key not in {a,b}: raise KeyError('关联不属于此专题')
    other=b if key==a else a
    audit(session,key,'remove_relation',row.model_dump())
    session.delete(row)
    if other != key: touch(session,state_for(session,other))
    return finish(session,key,state)


def merge(session,key,data):
    state=lock_event(session,key,data['expected_version'])
    target=data['target_event_key']
    if key==target: raise ValueError('不能合并到自身')
    require_event(session,target,active=True)
    target_state=state_for(session,target)
    if target_state.version != data['target_expected_version']:
        raise TaskConflict('目标专题已更新，请重新选择后合并')
    keys=linked_keys(session,key)
    origin_notes=loads(state.source_notes_json,{})
    _add_sources(session,target,target_state,keys,'从 '+key+' 合并','merge')
    target_notes=loads(target_state.source_notes_json,{})
    for article_key in keys:
        if article_key in origin_notes:
            target_notes.setdefault(article_key,{}).setdefault('previous_origin',origin_notes[article_key])
    target_state.source_notes_json=dumps(target_notes)
    moments=[]
    for row in active_moments(session,key):
        moments.append(row.moment_key)
        row.event_key=target
        session.add(row)
    for link in session.exec(select(ArticleEventLink).where(ArticleEventLink.event_key==key)).all():
        session.delete(link)
    for article_key in keys: _repair_assignment(session,article_key,key,target)
    state.merged_into=target
    state.source_notes_json='{}'
    payload={'from_event':key,'to_event':target,'articles':sorted(keys),'moments':moments,'source_notes':origin_notes,'note':data['note']}
    operation=uuid4().hex
    audit(session,key,'merge_out',payload,operation)
    audit(session,target,'merge_in',payload,operation)
    refresh_event(session,key,touch_version=False)
    refresh_event(session,target,touch_version=False)
    touch(session,state)
    touch(session,target_state)
    session.commit()
    return {'target_event_key':target,'workspace':workspace(session,target)}


def split(session,key,data):
    state=lock_event(session,key,data['expected_version'])
    chosen=set(data['article_keys'])
    if not chosen or not chosen <= linked_keys(session,key): raise ValueError('拆分报道必须属于当前专题')
    moving=[]
    for node in active_moments(session,key):
        sources={s['article_key'] for s in loads(node.sources_json,[])}
        if sources & chosen:
            if not sources <= chosen:
                raise TaskConflict('节点“'+node.title+'”同时引用两组报道，请先拆分或修改该节点')
            moving.append(node)
    target='evt_'+uuid4().hex[:20]
    origin=require_event(session,key)
    session.add(Event(event_key=target,event_title=data['title'],topic=origin.topic,market_scope=origin.market_scope))
    target_state=EventState(event_key=target,title_override=data['title'])
    session.add(target_state)
    notes=loads(state.source_notes_json,{})
    _add_sources(session,target,target_state,chosen,'从 '+key+' 拆分','split')
    new_notes=loads(target_state.source_notes_json,{})
    for a in chosen:
        if a in notes: new_notes[a]['previous_origin']=notes.pop(a)
    state.source_notes_json=dumps(notes)
    target_state.source_notes_json=dumps(new_notes)
    for row in moving:
        row.event_key=target
        session.add(row)
    for link in session.exec(select(ArticleEventLink).where(ArticleEventLink.event_key==key,ArticleEventLink.article_key.in_(chosen))).all():
        session.delete(link)
    for a in chosen: _repair_assignment(session,a,key,target)
    payload={'from_event':key,'to_event':target,'articles':sorted(chosen),'moments':[m.moment_key for m in moving],'note':data.get('note','')}
    operation=uuid4().hex
    audit(session,key,'split_out',payload,operation)
    audit(session,target,'split_in',payload,operation)
    session.add(EventRelation(relation_key='rel_'+uuid4().hex[:20],from_event=key,to_event=target,kind='related',note='人工拆分：'+data.get('note','')))
    refresh_event(session,key,touch_version=False)
    refresh_event(session,target,touch_version=False)
    touch(session,state)
    touch(session,target_state)
    session.commit()
    return {'target_event_key':target,'workspace':workspace(session,target)}


def workspace(session,key):
    event=require_event(session,key)
    state=session.get(EventState,key)
    latest=select(func.max(ArticleVersion.revision)).where(ArticleVersion.article_key==Article.article_key).correlate(Article).scalar_subquery()
    rows=session.exec(select(Article,ArticleVersion).join(ArticleEventLink,Article.article_key==ArticleEventLink.article_key)
        .outerjoin(ArticleVersion,(ArticleVersion.article_key==Article.article_key)&(ArticleVersion.revision==latest))
        .where(ArticleEventLink.event_key==key).order_by(Article.published_at,Article.id)).unique().all()
    notes=loads(state.source_notes_json,{}) if state else {}
    sources=[]
    for a,v in rows:
        doc=loads(v.payload_json,{}) if v else a.model_dump()
        sources.append(dict(article_key=a.article_key,title=doc.get('title',''),url=doc.get('url',''),source_domain=doc.get('source_domain',''),
            published_at=doc.get('published_at',''),excerpt=doc.get('content','')[:800],revision=v.revision if v else None,
            content_hash=v.content_hash if v else '',is_duplicate=a.is_duplicate,origin=notes.get(a.article_key,{})))
    moments=[moment_data(m) for m in active_moments(session,key)]
    moments.sort(key=lambda m:(not bool(m['date_start']),m['date_start'],m['created_at'],m['moment_key']))
    for moment in moments:
        for source in moment['sources']:
            current=next((a for a in sources if a['article_key']==source['article_key']),None)
            source['newer_revision_available']=bool(current and current['revision'] and current['revision']>source['revision'])
    relations=[]
    for r in session.exec(select(EventRelation).order_by(EventRelation.created_at)):
        a,b=canonical_event(session,r.from_event),canonical_event(session,r.to_event)
        if key not in {a,b} or a==b: continue
        other=b if key==a else a
        row=require_event(session,other)
        relations.append(dict(relation_key=r.relation_key,event_key=other,title=row.event_title,kind=r.kind,note=r.note,direction='out' if key==a else 'in'))
    activity=list(session.exec(select(EventActivity).where(EventActivity.event_key==key).order_by(EventActivity.id.desc()).limit(50)))
    research=list(session.exec(select(EventResearch).where(EventResearch.event_key==key).order_by(EventResearch.created_at.desc()).limit(10)))
    return dict(event_key=key,title=event.event_title,summary=event.summary,topic=event.topic,
        version=state.version if state else 0,merged_into=canonical_event(session,key) if state and state.merged_into else None,
        counts=evidence_counts(session,key),sources=sources,moments=moments,relations=relations,
        activity=[dict(id=a.id,action=a.action,created_at=a.created_at,payload=loads(a.payload_json,{})) for a in activity],
        research=[dict(research_key=r.research_key,status=r.status,query=loads(r.query_json,{}),count=len(loads(r.results_json,[])),created_at=r.created_at,error=r.error_text) for r in research])


def export_evidence(session,key):
    # Keep the exported graph and pinned documents at one curation revision.
    lock_news(session)
    view=workspace(session,key)
    references={(s['article_key'],s['revision']) for s in view['sources'] if s['revision']}
    references.update((s['article_key'],s['revision']) for m in view['moments'] for s in m['sources'])
    snapshots=[]
    research_keys=set()
    for article_key,revision in sorted(references):
        row=session.exec(select(ArticleVersion).where(ArticleVersion.article_key==article_key,ArticleVersion.revision==revision)).first()
        if not row: raise TaskConflict('来源版本缺失，无法导出完整证据包')
        snapshots.append(dict(article_key=article_key,revision=revision,content_hash=row.content_hash,
            origin=row.origin,observed_at=row.observed_at,document=loads(row.payload_json,{})))
        if row.origin=='external_research' and row.run_key: research_keys.add(row.run_key)
    searches=[]
    for research_key in sorted(research_keys):
        row=session.get(EventResearch,research_key)
        if row: searches.append(dict(research_key=research_key,query=loads(row.query_json,{}),created_at=row.created_at,
                                     finished_at=row.finished_at,imported=loads(row.imported_json,{})))
    return dict(schema_version=1,kind='news2douyin.event_evidence',exported_at=utc_now_iso(),
        note='人工整理的证据包；reviewed 仅表示用户已核对，不代表系统保证事实准确。',
        source_versions=snapshots,research_provenance=searches,**view)
