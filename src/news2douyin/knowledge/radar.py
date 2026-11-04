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
from sqlalchemy import case, func, or_
from sqlmodel import select
from ..desk.models import RadarEntry, GkgSource
from ..extraction.models import TextMention, MentionReview
from ..search.service import event_query
from ..storage.models import Article, ArticleVersion, ArticleEventLink, Event, GdeltDiscovery, CollectedObservation, TaskRecord, RunRecord
from .models import GraphMetric, GraphRelation


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


def current_mentions():
    latest=select(func.max(ArticleVersion.id)).group_by(ArticleVersion.article_key)
    return select(TextMention).outerjoin(MentionReview,MentionReview.id==TextMention.id).where(
        TextMention.version_id.in_(latest),or_(MentionReview.id==None,MentionReview.decision!='rejected'))


def observation_articles(provider=None):
    task_json=case((func.json_valid(TaskRecord.profile_json),TaskRecord.profile_json),else_='{}')
    run_json=case((func.json_valid(RunRecord.stats_json),RunRecord.stats_json),else_='{}')
    conditions=[]
    for document in (task_json,run_json):
        field=func.json_extract(document,'$.provider')
        valid=(func.json_type(document,'$.provider')=='text') & ~field.in_(['','unknown'])
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


def statement(s,filters):
    """Shared selection for HTML, network and country aggregation."""
    f=checked(**filters)
    base=event_query(s,query=f.get('q',''),country=f.get('country',''),topic=f.get('topic',''),mode=f.get('mode','contains'))
    stmt=select(RadarEntry).where(RadarEntry.key.in_(base.with_only_columns(Event.event_key)))
    stmt=stmt.outerjoin(GraphMetric,GraphMetric.event==RadarEntry.key)
    band=case((GraphMetric.score==None,RadarEntry.band),(GraphMetric.score>=60,'优先看'),(GraphMetric.score>=35,'优先核查'),else_='其他进展')
    if f.get('band'):stmt=stmt.where(band==f['band'])
    if f['provider']!='all':
        # GKG records and DOC discovery are retained independently of Article.provider.
        candidates=provider_articles(f['provider'])
        stmt=stmt.where(RadarEntry.key.in_(select(ArticleEventLink.event_key).where(ArticleEventLink.article_key.in_(candidates))))
    if f['channel']!='all':
        # data is canonical validated JSON; kind also has no denormalized SQL column.
        mentions=current_mentions().with_only_columns(TextMention.article).where(
            func.json_extract(TextMention.data,'$.kind').in_(KINDS[f['channel']]))
        stmt=stmt.where(RadarEntry.key.in_(select(ArticleEventLink.event_key).where(ArticleEventLink.article_key.in_(mentions))))
    historical=False
    if f['period'] in ('auto','1','7','30'):
        days=7 if f['period']=='auto' else int(f['period'])
        since=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat(timespec='seconds').replace('+00:00','Z')
        recent=stmt.where(RadarEntry.seen>=since)
        if f['period']!='auto' or s.exec(recent.limit(1)).first():stmt=recent
        else:historical=True
    return stmt.order_by(func.coalesce(GraphMetric.score,-1).desc(),RadarEntry.priority.desc(),RadarEntry.seen.desc(),RadarEntry.key),historical


def count(s,stmt):
    return s.exec(select(func.count()).select_from(stmt.order_by(None).subquery())).one()


def _provider(value):
    return value if value in PROVIDERS and value!='all' else 'unknown' if not value else 'other'


def sources(s,keys):
    """Use current authoritative provenance, including metadata attached by another collector."""
    keys=list(keys)
    if not keys:return {}
    versions=select(ArticleVersion.article_key,func.max(ArticleVersion.revision).label('revision')).group_by(ArticleVersion.article_key).subquery()
    rows=s.exec(select(ArticleEventLink.event_key,Article,GkgSource,GdeltDiscovery,versions.c.revision)
        .join(Article,Article.article_key==ArticleEventLink.article_key)
        .outerjoin(GkgSource,GkgSource.article_key==Article.article_key)
        .outerjoin(GdeltDiscovery,GdeltDiscovery.article_key==Article.article_key)
        .outerjoin(versions,versions.c.article_key==Article.article_key)
        .where(ArticleEventLink.event_key.in_(keys)).order_by(ArticleEventLink.event_key,Article.id)).all()
    observations=defaultdict(set)
    article_ids={article.article_key for _,article,_,_,_ in rows}
    if article_ids:
        for key,profile,stats in s.exec(select(CollectedObservation.article_key,TaskRecord.profile_json,RunRecord.stats_json)
                .outerjoin(TaskRecord,TaskRecord.task_id==CollectedObservation.scope_id)
                .outerjoin(RunRecord,RunRecord.run_key==CollectedObservation.scope_id)
                .where(CollectedObservation.article_key.in_(article_ids))):
            for value in (profile,stats):
                if not value:continue
                try:payload=json.loads(value)
                except (ValueError,TypeError):continue
                provider=payload.get('provider') if isinstance(payload,dict) else None
                if isinstance(provider,str) and provider:observations[key].add(_provider(provider))
    grouped=defaultdict(lambda:defaultdict(dict))
    for event,article,gkg,doc,revision in rows:
        kinds={_provider(article.provider)}|observations[article.article_key]
        # These records prove additional collection paths, even when primary article came elsewhere.
        if gkg:kinds.add('gkg')
        if doc and not gkg and article.provider!='gkg':kinds.add('gdelt')
        # An unspecified primary path adds no useful competing label when a retained path exists.
        if len(kinds)>1:kinds.discard('unknown')
        for provider in sorted(kinds):
            grouped[event][provider][article.article_key]=dict(key=article.article_key,title=article.title,
                domain=article.source_domain,url=article.url,provider=provider,label=PROVIDERS[provider],revision=revision)
    return {key:[dict(provider=p,label=PROVIDERS[p],count=len(articles),articles=list(articles.values()))
                 for p,articles in sorted(groups.items())] for key,groups in grouped.items()}


def summary(s,stmt):
    ids=stmt.order_by(None).with_only_columns(RadarEntry.key)
    return dict(total=count(s,stmt),
        analyzed=s.exec(select(func.count()).select_from(GraphMetric).where(GraphMetric.event.in_(ids))).one(),
        source_count=s.exec(select(func.count(func.distinct(ArticleEventLink.article_key))).where(ArticleEventLink.event_key.in_(ids))).one())


def page(s,filters,offset=0,limit=30):
    stmt,historical=statement(s,filters)
    items=s.exec(stmt.offset(offset).limit(limit)).all()
    metrics={m.event:json.loads(m.data) for m in s.exec(select(GraphMetric).where(GraphMetric.event.in_([c.key for c in items])))}
    channels=[dict(id=key,label=label,count=count(s,statement(s,dict(filters,channel=key))[0])) for key,label in CHANNELS.items()]
    providers=[dict(id=key,label=label,count=count(s,statement(s,dict(filters,provider=key))[0])) for key,label in PROVIDERS.items()]
    return dict(items=items,total=count(s,stmt),historical=historical,metrics=metrics,
        sources_by_event=sources(s,[c.key for c in items]),radar_stats=summary(s,stmt),channels=channels,providers=providers,
        scope_note=SCOPE_NOTE)


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
    rows=s.exec(select(ArticleEventLink.event_key,TextMention).join(TextMention,TextMention.article==ArticleEventLink.article_key)
        .where(ArticleEventLink.event_key.in_(keys),TextMention.id.in_(current_mentions().with_only_columns(TextMention.id)))
        .where(func.json_extract(TextMention.data,'$.kind').in_(relevant))
        .order_by(TextMention.version_id.desc(),ArticleEventLink.event_key,TextMention.id).limit(MENTION_LIMIT+1)).all() if keys else []
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
    stmt,historical=statement(s,filters)
    total=count(s,stmt)
    cards=s.exec(stmt.limit(WINDOW)).all();keys=[c.key for c in cards]
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
    edge_total=count(s,relation_query)
    relations=s.exec(relation_query.order_by(GraphRelation.weight.desc(),GraphRelation.source,GraphRelation.target).limit(NETWORK_EDGE_LIMIT)).all()
    return dict(network=dict(nodes=nodes,edges=[dict(source=r.source,target=r.target,weight=r.weight,evidence=json.loads(r.evidence)) for r in relations],
            limit=NETWORK_LIMIT,edge_limit=NETWORK_EDGE_LIMIT,edge_total=edge_total,truncated=len(cards)>NETWORK_LIMIT or edge_total>NETWORK_EDGE_LIMIT),
        countries=country_view(s,keys,filters.get('channel','all')),total=total,sampled=len(cards),truncated=total>WINDOW,
        window=WINDOW,historical=historical,channel=filters.get('channel','all'),provider=filters.get('provider','all'),
        scope_note=SCOPE_NOTE,radar_stats=summary(s,stmt))
