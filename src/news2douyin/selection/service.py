"""Read-only, bounded event discovery; research starts only on explicit selection.

Coverage counts describe documents and domains observed by this installation.
They are not lifetime GDELT totals or proof of independent corroboration.
"""
from datetime import datetime, timedelta, timezone
import json
import re
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import func, or_
from sqlmodel import Session, select

from ..intake.official_models import OfficialEvent, OfficialMention
from ..intake.models import FeedState, IntakeBatch
from ..storage.models import utc_now_iso
from ..storage.articles import lock_news
from ..desk.models import Watch
from ..desk.rules import ALIASES, terms
from ..tasks.control import TaskConflict
from ..workbench.service import visible_task
from .models import SelectionFilter, SelectionOrigin

CAMEO_LABELS = {
    '01':'公开表态', '02':'呼吁', '03':'表达合作意向', '04':'磋商与接触',
    '05':'外交合作', '06':'实质合作', '07':'提供援助', '08':'让步',
    '09':'调查', '10':'要求', '11':'表达不满', '12':'拒绝', '13':'威胁',
    '14':'抗议', '15':'展示军事姿态', '16':'减少关系', '17':'强制',
    '18':'袭击', '19':'交战', '20':'非常规大规模暴力',
}
PERIODS = {'24h':24, '72h':72, '7d':168, '30d':720, 'all':None}


def checked(q='', country='', cameo='', period='72h', min_sources=0, watch='', preset='', order='recent'):
    if not isinstance(q, str) or len(q)>200: raise ValueError('关键词最多200字符')
    if q.strip() and not terms(q): raise ValueError('请输入有效关键词')
    country=country.strip().lower()
    if country:
        from ..desk.places import countries
        if country not in countries(): raise ValueError('请选择有效的ISO国家/地区代码')
    if cameo and cameo not in CAMEO_LABELS: raise ValueError('请选择有效的CAMEO事件大类')
    if period not in PERIODS: raise ValueError('时间范围无效')
    if isinstance(min_sources,bool): raise ValueError('来源数须为0–1000的整数')
    try: minimum=int(min_sources)
    except (ValueError, TypeError): raise ValueError('来源数须为0–1000的整数') from None
    if str(min_sources).strip()!=str(minimum) or not 0<=minimum<=1000: raise ValueError('来源数须为0–1000的整数')
    if watch and not re.fullmatch(r'w_[0-9a-f]{32}',watch): raise ValueError('焦点编号无效')
    if preset and not re.fullmatch(r'filter_[0-9a-f]{32}',preset): raise ValueError('筛选方案编号无效')
    if order not in ('recent','event_date'): raise ValueError('排序方式无效')
    return dict(q=q.strip(), country=country, cameo=cameo, period=period, min_sources=minimum,
                watch=watch, preset=preset, order=order)


@lru_cache(maxsize=1)
def _country_words():
    path=Path(__file__).resolve().parents[1]/'server/data/countries.json'
    aliases={}
    for row in json.loads(path.read_text(encoding='utf-8')):
        values=[row['zh'].casefold(),row['en'].casefold(),row['a2'].casefold(),row['a3'].casefold()]
        for value in values: aliases[value]=values
    return aliases


def _patterns(value):
    words=[]
    countries=_country_words()
    for word in terms(value):
        words.extend((word, *ALIASES.get(word, ()), *countries.get(word, ())))
    return list(dict.fromkeys(words))[:80]


def _match(words):
    names=(OfficialEvent.actor1_name, OfficialEvent.actor2_name, OfficialEvent.action_place)
    # ISO/CAMEO short codes use equality: searching US must not match RUS.
    code_words=[word for word in words if re.fullmatch(r'[a-z]{2,3}',word) and word in _country_words()]
    name_words=[word for word in words if word not in code_words]
    conditions=[func.lower(column).contains(word.casefold(),autoescape=True) for word in name_words for column in names]
    if code_words:
        upper=[word.upper() for word in code_words]
        conditions.extend((OfficialEvent.actor1_country.in_(upper),OfficialEvent.actor2_country.in_(upper),
                           OfficialEvent.action_country_iso2.in_(code_words)))
    return or_(*conditions)


def _filtered(s, filters, now=None):
    query=select(OfficialEvent)
    if filters['country']:
        iso=filters['country']
        query=query.where(or_(OfficialEvent.action_country_iso2==iso,
            OfficialEvent.actor1_country_iso2==iso, OfficialEvent.actor2_country_iso2==iso))
    if filters['cameo']: query=query.where(OfficialEvent.event_root_code==filters['cameo'])
    hours=PERIODS[filters['period']]
    if hours:
        lower=((now or datetime.now(timezone.utc))-timedelta(hours=hours)).isoformat(timespec='seconds').replace('+00:00','Z')
        query=query.where(OfficialEvent.last_seen>=lower)
    if filters['q']: query=query.where(_match(_patterns(filters['q'])))
    if filters['watch']:
        from ..desk.focus_lifecycle import require_active
        watch=require_active(s,filters['watch'])
        # This is source recall, not publication of an LLM-accepted membership.
        words=_patterns(watch.scope)
        if words: query=query.where(_match(words))
        excludes=_patterns(watch.exclude)
        if excludes: query=query.where(~_match(excludes))
    if filters['min_sources']:
        domains=select(func.count(func.distinct(OfficialMention.domain))).where(
            OfficialMention.event_id==OfficialEvent.event_id, OfficialMention.domain!='').correlate(OfficialEvent).scalar_subquery()
        query=query.where(domains>=filters['min_sources'])
    return query


def _coverage(s, keys):
    if not keys: return {}
    rows=s.exec(select(OfficialMention.event_id,func.count(),func.count(func.distinct(func.nullif(OfficialMention.domain,''))),
        func.max(OfficialMention.confidence)).where(OfficialMention.event_id.in_(keys))
        .group_by(OfficialMention.event_id).execution_options(n2d_query_name='selection.coverage.page')).all()
    return {key:dict(documents=docs, sources=domains, confidence=confidence) for key,docs,domains,confidence in rows}


def _url(value):
    if not isinstance(value,str) or len(value)>16000:return ''
    try:
        url=urlsplit(value)
        if url.scheme not in ('http','https') or not url.hostname or url.username or url.password or any(ord(c)<33 for c in value):return ''
        url.port
        return value
    except (ValueError,TypeError):return ''


def _card(event, coverage=None, selected_task=''):
    from ..translations.language import describe_language
    label=CAMEO_LABELS.get(event.event_root_code,event.event_code or '待分类')
    title=' · '.join(value for value in (event.actor1_name,label,event.actor2_name) if value)
    known=coverage is not None
    coded_title=title or 'GDELT来源事件 '+event.event_id
    return dict(event_id=event.event_id,title=coded_title,coded_title=coded_title,
        display_title='报道内容待补充',excerpt='',report_preview=None,reports=[],content_status='missing',
        content_job_status='missing',
        actor1_name=event.actor1_name,actor2_name=event.actor2_name,
        event_code=event.event_code,event_root=event.event_root_code,cameo_label=label,
        action_country=event.action_country_iso2,action_name=event.action_place,
        latitude=event.latitude,longitude=event.longitude,event_date=event.event_date,
        added_at=event.added_at,first_seen=event.first_seen,last_seen=event.last_seen,
        observed_documents=coverage['documents'] if known else None,
        observed_sources=coverage['sources'] if known else None,
        confidence=coverage.get('confidence') if known else None,coverage_known=known,
        coverage_complete=False,initial_mentions=event.initial_mentions,initial_sources=event.initial_sources,
        goldstein=event.goldstein,source_url=_url(event.source_url),selected_task=selected_task,
        source_ref=dict(feed=event.feed,stamp=event.stamp,row_index=event.row_index),
        reasons=['官方结构化事件线索，尚未完成原文核查',
                 '媒体域名数仅反映本站已接收的Mentions，不等于独立证实'],
        material_kind='event_metadata',language=describe_language(' '.join(
            value for value in (event.actor1_name,event.actor2_name,event.action_place) if value)),
        source_language=dict(code='',label='未知',basis='not_provided',display='报道语言未知'))


def list_candidates(s, filters, offset=0, limit=30):
    filters=checked(**filters)
    if isinstance(offset,bool) or not isinstance(offset,int) or not 0<=offset<=100000: raise ValueError('分页位置无效')
    if isinstance(limit,bool) or not isinstance(limit,int) or not 1<=limit<=100: raise ValueError('每页数量无效')
    query=_filtered(s,filters)
    order=OfficialEvent.last_seen if filters['order']=='recent' else OfficialEvent.event_date
    events=s.exec(query.order_by(order.desc(),OfficialEvent.event_id.desc()).offset(offset).limit(limit+1)
        .execution_options(n2d_query_name='selection.candidates.page')).all()
    more=len(events)>limit
    events=events[:limit]
    keys=[row.event_id for row in events]
    counts=_coverage(s,keys)
    origins=s.exec(select(SelectionOrigin.event_id,SelectionOrigin.task).where(
        SelectionOrigin.event_id.in_(keys),visible_task(SelectionOrigin.task))
        .order_by(SelectionOrigin.created,SelectionOrigin.task).execution_options(n2d_query_name='selection.selected.page')).all() if keys else []
    selected={}
    for key,task in origins:selected.setdefault(key,task)
    feeds=s.exec(select(FeedState).order_by(FeedState.key)).all()
    from .event_content import enrich_cards
    cards=[_card(row,counts.get(row.event_id),selected.get(row.event_id,'')) for row in events]
    enrich_cards(s,events,cards,_url)
    return dict(items=cards,
        total=None,has_more=more,offset=offset,limit=limit,filters=filters,
        pagination=dict(page=offset//limit+1,limit=limit,max_offset=100000),
        coverage=dict(scope='本站已索引的官方来源记录；未观测到提及不代表没有报道',
            counts_are_partial=True,time_basis='最近官方事件入库/提及时间，事件发生日期单列',
            watch_mode='焦点关键词匹配，未经模型精筛' if filters['watch'] else '',
            feeds=[dict(feed=row.key,enabled=row.enabled,last_complete=row.last_complete,last_error=row.last_error) for row in feeds
                   if row.key.startswith(('events_','mentions_'))]))


def content_cards(s,event_ids):
    """Refresh only saved report content for one visible page, without counts."""
    if not isinstance(event_ids,(list,tuple)) or len(event_ids)>30:
        raise ValueError('每次最多刷新30条事件报道')
    if any(not isinstance(key,str) or not re.fullmatch(r'\d{1,30}',key) for key in event_ids):
        raise ValueError('GDELT事件编号无效')
    keys=list(dict.fromkeys(event_ids))
    if not keys:return dict(items=[],missing=[],scope='当前可见事件的已保存报道内容')
    rows=s.exec(select(OfficialEvent).where(OfficialEvent.event_id.in_(keys))
        .execution_options(n2d_query_name='selection.content_cards.events')).all()
    by_id={row.event_id:row for row in rows}
    events=[by_id[key] for key in keys if key in by_id]
    from .event_content import enrich_cards
    cards=[_card(row) for row in events]
    enrich_cards(s,events,cards,_url)
    fields=('event_id','title','display_title','coded_title','report_preview','reports',
            'content_status','content_job_status','source_language','language','excerpt','content_note','content_scope','reconstruction')
    return dict(items=[{name:card.get(name) for name in fields} for card in cards],
        missing=[key for key in keys if key not in by_id],scope='当前可见事件的已保存报道内容')


def detail(s,event_id):
    if not re.fullmatch(r'\d{1,30}',event_id or ''):raise ValueError('GDELT事件编号无效')
    event=s.get(OfficialEvent,event_id)
    if not event:raise KeyError('官方事件尚未接收或索引，请检查Events接收进度')
    coverage=_coverage(s,[event_id])
    recent_rows=s.exec(select(OfficialMention).where(OfficialMention.event_id==event_id)
        .order_by(OfficialMention.last_seen.desc()).limit(31)
        .execution_options(n2d_query_name='selection.mentions.detail')).all()
    # The indexed recent read bounds source work for one hot event. Confidence
    # ordering applies only within this candidate set, not all historical rows.
    def ranked(values):
        values=sorted(values,key=lambda row:row.key)
        values.sort(key=lambda row:row.last_seen or '',reverse=True)
        values.sort(key=lambda row:row.confidence if row.confidence is not None else -1,reverse=True)
        return values
    rows=ranked(recent_rows)[:30]
    batch_keys={event.feed+'_'+event.stamp, *(row.last_feed+'_'+row.last_stamp for row in rows)}
    manifests=dict(s.exec(select(IntakeBatch.key,IntakeBatch.sha256).where(IntakeBatch.key.in_(batch_keys))
        .execution_options(n2d_query_name='selection.source_manifests')).all())
    card=_card(event,coverage.get(event_id))
    card['selected_task']=s.exec(select(SelectionOrigin.task).where(
        SelectionOrigin.event_id==event_id,visible_task(SelectionOrigin.task))
        .order_by(SelectionOrigin.created,SelectionOrigin.task).limit(1)
        .execution_options(n2d_query_name='selection.selected.detail')).first() or ''
    if manifests.get(event.feed+'_'+event.stamp):
        card['source_ref']['archive_sha256']=manifests[event.feed+'_'+event.stamp]
    from .event_content import enrich_cards,MENTIONS_PER_EVENT,RECENT_MENTION_CANDIDATES,RELATION_NOTE
    report_mentions={event_id:[dict(key=row.key,url=row.doc_identifier,domain=row.domain,
        confidence=row.confidence,last_seen=row.last_seen)
        for row in ranked(recent_rows[:RECENT_MENTION_CANDIDATES])[:MENTIONS_PER_EVENT]]}
    from .source_content import resolve_previews
    mention_urls=list(dict.fromkeys(url for url in [_url(event.source_url),
        *(_url(row.doc_identifier) for row in rows),
        *(_url(row['url']) for row in report_mentions[event_id])] if url))
    previews=resolve_previews(s,mention_urls) if mention_urls else {}
    enrich_cards(s,[event],[card],_url,source_mentions=report_mentions,resolved_previews=previews)
    mentions=[dict(doc_identifier=row.doc_identifier,url=_url(row.doc_identifier),domain=row.domain,
        first_seen=row.first_seen,last_seen=row.last_seen,confidence=row.confidence,
        feed=row.last_feed,stamp=row.last_stamp,row_index=row.last_row_index,
        archive_sha256=manifests.get(row.last_feed+'_'+row.last_stamp,''),
        sentence_id=row.sentence_id,action_offset=row.action_offset,in_raw_text=row.in_raw_text,
        preview=dict(previews[_url(row.doc_identifier)],relation='mention',relation_note=RELATION_NOTE,
            source_kind='search_snippet',material_kind='source_excerpt',requires_original_text=True)
            if _url(row.doc_identifier) in previews else None) for row in rows]
    return dict(event=card,mentions=mentions,
        mentions_limited=len(recent_rows)>30,
        mentions_scope=dict(ranking_scope='recent_candidates_not_full_history',recent_candidate_limit=31,
            returned_limit=30,counts_are_partial=True,
            note='从最多31条近期已索引提及中按置信度展示30条；不是全历史最高置信报道。'),
        note='来源事件与媒体提及是研究线索；选题后再补原文、交叉验证。')


def list_filters(s):
    rows=s.exec(select(SelectionFilter).order_by(SelectionFilter.updated.desc(),SelectionFilter.key).limit(100)
        .execution_options(n2d_query_name='selection.filters.list')).all()
    return [dict(key=row.key,name=row.name,filters=json.loads(row.filters_json),updated=row.updated) for row in rows]


def save_filters(s,name,filters,key=None):
    name=' '.join(str(name).split())
    if not 1<=len(name)<=80:raise ValueError('筛选方案名称须为1–80字符')
    values=checked(**filters)
    values['preset']=''
    key=key or 'filter_'+uuid4().hex
    if not re.fullmatch(r'filter_[0-9a-f]{32}',key):raise ValueError('筛选方案编号无效')
    lock_news(s,'selection-filter/'+key)
    row=s.get(SelectionFilter,key) or SelectionFilter(key=key,name=name,filters_json='{}')
    row.name,row.filters_json,row.updated=name,json.dumps(values,ensure_ascii=False,sort_keys=True),utc_now_iso()
    s.add(row);s.commit()
    return dict(key=key,name=name,filters=values,updated=row.updated)


def delete_filter(s,key):
    if not re.fullmatch(r'filter_[0-9a-f]{32}',key or ''):raise ValueError('筛选方案编号无效')
    lock_news(s,'selection-filter/'+key)
    row=s.get(SelectionFilter,key)
    if not row:raise KeyError('筛选方案不存在')
    s.delete(row);s.commit();return dict(ok=True)


def select_candidate(engine,event_id,request_id,title='',angle=''):
    if not isinstance(request_id,str) or not 8<=len(request_id)<=100:raise ValueError('创建编号须为8–100字符')
    if not isinstance(title,str) or len(title)>250 or not isinstance(angle,str) or len(angle)>1500:raise ValueError('选题标题或角度过长')
    from ..research import service as research
    from ..research.models import ResearchCase
    from ..workbench import service as work
    from ..workbench.models import ContentTask
    from ..topics.models import TopicWorkspace,TopicSource
    key='topic_'+research.digest(['gdelt-select',request_id])[:24]
    fingerprint=research.digest(dict(event_id=event_id,title=title,angle=angle))
    # Selection origin, workspace, initial source snapshot and task publish in
    # one transaction. A crash cannot leave an unbound task or consume an ID.
    with Session(engine) as s:
        lock_news(s,'content:'+key)
        old=s.get(SelectionOrigin,key)
        if old:
            if old.request_hash!=fingerprint:raise TaskConflict('创建编号已用于其他来源事件或角度')
            return dict(key=key,url='/workbench/'+key+'/selection',event_id=old.event_id)
        if s.get(ContentTask,key) or s.get(TopicWorkspace,key):raise TaskConflict('创建编号已被使用')
        data=detail(s,event_id)
        event=data['event']
        snapshot=dict(event=event,mentions=data['mentions'])
        reconstructed = event.get('reconstruction')
        selected_title=(title.strip() or (event.get('report_preview') or {}).get('title')
                        or (reconstructed or {}).get('excerpt')
                        or event.get('coded_title') or event['title'])[:250]
        case_id='topiccase_'+research.digest(key)[:24]
        workspace=TopicWorkspace(workspace_id=key,request_hash=fingerprint,case_id=case_id,
            title=selected_title,angle=angle,scope_json=research.dump(work.topics.Scope().checked()))
        s.add(ResearchCase(case_id=case_id,title=selected_title,goal='内容任务中的选题讨论',
            strategy_json=research.dump(dict(purpose='topic_selection',max_minutes=10,max_searches=0,max_reads=30)),cutoff=utc_now_iso()))
        s.add(workspace)
        s.add(ContentTask(key=key,origin_hash=fingerprint))
        text='GDELT结构化事件线索（自动编码，尚未核查原文）\n'+json.dumps(snapshot,ensure_ascii=False,sort_keys=True)
        document=dict(provider='gdelt_events',title=selected_title,content=text,
            url=event['source_url'],canonical_url=event['source_url'],published_at='',
            source_domain='gdeltproject.org',language='',country=event['action_country'],revision=None,
            source_kind='event_metadata',official_event_id=event_id,source_ref=event['source_ref'],requires_original_text=True)
        sid='topic_source_'+research.digest([key,'gdelt_event',event_id])[:24]
        s.add(TopicSource(source_id=sid,workspace_id=key,article_key='gdelt_event/'+event_id,
            document_json=research.dump(document),content_hash=research.digest(document)))
        selected_sources=[sid]
        if reconstructed and _url(reconstructed.get('url')):
            from ..research.materials import reconstruction_lead
            url = _url(reconstructed['url'])
            doc = dict(reconstruction_lead(reconstructed), title='GDELT 重建片段 · '+(event.get('coded_title') or event_id),
                       url=url, canonical_url=url, published_at='', revision=None, country='',
                       source_domain=urlsplit(url).hostname or '', official_event_id=event_id)
            source_id = 'topic_source_'+research.digest([key,'ngrams',event_id,url])[:24]
            s.add(TopicSource(source_id=source_id, workspace_id=key, article_key='gdelt_ngrams/'+event_id,
                              document_json=research.dump(doc), content_hash=research.digest(doc)))
            selected_sources.append(source_id)
        # Source previews remain excerpts/leads even when the local work library
        # happens to own full text. Selection freezes only what this radar showed.
        reports=[report for report in event.get('reports',[]) if report.get('title') or report.get('excerpt')]
        for report in reports[:12-len(selected_sources)]:
            report_key=report.get('key') or research.digest(report.get('source_url') or report)
            doc=dict(provider='gdelt_source',title=report.get('title') or '相关报道摘录',
                content=report.get('excerpt') or '',url=_url(report.get('source_url')),
                canonical_url=_url(report.get('source_url')),published_at=report.get('published_at') or '',
                source_domain=report.get('domain') or '',language=report.get('language') or '',country='',revision=None,
                source_kind='search_snippet',material_kind='source_excerpt',requires_original_text=True,
                official_event_id=event_id,source_preview=dict(report),
                provenance_note='同文档报道线索可能提及多个事件；本预览不是本条GDELT事件的核实摘要。')
            report_sid='topic_source_'+research.digest([key,'gdelt_report',report_key])[:24]
            s.add(TopicSource(source_id=report_sid,workspace_id=key,article_key='gdelt_report/'+report_key,
                document_json=research.dump(doc),content_hash=research.digest(doc)))
            selected_sources.append(report_sid)
        workspace.selected_json=research.dump(selected_sources);s.add(workspace)
        from ..desk.official_graph import track
        track(s,event_id)
        work.bind(s,key,'gdelt_event',event_id)
        s.add(SelectionOrigin(task=key,event_id=event_id,request_hash=fingerprint,snapshot_json=research.dump(snapshot)))
        s.commit()
    return dict(key=key,url='/workbench/'+key+'/selection',event_id=event_id)
