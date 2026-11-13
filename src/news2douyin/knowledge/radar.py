"""Read-only radar projections with explicit provenance and conservative country roles.

Provider selection operates on event membership: mixed-source events retain all
their sources. Country links are a projection of sourced role assertions, never
of country co-occurrence, media location or inferred citizenship.
"""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from itertools import combinations, product
import json
import re
from pathlib import Path
from sqlalchemy import and_, case, func, literal, or_, union_all
from sqlmodel import select
from ..desk.models import RadarEntry, GkgSource
from ..extraction.models import TextMention, MentionReview
from ..search.service import event_query
from ..storage.models import Article, ArticleVersion, ArticleEventLink, Event, GdeltDiscovery, CollectedObservation, TaskRecord, RunRecord
from .models import GraphMetric, GraphRelation
from ..storage.sql import json_text,json_is_string
from ..storage.queries import project_keys,count_keys,scalar,all_rows,exists


CHANNELS={'all':'综合动态','diplomacy':'外交活动','conflict':'军事冲突线索'}
KINDS={'diplomacy':('meeting','visit','signing'),'conflict':('attack',)}
PROVIDERS={'all':'全部采集源','gkg':'GDELT GKG','gdelt':'GDELT DOC',
           'worldnewsapi':'WorldNewsAPI','mock':'演示数据','other':'其他采集源','unknown':'来源待核'}
WINDOW=5000
NETWORK_LIMIT=150
COUNTRY_LIMIT=40
COUNTRY_EDGE_LIMIT=300
NETWORK_EDGE_LIMIT=500
MENTION_LIMIT=20000
SCOPE_NOTE='采集源筛选按事件任一报道匹配；混合来源事件保留全部引用。显示已记录的采集渠道，历史路径可能不完整。'


def checked(channel='all',provider='all',view='list',period='auto',**filters):
    if channel not in CHANNELS:raise ValueError('未知雷达频道')
    if provider not in PROVIDERS:raise ValueError('未知采集源')
    if view not in ('list','network','countries'):raise ValueError('未知雷达视图')
    if period not in ('auto','all','1','7','30'):raise ValueError('未知时间范围')
    return dict(filters,channel=channel,provider=provider,view=view,period=period)


def latest_version_id(article_key):
    # Indexed lookup per relevant article; never group the complete version log.
    return select(ArticleVersion.id).where(ArticleVersion.article_key==article_key).order_by(ArticleVersion.id.desc()).limit(1).correlate_except(ArticleVersion).scalar_subquery()


def current_mentions():
    return select(TextMention).outerjoin(MentionReview,MentionReview.id==TextMention.id).where(
        TextMention.version_id==latest_version_id(TextMention.article),
        or_(MentionReview.id==None,MentionReview.decision!='rejected'))


def observation_articles(provider=None):
    task_json=TaskRecord.profile_json
    run_json=RunRecord.stats_json
    conditions=[]
    for document in (task_json,run_json):
        field=json_text(document,'provider')
        valid=json_is_string(document,'provider') & ~field.in_(['','unknown'])
        if provider=='other':valid=valid & ~field.in_(['gkg','gdelt','worldnewsapi','mock'])
        elif provider is not None:valid=valid & (field==provider)
        conditions.append(valid)
    return select(CollectedObservation.article_key).outerjoin(TaskRecord,TaskRecord.task_id==CollectedObservation.scope_id).outerjoin(RunRecord,RunRecord.run_key==CollectedObservation.scope_id).where(or_(*conditions))


def provider_articles(provider):
    # Both DOC and GKG write GdeltDiscovery. A GKG record alone must never create a DOC badge.
    if provider=='unknown':
        # Match the visible provenance labels: a missing primary provider is not
        # unknown when another durable record recovers an actual collection path.
        return select(Article.article_key).where(
            or_(Article.provider=='',Article.provider=='unknown',Article.provider==None),
            ~Article.article_key.in_(select(GkgSource.article_key)),
            ~Article.article_key.in_(select(GdeltDiscovery.article_key)),
            ~Article.article_key.in_(observation_articles()))
    path=Article.provider==provider
    if provider=='gkg':path=or_(path,Article.article_key.in_(select(GkgSource.article_key)))
    if provider=='gdelt':
        legacy=select(GdeltDiscovery.article_key).where(~GdeltDiscovery.article_key.in_(select(GkgSource.article_key)))
        path=or_(path,Article.article_key.in_(legacy))
    if provider=='other':path=~Article.provider.in_(['','unknown','gkg','gdelt','worldnewsapi','mock'])
    return select(Article.article_key).where(or_(path,Article.article_key.in_(observation_articles(provider))))


def _common_statement(s,f,columns=None):
    base=event_query(s,query=f.get('q',''),country=f.get('country',''),topic=f.get('topic',''),mode=f.get('mode','contains'))
    stmt=select(*(columns if columns is not None else (RadarEntry,))).where(RadarEntry.key.in_(project_keys(base,Event.event_key)))
    stmt=stmt.outerjoin(GraphMetric,GraphMetric.event==RadarEntry.key)
    band=case((GraphMetric.score==None,RadarEntry.band),(GraphMetric.score>=60,'优先看'),(GraphMetric.score>=35,'优先核查'),else_='其他进展')
    if f.get('band'):stmt=stmt.where(band==f['band'])
    return stmt


def _recent_boundary(period):
    days=7 if period=='auto' else int(period)
    return (datetime.now(timezone.utc)-timedelta(days=days)).isoformat(timespec='seconds').replace('+00:00','Z')


def statement(s,filters,*,historical=None,columns=None):
    """Shared exact selection; caller may reuse the facet's auto-range decision."""
    f=checked(**filters)
    stmt=_common_statement(s,f,columns)
    if f['provider']!='all':
        stmt=stmt.where(RadarEntry.key.in_(select(ArticleEventLink.event_key).where(ArticleEventLink.article_key.in_(provider_articles(f['provider'])))))
    if f['channel']!='all':
        mentions=current_mentions().with_only_columns(TextMention.article).where(json_text(TextMention.data,'kind').in_(KINDS[f['channel']]))
        stmt=stmt.where(RadarEntry.key.in_(select(ArticleEventLink.event_key).where(ArticleEventLink.article_key.in_(mentions))))
    if f['period'] in ('auto','1','7','30'):
        recent=stmt.where(RadarEntry.seen>=_recent_boundary(f['period']))
        if f['period']!='auto':stmt=recent;historical=False
        elif historical is None:
            historical=not exists(s,project_keys(recent,RadarEntry.key),name='radar.auto_range')
            if not historical:stmt=recent
        elif not historical:stmt=recent
    return stmt.order_by(func.coalesce(GraphMetric.score,-1).desc(),RadarEntry.priority.desc(),RadarEntry.seen.desc(),RadarEntry.key),bool(historical)


def count(s,stmt,key=RadarEntry.key):
    return count_keys(s,stmt,key,name='radar.count_keys')


def _materialized(s,statement,name):
    cte=statement.cte(name)
    dialect=s.bind.dialect
    if dialect.name=='postgresql' or (dialect.name=='sqlite' and (dialect.server_version_info or ())>=(3,35)):
        return cte.prefix_with('MATERIALIZED',dialect=dialect.name)
    return cte


def _facet_facts(s,f):
    """One bounded-width candidate relation; all facet counts reuse its flags."""
    # On all-time requests no operation consumes seen; do not force a heap read
    # for that field while materializing the reusable narrow base relation.
    seen=RadarEntry.seen if f['period']!='all' else literal('')
    base=_materialized(s,_common_statement(s,f).with_only_columns(RadarEntry.key.label('key'),seen.label('seen'),
        case((GraphMetric.event!=None,1),else_=0).label('analyzed'),maintain_column_froms=True),'radar_base')
    articles=select(ArticleEventLink.article_key).where(ArticleEventLink.event_key.in_(select(base.c.key))).distinct().cte('radar_articles')
    observed=select(CollectedObservation.article_key,CollectedObservation.scope_id).where(
        CollectedObservation.article_key.in_(select(articles.c.article_key))).distinct().cte('radar_observed')
    paths=[]
    for model,key,document in [(TaskRecord,TaskRecord.task_id,TaskRecord.profile_json),(RunRecord,RunRecord.run_key,RunRecord.stats_json)]:
        value=json_text(document,'provider')
        channel=case((value.in_(['gkg','gdelt','worldnewsapi','mock']),value),else_=literal('other'))
        paths.append(select(key.label('scope_id'),channel.label('provider')).where(
            key.in_(select(observed.c.scope_id)),json_is_string(document,'provider'),~value.in_(['','unknown'])))
    recorded=union_all(*paths).cte('radar_recorded_paths')
    # Only the actually observed subset needs a per-article aggregation. Primary
    # provider and GKG/DOC metadata are scalar joins, not a whole-corpus UNION
    # followed by a second large GROUP BY.
    known_providers=[p for p in PROVIDERS if p not in ('all','unknown')]
    obs=select(observed.c.article_key,func.count().label('valid'),
        *[func.max(case((recorded.c.provider==p,1),else_=0)).label(p) for p in known_providers])        .select_from(observed).join(recorded,recorded.c.scope_id==observed.c.scope_id)        .group_by(observed.c.article_key).cte('radar_observation_flags')
    predicates={p:or_(Article.provider==p,func.coalesce(obs.c[p],0)==1) for p in known_providers}
    predicates['gkg']=or_(predicates['gkg'],GkgSource.article_key!=None)
    predicates['gdelt']=or_(predicates['gdelt'],and_(GdeltDiscovery.article_key!=None,GkgSource.article_key==None))
    predicates['other']=or_(~Article.provider.in_(['','unknown','gkg','gdelt','worldnewsapi','mock']),func.coalesce(obs.c.other,0)==1)
    predicates['unknown']=and_(or_(Article.provider==None,Article.provider.in_(['','unknown'])),
        GkgSource.article_key==None,GdeltDiscovery.article_key==None,func.coalesce(obs.c.valid,0)==0)
    pf=select(ArticleEventLink.event_key,
        *[func.max(case((predicates[p],1),else_=0)).label('p_'+p) for p in PROVIDERS if p!='all'])        .select_from(ArticleEventLink).join(Article,Article.article_key==ArticleEventLink.article_key)        .outerjoin(GkgSource,GkgSource.article_key==Article.article_key)        .outerjoin(GdeltDiscovery,GdeltDiscovery.article_key==Article.article_key)        .outerjoin(obs,obs.c.article_key==Article.article_key)        .where(ArticleEventLink.event_key.in_(select(base.c.key))).group_by(ArticleEventLink.event_key).cte('radar_provider_flags')
    latest=select(articles.c.article_key,latest_version_id(articles.c.article_key).label('version_id')).cte('radar_latest')
    kind=json_text(TextMention.data,'kind')
    cf=select(ArticleEventLink.event_key,
        *[func.max(case((kind.in_(kinds),1),else_=0)).label('c_'+channel) for channel,kinds in KINDS.items()]).join(
            latest,latest.c.article_key==ArticleEventLink.article_key).join(TextMention,and_(TextMention.version_id==latest.c.version_id,TextMention.article==latest.c.article_key)).outerjoin(MentionReview,MentionReview.id==TextMention.id).where(
            ArticleEventLink.event_key.in_(select(base.c.key)),or_(MentionReview.id==None,MentionReview.decision!='rejected')).group_by(ArticleEventLink.event_key).cte('radar_channel_flags')
    return _materialized(s,select(base.c.key,base.c.seen,base.c.analyzed,
        *[func.coalesce(pf.c['p_'+p],0).label('p_'+p) for p in PROVIDERS if p!='all'],
        *[func.coalesce(cf.c['c_'+c],0).label('c_'+c) for c in KINDS]).outerjoin(pf,pf.c.event_key==base.c.key).outerjoin(cf,cf.c.event_key==base.c.key),'radar_facts')


def facets(s,filters):
    """One SQL aggregate for main counts and every facet, including auto fallbacks."""
    f=checked(**filters);facts=_facet_facts(s,f)
    def predicate(channel,provider):
        parts=[]
        if channel!='all':parts.append(facts.c['c_'+channel]==1)
        if provider!='all':parts.append(facts.c['p_'+provider]==1)
        return and_(*parts) if parts else literal(True)
    predicates={'main':predicate(f['channel'],f['provider'])}
    predicates.update({'c_'+channel:predicate(channel,f['provider']) for channel in CHANNELS})
    predicates.update({'p_'+provider:predicate(f['channel'],provider) for provider in PROVIDERS})
    recent=facts.c.seen>=_recent_boundary(f['period']) if f['period']!='all' else literal(True)
    cols=[]
    for name,pred in predicates.items():
        cols.extend([func.coalesce(func.sum(case((pred,1),else_=0)),0).label(name+'_all'),
                     func.coalesce(func.sum(case((pred & recent,1),else_=0)),0).label(name+'_recent')])
    cols.extend([func.coalesce(func.sum(case((predicates['main'],facts.c.analyzed),else_=0)),0).label('analyzed_all'),
                 func.coalesce(func.sum(case((predicates['main'] & recent,facts.c.analyzed),else_=0)),0).label('analyzed_recent')])
    totals=select(*cols).select_from(facts).cte('radar_totals')
    if f['period']=='auto':in_range=or_(totals.c.main_recent==0,recent)
    elif f['period']=='all':in_range=literal(True)
    else:in_range=recent
    main_keys=select(facts.c.key).select_from(facts).join(totals,literal(True)).where(predicates['main'],in_range)
    source_count=select(func.count(func.distinct(ArticleEventLink.article_key))).where(ArticleEventLink.event_key.in_(main_keys)).scalar_subquery()
    result=all_rows(s,select(totals,source_count.label('source_count')),name='radar.facets')[0]._mapping
    def value(name):
        if f['period']=='all':return int(result[name+'_all'])
        if f['period']=='auto' and not result[name+'_recent']:return int(result[name+'_all'])
        return int(result[name+'_recent'])
    historical=f['period']=='auto' and not result['main_recent']
    analyzed=int(result['analyzed_all'] if f['period']=='all' or historical else result['analyzed_recent'])
    return dict(total=value('main'),historical=historical,
        radar_stats=dict(total=value('main'),analyzed=analyzed,source_count=int(result['source_count'])),
        channels=[dict(id=c,label=label,count=value('c_'+c)) for c,label in CHANNELS.items()],
        providers=[dict(id=p,label=label,count=value('p_'+p)) for p,label in PROVIDERS.items()])


def _provider(value):
    return value if value in PROVIDERS and value!='all' else 'unknown' if not value else 'other'


def sources(s,keys):
    """Read only current-page display fields; never load article bodies/raw JSON."""
    keys=list(keys)
    if not keys:return {}
    revision=select(ArticleVersion.revision).where(ArticleVersion.article_key==Article.article_key).order_by(ArticleVersion.revision.desc()).limit(1).correlate(Article).scalar_subquery()
    rows=all_rows(s,select(ArticleEventLink.event_key,Article.article_key,Article.title,Article.source_domain,Article.url,Article.provider,
            GkgSource.article_key.label('gkg'),GdeltDiscovery.article_key.label('doc'),revision.label('revision'))
        .join(Article,Article.article_key==ArticleEventLink.article_key)
        .outerjoin(GkgSource,GkgSource.article_key==Article.article_key)
        .outerjoin(GdeltDiscovery,GdeltDiscovery.article_key==Article.article_key)
        .where(ArticleEventLink.event_key.in_(keys)).order_by(ArticleEventLink.event_key,Article.id),name='radar.sources')
    observations=defaultdict(set);article_ids={row.article_key for row in rows}
    if article_ids:
        paths=select(CollectedObservation.article_key,
            json_text(TaskRecord.profile_json,'provider'),json_is_string(TaskRecord.profile_json,'provider'),
            json_text(RunRecord.stats_json,'provider'),json_is_string(RunRecord.stats_json,'provider'))            .outerjoin(TaskRecord,TaskRecord.task_id==CollectedObservation.scope_id)            .outerjoin(RunRecord,RunRecord.run_key==CollectedObservation.scope_id)            .where(CollectedObservation.article_key.in_(article_ids)).distinct()
        for key,task,task_string,run,run_string in all_rows(s,paths,name='radar.source_paths'):
            for provider,is_string in ((task,task_string),(run,run_string)):
                if is_string and provider:observations[key].add(_provider(provider))
    grouped=defaultdict(lambda:defaultdict(dict))
    for row in rows:
        kinds={_provider(row.provider)}|observations[row.article_key]
        if row.gkg:kinds.add('gkg')
        if row.doc and not row.gkg and row.provider!='gkg':kinds.add('gdelt')
        if len(kinds)>1:kinds.discard('unknown')
        for provider in sorted(kinds):
            grouped[row.event_key][provider][row.article_key]=dict(key=row.article_key,title=row.title,
                domain=row.source_domain,url=row.url,provider=provider,label=PROVIDERS[provider],revision=row.revision)
    return {key:[dict(provider=p,label=PROVIDERS[p],count=len(articles),articles=list(articles.values()))
                 for p,articles in sorted(groups.items())] for key,groups in grouped.items()}


def summary(s,stmt):
    ids=project_keys(stmt,RadarEntry.key).cte('radar_summary_keys')
    source_count=select(func.count(func.distinct(ArticleEventLink.article_key))).where(ArticleEventLink.event_key.in_(select(ids.c.key))).scalar_subquery()
    row=all_rows(s,select(func.count(ids.c.key),func.count(GraphMetric.event),source_count).select_from(ids).outerjoin(GraphMetric,GraphMetric.event==ids.c.key),name='radar.summary')[0]
    return dict(total=int(row[0]),analyzed=int(row[1]),source_count=int(row[2]))


def _page_content(s,filters,historical,offset,limit,*,lookahead=False):
    if offset<0 or not 1<=limit<=500:raise ValueError('Invalid page boundary')
    stmt,_=statement(s,filters,historical=historical,columns=(RadarEntry.key,))
    keys=all_rows(s,stmt.offset(offset).limit(limit+int(lookahead)),name='radar.page_keys')
    has_more=len(keys)>limit;keys=keys[:limit]
    loaded=all_rows(s,select(RadarEntry).where(RadarEntry.key.in_(keys)).limit(limit),name='radar.page') if keys else []
    by_key={card.key:card for card in loaded};items=[by_key[key] for key in keys if key in by_key]
    metrics={m.event:json.loads(m.data) for m in all_rows(s,select(GraphMetric).where(GraphMetric.event.in_(keys)),name='radar.page_metrics')}
    return dict(items=items,metrics=metrics,sources_by_event=sources(s,keys),scope_note=SCOPE_NOTE,has_more=has_more)


def page(s,filters,offset=0,limit=30):
    data=facets(s,filters)
    return dict(**_page_content(s,filters,data['historical'],offset,limit),**data)


def preview(s,filters,offset=0,limit=30):
    """First-paint records, independent from the exact aggregate request.

    None means not computed, never a fabricated zero. Pagination needs only
    one extra ordered key; every request still checks its own actual range.
    """
    filters=checked(**filters)
    _,historical=statement(s,filters,columns=(RadarEntry.key,))
    return dict(**_page_content(s,filters,historical,offset,limit,lookahead=True),
        total=None,historical=historical,radar_stats=dict(total=None,analyzed=None,source_count=None),
        channels=[dict(id=c,label=label,count=None) for c,label in CHANNELS.items()],
        providers=[dict(id=p,label=label,count=None) for p,label in PROVIDERS.items()])


@lru_cache
def country_names():
    rows=json.loads((Path(__file__).resolve().parents[1]/'server/data/countries.json').read_text(encoding='utf-8'))
    names={}
    labels={}
    for row in rows:
        labels[row['a2']]=row['zh']
        for name in (row['zh'],row['en']):names[name.casefold()]=row['a2']
    # Exact curated country names only: do not split free-form alias fields into words.
    aliases={'United States of America':'US','US':'US','U.S.':'US','USA':'US','U.S.A.':'US','美利坚合众国':'US',
        'UK':'GB','U.K.':'GB','Great Britain':'GB','联合王国':'GB','大不列颠':'GB',
        '中华人民共和国':'CN','PRC':'CN','Russian Federation':'RU','俄罗斯联邦':'RU',
        'Republic of Korea':'KR','大韩民国':'KR','DPRK':'KP','朝鲜民主主义人民共和国':'KP','Turkey':'TR'}
    names.update({name.casefold():iso for name,iso in aliases.items()})
    # Ambiguous English state/country name requires explicit country identification in future.
    names.pop('georgia',None)
    return names,labels


def country(argument):
    if argument.get('role') not in ('actor','participant','target'):return None
    if argument.get('entity_type')=='person':return None
    raw=argument.get('text','').strip()
    evidence=argument.get('evidence','')
    # A substring such as Russia inside Russian is not an explicit country token.
    if evidence and re.search('[A-Za-z]',raw) and not re.search(r'(?<![A-Za-z])'+re.escape(raw)+r'(?![A-Za-z])',evidence):return None
    if raw in ('us','Us'):return None
    if argument.get('entity_type','unknown')=='unknown' and raw.casefold() in ('jordan','chad','georgia','congo','korea','us'):return None
    return country_names()[0].get(raw.casefold())


def country_view(s,keys,channel):
    relevant=set(KINDS.get(channel,(*KINDS['diplomacy'],*KINDS['conflict'])))
    scoped=select(ArticleEventLink.event_key,ArticleEventLink.article_key).where(ArticleEventLink.event_key.in_(keys)).cte('country_articles')
    latest=select(scoped.c.event_key,scoped.c.article_key,latest_version_id(scoped.c.article_key).label('version_id')).cte('country_latest')
    rows=all_rows(s,select(latest.c.event_key,TextMention).join(TextMention,and_(TextMention.version_id==latest.c.version_id,TextMention.article==latest.c.article_key))
        .outerjoin(MentionReview,MentionReview.id==TextMention.id)
        .where(or_(MentionReview.id==None,MentionReview.decision!='rejected'),json_text(TextMention.data,'kind').in_(relevant))
        .order_by(TextMention.version_id.desc(),latest.c.event_key,TextMention.id).limit(MENTION_LIMIT+1),name='radar.country_mentions') if keys else []
    mentions_truncated=len(rows)>MENTION_LIMIT;rows=rows[:MENTION_LIMIT]
    grouped={};seen=set();eligible=set();excluded=Counter();statuses=Counter()
    for event,mention in rows:
        data=json.loads(mention.data)
        if data.get('kind') not in relevant:continue
        fresh=mention.id not in seen
        seen.add(mention.id)
        if fresh:statuses[data.get('status','uncertain')]+=1
        if data.get('status')!='reported':
            if fresh:excluded['not_reported']+=1
            continue
        roles=defaultdict(set)
        for argument in data.get('arguments',[]):
            iso=country(argument)
            if iso:roles[argument['role']].add(iso)
        links=[]
        if roles['actor'] and roles['target']:
            links=[(a,b,True) for a,b in product(roles['actor'],roles['target']) if a!=b]
        elif data['kind'] in KINDS['diplomacy']:
            # Multiple named parties in one diplomatic assertion are undirected participation.
            parties=roles['actor']|roles['participant']|roles['target']
            links=[(a,b,False) for a,b in combinations(sorted(parties),2)]
        if not links:
            if fresh:excluded['no_explicit_country_pair']+=1
            continue
        eligible.add(mention.id)
        proof=dict(event=event,article=mention.article,revision=mention.revision,quote=data['quote'],
            status=data['status'],date=data.get('date',''),kind=data['kind'],mention=mention.id,
            roles=[dict(role=a['role'],text=a['text'],country=country(a)) for a in data.get('arguments',[]) if country(a)])
        for source,target,directed in links:
            link=grouped.setdefault((source,target,directed,data['kind']),dict(events=set(),proofs={}))
            link['events'].add(event);link['proofs'][(event,mention.id)]=proof
    weights=Counter()
    for (a,b,_,_),link in grouped.items():weights[a]+=len(link['events']);weights[b]+=len(link['events'])
    selected=set(sorted(weights,key=lambda iso:(-weights[iso],iso))[:COUNTRY_LIMIT])
    labels=country_names()[1]
    links=[dict(source=a,target=b,directed=directed,kind=kind,value=len(link['events']),
        evidence=list(link['proofs'].values())[:12],evidence_truncated=len(link['proofs'])>12)
        for (a,b,directed,kind),link in sorted(grouped.items()) if a in selected and b in selected]
    link_total=len(links)
    links=sorted(links,key=lambda link:(-link['value'],link['source'],link['target'],link['kind']))[:COUNTRY_EDGE_LIMIT]
    visible={l['source'] for l in links}|{l['target'] for l in links}
    return dict(nodes=[dict(id=iso,label=labels[iso],value=weights[iso]) for iso in sorted(visible)],links=links,
        considered_mentions=len(seen),eligible_mentions=len(eligible),excluded_mentions=len(seen-eligible),
        excluded_reasons=dict(excluded),status_counts=dict(statuses),
        truncated=len(weights)>COUNTRY_LIMIT or link_total>COUNTRY_EDGE_LIMIT or mentions_truncated,
        node_limit=COUNTRY_LIMIT,edge_limit=COUNTRY_EDGE_LIMIT,edge_total=link_total,
        mention_limit=MENTION_LIMIT,mentions_truncated=mentions_truncated,
        measure='不同事件记录数',scope='仅报道描述已发生、且同一陈述明确给出国家行动角色的关系；不代表独立证实、金额或伤亡。')


def view(s,filters):
    stmt,historical=statement(s,filters,columns=(RadarEntry.key,RadarEntry.title))
    stats=summary(s,stmt);total=stats['total']
    cards=all_rows(s,stmt.limit(WINDOW),name='radar.view_cards');keys=[c.key for c in cards]
    subset=cards[:NETWORK_LIMIT];visible={c.key for c in subset}
    metrics={m.event:m for m in s.exec(select(GraphMetric).where(GraphMetric.event.in_(visible)))}
    provenance=sources(s,[c.key for c in subset])
    nodes=[]
    for c in subset:
        metric=metrics.get(c.key)
        data=json.loads(metric.data) if metric else {}
        nodes.append(dict(data,id=c.key,title=c.title,score=metric.score if metric else 0,
            community=metric.community if metric else c.key,reason=data.get('reason','图分析待更新'),
            analyzed=bool(metric),sources=[{k:v for k,v in group.items() if k!='articles'} for group in provenance.get(c.key,[])]))
    relation_query=select(GraphRelation).where(GraphRelation.source.in_(visible),GraphRelation.target.in_(visible))
    edge_total=count(s,relation_query,GraphRelation.key)
    relations=s.exec(relation_query.order_by(GraphRelation.weight.desc(),GraphRelation.source,GraphRelation.target).limit(NETWORK_EDGE_LIMIT)).all()
    return dict(network=dict(nodes=nodes,edges=[dict(source=r.source,target=r.target,weight=r.weight,evidence=json.loads(r.evidence)) for r in relations],
            limit=NETWORK_LIMIT,edge_limit=NETWORK_EDGE_LIMIT,edge_total=edge_total,truncated=len(cards)>NETWORK_LIMIT or edge_total>NETWORK_EDGE_LIMIT),
        countries=country_view(s,keys,filters.get('channel','all')),total=total,sampled=len(cards),truncated=total>WINDOW,
        window=WINDOW,historical=historical,channel=filters.get('channel','all'),provider=filters.get('provider','all'),
        scope_note=SCOPE_NOTE,radar_stats=stats)
