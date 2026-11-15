"""Bounded PostgreSQL reads and explicit, atomic selection of attention issues."""
from datetime import datetime, timedelta, timezone
import json
import re

from sqlalchemy import case, or_
from sqlmodel import Session, select

from .models import (AttentionEventMatch, AttentionInbox, AttentionItem, AttentionMeta,
                     AttentionSelection, AttentionTopic, AttentionWatchMatch)
from ..storage.articles import lock_news
from ..storage.models import utc_now_iso
from ..desk.models import Watch
from ..desk.focus_lifecycle import require_active, visible_watch
from ..selection.service import _card as event_card, _url
from ..intake.official_models import OfficialEvent
from ..tasks.control import TaskConflict

PERIODS = {'24h':24, '72h':72, '7d':168, 'all':None}


def parsed(value, fallback):
    try:
        result = json.loads(value)
        return result if isinstance(result, type(fallback)) else fallback
    except (ValueError, TypeError): return fallback


def checked(q='', period='72h', order='attention', watch=''):
    if not isinstance(q, str) or len(q)>200: raise ValueError('关键词最多200字符')
    if period not in PERIODS: raise ValueError('时间范围无效')
    if order not in ('attention', 'rising', 'recent'): raise ValueError('排序方式无效')
    if watch and not re.fullmatch(r'w_[0-9a-f]{32}', watch): raise ValueError('专题编号无效')
    return dict(q=q.strip(),period=period,order=order,watch=watch)


def _key(key):
    if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', key):
        raise ValueError('关注议题编号无效')
    return key


def card(row, selected_task=''):
    summary = parsed(row.summary_json,{})
    observed=summary.get('last_observed_at') or summary.get('observed_at') or row.last_seen
    try:
        stamp=datetime.fromisoformat(observed.replace('Z','+00:00'))
        if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
        stale=datetime.now(timezone.utc)-stamp>timedelta(hours=6)
    except (ValueError,TypeError,AttributeError):stale=True
    if stale:
        # A paused collector must not leave yesterday's positive trend looking
        # current forever. Keep historical samples; only current display ages.
        summary.update(stale=True,attention_score=None,rise_score=None,platform_count=0,
            baseline_state='stale',platforms=[dict(p,stale=True,counts_for_attention=False,
                observation_state='stale') for p in summary.get('platforms',[]) if isinstance(p,dict)],
            notes=[*summary.get('notes',[]),'最近本地观察已超过6小时，当前关注与升位状态未知。'])
    return dict(key=row.key,title=row.title,platform_count=row.platform_count,
        attention_score=row.attention_score,rise_score=row.rise_score,
        baseline_state=row.baseline_state,last_seen=row.last_seen,first_seen=row.first_seen,
        item_count=row.item_count,summary=summary,selected_task=selected_task,
        match_checked_at=row.match_checked_at,match_error=row.match_error) | (
            dict(platform_count=0,attention_score=None,rise_score=None,baseline_state='stale') if stale else {})


def _selected(s, keys):
    if not keys: return {}
    rows=s.exec(select(AttentionSelection.topic,AttentionSelection.task)
        .where(AttentionSelection.topic.in_(keys)).order_by(AttentionSelection.created,AttentionSelection.task)).all()
    result={}
    for key,task in rows: result.setdefault(key,task)
    return result


def list_topics(s, filters, offset=0, limit=30):
    filters=checked(**filters)
    if isinstance(offset,bool) or not isinstance(offset,int) or not 0<=offset<=100000: raise ValueError('分页位置无效')
    if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=100: raise ValueError('每页数量无效')
    query=select(AttentionTopic)
    fresh_since=(datetime.now(timezone.utc)-timedelta(hours=6)).isoformat(timespec='seconds').replace('+00:00','Z')
    if filters['q']:
        query=query.where(AttentionTopic.title.contains(filters['q'],autoescape=True))
    hours=PERIODS[filters['period']]
    if hours:
        since=(datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat(timespec='seconds').replace('+00:00','Z')
        query=query.where(AttentionTopic.last_seen>=since)
    if filters['watch']:
        watch=require_active(s,filters['watch'])
        query=query.where(select(AttentionWatchMatch.key).where(AttentionWatchMatch.topic==AttentionTopic.key,
            AttentionWatchMatch.watch==watch.key,AttentionWatchMatch.scope_version==watch.version)
            .correlate(AttentionTopic).exists())
    if filters['order']=='rising':
        # A first observation or a missing source is not a positive trend.
        query=query.where(AttentionTopic.rise_score>0,AttentionTopic.last_seen>=fresh_since).order_by(AttentionTopic.rise_score.desc())
    elif filters['order']=='attention':
        query=query.order_by(case((AttentionTopic.last_seen>=fresh_since,AttentionTopic.platform_count),else_=0).desc(),
            case((AttentionTopic.last_seen>=fresh_since,AttentionTopic.attention_score),else_=None).desc().nulls_last())
    rows=s.exec(query.order_by(AttentionTopic.last_seen.desc(),AttentionTopic.key)
        .offset(offset).limit(limit+1).execution_options(n2d_query_name='attention.topics.page')).all()
    more=len(rows)>limit; rows=rows[:limit]; selected=_selected(s,[r.key for r in rows])
    return dict(items=[card(r,selected.get(r.key,'')) for r in rows],total=None,has_more=more,
        offset=offset,limit=limit,filters=filters,pagination=dict(page=offset//limit+1,limit=limit),
        note='本站观测到的平台榜单位置与覆盖，不等于播放量、搜索量或事件真实性。')


def detail(s,key):
    row=s.get(AttentionTopic,_key(key))
    if not row: raise KeyError('尚未生成该关注议题，请检查正式采集和后台处理状态')
    members=s.exec(select(AttentionItem).where(AttentionItem.topic==key)
        .order_by(AttentionItem.last_seen.desc(),AttentionItem.key).limit(31)).all()
    links=s.exec(select(AttentionEventMatch).where(AttentionEventMatch.topic==key)
        .order_by(AttentionEventMatch.updated_at.desc(),AttentionEventMatch.score.desc(),AttentionEventMatch.key)
        .limit(30)).all()
    ids=[x.event_id for x in links]
    events={e.event_id:e for e in s.exec(select(OfficialEvent).where(OfficialEvent.event_id.in_(ids))).all()} if ids else {}
    watches=s.exec(select(AttentionWatchMatch,Watch).join(Watch,Watch.key==AttentionWatchMatch.watch)
        .where(AttentionWatchMatch.topic==key,AttentionWatchMatch.scope_version==Watch.version,visible_watch())
        .order_by(Watch.name,Watch.key).limit(30)).all()
    return dict(topic=card(row,_selected(s,[key]).get(key,'')),
        items=[dict(key=i.key,source_id=i.source_id,platform=i.platform,source_kind=i.source_kind,
            audience_scope=i.audience_scope,title=i.title,url=_url(i.url),excerpt=i.excerpt,
            list_position=i.list_position,first_seen=i.first_seen,last_seen=i.last_seen,
            grouping=parsed(i.grouping_json,{}),snapshot_ref=parsed(i.snapshot_ref_json,{})) for i in members[:30]],
        items_limited=len(members)>30,
        events=[dict(key=i.key,event_id=i.event_id,status=i.status,score=i.score,method=i.method,
            reasons=parsed(i.reasons_json,[]),current=i.generation==row.generation,
            event=event_card(events[i.event_id]) if i.event_id in events else dict(event_id=i.event_id,title='来源记录暂不可用')) for i in links],
        watches=[dict(key=w.key,name=w.name,scope_version=m.scope_version,reasons=parsed(m.reasons_json,[]),
            mode='keyword_recall') for m,w in watches],
        series=parsed(row.chart_json,[]),notes=parsed(row.summary_json,{}).get('notes',[]),
        note='议题为规则归并；事件关联为候选线索，专题关联为关键词召回，均须研究核查。')


def set_match(s,key,status):
    if status not in ('confirmed','rejected','candidate'): raise ValueError('关联状态无效')
    link=s.get(AttentionEventMatch,_key(key))
    if not link: raise KeyError('候选关联不存在')
    topic=link.topic
    lock_news(s,'attention:'+topic)
    link=s.get(AttentionEventMatch,key)
    if not link: raise KeyError('候选关联已更新，请刷新')
    link.status=status;link.updated_at=utc_now_iso();s.add(link)
    from ..desk.service import enqueue_graph
    enqueue_graph(s,'attention/'+topic);s.commit()
    return dict(ok=True,status=status)


def status(s,worker):
    jobs=s.exec(select(AttentionInbox.key,AttentionInbox.state,AttentionInbox.error,
        AttentionInbox.finished_at).where(AttentionInbox.state!='done')
        .order_by(AttentionInbox.created,AttentionInbox.key).limit(201)).all()
    completed=s.exec(select(AttentionInbox.finished_at).where(AttentionInbox.state=='done')
        .order_by(AttentionInbox.finished_at.desc()).limit(1)).first() or ''
    meta={r.key:r.value for r in s.exec(select(AttentionMeta).where(
        AttentionMeta.key.in_(['backfill_cursor','backfill_error']))).all()}
    error=worker.error or next((r[2] for r in jobs if r[2]),'')
    return dict(enabled=worker.enabled,state='disabled' if not worker.enabled else 'error' if error else worker.state,
        running=bool(worker.thread and worker.thread.is_alive()),
        backlog=min(len(jobs),200),backlog_limited=len(jobs)>200,
        error=error,last_processed=worker.last_processed or completed,
        backfill_cursor=meta.get('backfill_cursor','0'),backfill_error=parsed(meta.get('backfill_error',''),[]),
        note='后台处理正式采集的快照；测试采集不会发布关注议题。')


def select_topic(engine,topic_key,request_id,title='',angle=''):
    _key(topic_key)
    if not isinstance(request_id,str) or not 8<=len(request_id)<=100: raise ValueError('创建编号须为8–100字符')
    if not isinstance(title,str) or len(title)>250 or not isinstance(angle,str) or len(angle)>1500: raise ValueError('选题标题或角度过长')
    from ..research import service as research
    from ..research.models import ResearchCase
    from ..workbench import service as work
    from ..workbench.models import ContentTask
    from ..topics.models import TopicWorkspace,TopicSource
    key='topic_'+research.digest(['attention-select',request_id])[:24]
    fingerprint=research.digest(dict(topic=topic_key,title=title,angle=angle))
    with Session(engine) as s:
        lock_news(s,'content:'+key)
        old=s.get(AttentionSelection,key)
        if old:
            if old.request_hash!=fingerprint: raise TaskConflict('创建编号已用于其他议题或角度')
            return dict(key=key,url='/workbench/'+key+'/selection',topic=old.topic)
        if s.get(ContentTask,key) or s.get(TopicWorkspace,key): raise TaskConflict('创建编号已被使用')
        snapshot=detail(s,topic_key); selected_title=(title.strip() or snapshot['topic']['title'])[:250]
        case_id='topiccase_'+research.digest(key)[:24]
        workspace=TopicWorkspace(workspace_id=key,request_hash=fingerprint,case_id=case_id,
            title=selected_title,angle=angle,scope_json=research.dump(work.topics.Scope().checked()))
        s.add(ResearchCase(case_id=case_id,title=selected_title,goal='内容任务中的选题讨论',
            strategy_json=research.dump(dict(purpose='topic_selection',max_minutes=10,max_searches=0,max_reads=30)),cutoff=utc_now_iso()))
        s.add(workspace);s.add(ContentTask(key=key,origin_hash=fingerprint))
        # Freeze each original lead separately so research can resolve each URL;
        # no invented Article, local Event or fulltext is created on selection.
        sources=[]
        for item in snapshot['items']:
            document=dict(provider='newsnow',title=item['title'],content=item['excerpt'],url=item['url'],
                canonical_url=item['url'],published_at='',source_domain='',language='',country='',revision=None,
                source_kind='newsnow_metadata',material_kind='newsnow_discovery',content_kind='source_excerpt',
                attention_topic=topic_key,newsnow=dict(source_id=item['source_id'],platform=item['platform'],
                    archive_ref=item['snapshot_ref'],observed_at=item['last_seen'],grouping=item['grouping']),requires_original_text=True)
            sid='topic_source_'+research.digest([key,'attention',item['key']])[:24]
            s.add(TopicSource(source_id=sid,workspace_id=key,article_key='newsnow_attention/'+item['key'],
                document_json=research.dump(document),content_hash=research.digest(document)))
            sources.append(sid)
        if not sources: raise TaskConflict('议题尚无可选线索，请等待后台归并完成')
        # The research contract accepts at most twelve selected leads. Keep
        # extra snapshots available, without freezing an unexecutable handoff.
        workspace.selected_json=research.dump(sources[:12]);s.add(workspace)
        snapshot['selection']=dict(saved_sources=len(sources),selected_sources=min(12,len(sources)),
            note='默认选入至多12条线索，其余已保存，可在工作台调整选择。')
        work.bind(s,key,'attention_topic',topic_key)
        s.add(AttentionSelection(task=key,topic=topic_key,request_hash=fingerprint,snapshot_json=research.dump(snapshot)))
        s.commit()
    return dict(key=key,url='/workbench/'+key+'/selection',topic=topic_key)
