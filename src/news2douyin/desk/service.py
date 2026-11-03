"""SQLite is the durable owner of topic rules, editorial choices and research receipts."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import text, func, or_
from sqlmodel import Session, select
from ..storage.articles import lock_news
from ..storage.models import Article, ArticleEventLink, Event, EventState, EventMoment, GdeltDiscovery, utc_now_iso
from ..storage.utils import dumps, loads
from ..tasks.control import TaskConflict
from .models import Watch, WatchEvent, RadarEntry, DeskDirty, DeskMeta, GraphPending, GkgSource, WatchReview
from .rules import Scope, match, focus_context, norm, CHANGE, TENTATIVE, IMPACT


def initialize(engine):
    """Triggers enqueue changes in the same transaction as the existing writer."""
    with engine.begin() as conn:
        direct={'event':'event_key','eventstate':'event_key','articleeventlink':'event_key','eventmoment':'event_key','eventplace':'event'}
        indirect={'article':'article_key','gkgsource':'article_key','gdeltdiscovery':'article_key'}
        for table,column in {**direct,**indirect}.items():
            for op in ('insert','update','delete'):
                refs={'insert':['NEW'],'delete':['OLD'],'update':['OLD','NEW']}[op]
                statements=[]
                for ref in refs:
                    key=f'{ref}.{column}'
                    lookup=f'SELECT {key}' if table in direct else f'SELECT event_key FROM articleeventlink WHERE article_key={key}'
                    statements.append(f'INSERT INTO deskdirty(key) {lookup} ON CONFLICT(key) DO NOTHING;')
                # Disambiguate SQLite SELECT ... ON CONFLICT after FROM using the WHERE above.
                conn.exec_driver_sql(f"CREATE TRIGGER IF NOT EXISTS desk_{table}_{op} AFTER {op} ON {table} BEGIN "+' '.join(statements)+' END')
        for table in ('researchquestion','researchclaim','researchreport'):
            for op in ('insert','update'):
                conn.exec_driver_sql(f"""CREATE TRIGGER IF NOT EXISTS desk_{table}_{op} AFTER {op} ON {table} BEGIN
                    INSERT INTO graphpending(key,token,written) SELECT 'watch/'||watch,hex(randomblob(16)),''
                    FROM watchreview WHERE case_key=NEW.case_id
                    ON CONFLICT(key) DO UPDATE SET token=excluded.token; END""")
        if not conn.execute(text("SELECT 1 FROM deskmeta WHERE key='initial_index'")).first():
            conn.exec_driver_sql('INSERT OR IGNORE INTO deskdirty SELECT event_key FROM event')
            conn.exec_driver_sql("INSERT INTO deskmeta(key,value) VALUES('initial_index','1')")


def enqueue_graph(s,key):
    s.merge(GraphPending(key=key,token=uuid4().hex))


def snapshot(s,event):
    rows=s.exec(select(Article,GkgSource,GdeltDiscovery)
                .join(ArticleEventLink,ArticleEventLink.article_key==Article.article_key)
                .outerjoin(GkgSource,GkgSource.article_key==Article.article_key)
                .outerjoin(GdeltDiscovery,GdeltDiscovery.article_key==Article.article_key)
                .where(ArticleEventLink.event_key==event.event_key).order_by(Article.id)).unique().all()
    from ..storage.articles import latest_version
    from ..extraction.service import mentions
    titles=[event.event_title]; material=[event.summary]; source=[]; places=set(); dates=[]
    for art,gkg,discovery in rows:
        attrs=loads(gkg.attributes,{}) if gkg else {}
        titles.append(art.title)
        material.extend([art.content[:3000],*attrs.get('persons',[]),*attrs.get('orgs',[]),*attrs.get('locations',[]),*attrs.get('themes',[])])
        places.update(attrs.get('locations',[]))
        when=art.published_at or (discovery.seen_at if discovery else '') or art.created_at
        dates.append(when)
        version = latest_version(s, art.article_key)
        actions = mentions(s, version.id) if version else []
        source.append(dict(actions=actions, key=art.article_key,title=art.title,domain=art.source_domain,
            when=when,time_kind='发布' if art.published_at else ('GDELT 收录' if discovery and discovery.seen_at else '入库'),
            url=art.url,media_country=art.country,mention_iso=attrs.get('mention_iso',[]),author=attrs.get('author',''),duplicate=bool(art.is_duplicate),
            duplicate_of=art.duplicate_of_article_key or '',
            record=gkg.record_id if gkg else '',persons=attrs.get('persons',[]),orgs=attrs.get('orgs',[])))
    reviewed=s.exec(select(func.count()).select_from(EventMoment).where(EventMoment.event_key==event.event_key,
                       EventMoment.reviewed==True, EventMoment.deleted==False)).one()
    impact=IMPACT.search(event.event_title)
    change=bool(CHANGE.search(event.event_title))
    uncertain=bool(TENTATIVE.search(event.event_title))
    priority=2*int(bool(impact))+int(change)
    band=('优先核查' if uncertain or (priority>=2 and not reviewed)
          else '优先看' if priority>=2 else '其他进展')
    reason='、'.join(filter(None,('影响线索「'+impact.group()+'」' if impact else '',
          '有行动或状态变化' if change else '', '说法仍待核实' if uncertain else ''))) or '尚无强影响或状态变化线索'
    domains={a['domain'] for a in source if a['domain']}
    evidence=f'{len(source)} 篇报道 · {len(domains)} 个域名 · {reviewed} 个已核对节点'
    return RadarEntry(key=event.event_key,title=event.event_title,seen=max(dates,default=event.last_seen_at),
          priority=priority,band=band,reason=reason,evidence=evidence,headings=norm('\n'.join(titles)),
          material=norm('\n'.join(material)),sources=dumps(source),geography=dumps(sorted(places)))


def sync_auto(s,w,card):
    link=s.get(WatchEvent,(w.key,card.key))
    if link and link.choice!='auto':return
    why=match(w,card)
    if why:
        if not link:link=WatchEvent(watch=w.key,event=card.key)
        link.reason=why
        link.relevance,link.branch=focus_context(w,card,why)
        s.add(link)
    elif link:s.delete(link)


def update_event(s,key,watches):
    event=s.exec(select(Event).where(Event.event_key==key)).first()
    state=s.get(EventState,key)
    current=s.get(RadarEntry,key)
    if not event or (state and state.merged_into):
        if current:s.delete(current)
        for link in s.exec(select(WatchEvent).where(WatchEvent.event==key)).all():
            if state and state.merged_into and link.choice!='auto':
                from ..events.workbench import canonical_event
                dest=canonical_event(s,key)
                other=s.get(WatchEvent,(link.watch,dest))
                if not other or other.choice=='auto':
                    s.merge(WatchEvent(watch=link.watch,event=dest,choice=link.choice,reason=link.reason,relevance=link.relevance,branch=link.branch))
                    s.merge(DeskDirty(key=dest))
            enqueue_graph(s,'watch/'+link.watch)
            s.delete(link)
    else:
        card=s.merge(snapshot(s,event))
        for w in watches:
            before=s.get(WatchEvent,(w.key,key))
            if w.enabled:sync_auto(s,w,card)
            after=s.get(WatchEvent,(w.key,key))
            if before or after:enqueue_graph(s,'watch/'+w.key)
            today=utc_now_iso()[:10]
            if (w.enabled and w.auto_research and w.last_auto_day!=today and after
                    and after.choice!='exclude' and not after.reason.startswith('背景')
                    and card.priority>=2 and card.seen>=w.created and event.created_at>=w.created):
                make_review(s,w,key,'signal-'+today)
                w.last_auto_day=today;w.next_review=iso_after(w.review_days);s.add(w)
    enqueue_graph(s,'event/'+key)


def drain(engine,limit=60):
    with Session(engine) as s:
        if not s.exec(select(DeskDirty.key).limit(1)).first():return 0
        lock_news(s)
        keys=s.exec(select(DeskDirty).order_by(DeskDirty.key).limit(limit)).all()
        watches=s.exec(select(Watch)).all()
        for dirty in keys:
            update_event(s,dirty.key,watches)
            s.delete(dirty)
        s.commit()
        return len(keys)


def get_watch(s,key):
    row=s.get(Watch,key)
    if not row:raise KeyError('专题不存在')
    return row


def iso_after(days):
    return (datetime.now(timezone.utc)+timedelta(days=days)).isoformat(timespec='seconds').replace('+00:00','Z')


def save_watch(s,scope:Scope,key=None,expected=None,seed=''):
    lock_news(s)
    w=get_watch(s,key) if key else Watch(key='w_'+uuid4().hex,**scope.model_dump())
    if key and expected!=w.version:raise TaskConflict('专题已被修改，请刷新后重试')
    duplicate=s.exec(select(Watch.key).where(Watch.name==scope.name, Watch.key!=w.key)).first()
    if duplicate:raise ValueError('名称已存在')
    if seed and not s.exec(select(Event.id).where(Event.event_key==seed)).first():raise KeyError('事件不存在')
    rescope=(not key or (scope.scope,scope.dimensions,scope.exclude,scope.kind,scope.enabled)!=(w.scope,w.dimensions,w.exclude,w.kind,w.enabled))
    if key:w.version+=1
    if not key or w.auto_research!=scope.auto_research or w.review_days!=scope.review_days or w.enabled!=scope.enabled:
        w.next_review=iso_after(scope.review_days)
    for field,value in scope.model_dump().items():setattr(w,field,value)
    s.add(w);s.flush()
    if rescope and w.enabled:
        for card in s.exec(select(RadarEntry).execution_options(yield_per=100)):
            sync_auto(s,w,card)
    if seed:link_event(s,w.key,seed,'include',commit=False)
    enqueue_graph(s,'watch/'+w.key)
    s.commit();s.refresh(w)
    return w


def link_event(s,key,event_key,action,*,commit=True):
    if commit:lock_news(s)
    w=get_watch(s,key)
    from ..events.workbench import canonical_event, require_event
    require_event(s,event_key)
    event_key=canonical_event(s,event_key)
    link=s.get(WatchEvent,(key,event_key))
    if action=='auto':
        if link:s.delete(link);s.flush()
        card=s.get(RadarEntry,event_key)
        if card and w.enabled:sync_auto(s,w,card)
    elif action in ('include','exclude'):
        s.merge(WatchEvent(watch=key,event=event_key,choice=action,relevance=3,branch='人工整理',reason='手动关联' if action=='include' else '手动排除'))
    else:raise ValueError('无效关联操作')
    enqueue_graph(s,'watch/'+key)
    if commit:s.commit()


def memberships(s,event_key):
    return [dict(key=w.key,name=w.name,reason=l.reason) for w,l in
            s.exec(select(Watch,WatchEvent).join(WatchEvent,WatchEvent.watch==Watch.key)
            .where(WatchEvent.event==event_key,WatchEvent.choice!='exclude').order_by(Watch.name))]


def focus_entries(s,key,offset=0,limit=30,*,order='recent',branch=''):
    stmt=select(RadarEntry,WatchEvent).join(WatchEvent,WatchEvent.event==RadarEntry.key).where(
        WatchEvent.watch==key,WatchEvent.choice!='exclude')
    if branch:
        stmt=stmt.where(WatchEvent.branch==branch)
    total=s.exec(select(func.count()).select_from(stmt.subquery())).one()
    if order=='relevance':
        stmt=stmt.order_by(WatchEvent.relevance.desc())
    rows=s.exec(stmt.order_by(RadarEntry.seen.desc(),RadarEntry.key).offset(offset).limit(limit)).all()
    return total,[(c,l.reason) for c,l in rows]


def make_review(s,w,event_key,trigger,cutoff=''):
    ident=hashlib.sha256(f'{w.key}|{event_key}|{trigger}|{w.version}'.encode()).hexdigest()[:28]
    review=s.get(WatchReview,ident)
    if review:return review
    review=WatchReview(key=ident,watch=w.key,cutoff=cutoff,event=event_key,question=w.question or f'核对{w.name}的最新进展与历史依据',
                       case_key='focus_'+ident,automatic=trigger.startswith(('due-','signal-')),scope_version=w.version)
    if trigger.startswith('summary-'):
        review.question=f'围绕“{w.name}”生成截至本轮截止日期的专题综述。长期问题：{w.question or w.scope}。按进展与背景分节，引用来源，单列争议、地理范围和待核问题。'
    s.add(review)
    return review
