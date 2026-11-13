"""SQLite is the durable owner of topic rules, editorial choices and research receipts."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from sqlalchemy import text, func, or_
from sqlmodel import Session, select
from sqlalchemy.orm import load_only
from ..storage.articles import lock_news
from ..storage.queries import all_rows, count_keys, page_rows
from ..storage.models import Article, ArticleEventLink, Event, EventState, EventMoment, GdeltDiscovery, utc_now_iso
from ..storage.utils import dumps, loads
from ..tasks.control import TaskConflict
from .models import Watch, WatchEvent, RadarEntry, DeskDirty, DeskMeta, GraphPending, GkgSource, WatchReview
from .rules import Scope, match, focus_context, norm, CHANGE, TENTATIVE, IMPACT


def initialize(engine,*,backfill=True):
    """Install transactional invalidation; migration can opt out of row backfill."""
    from sqlmodel import SQLModel
    from ..storage.sql import install_sql_functions
    direct={'event':'event_key','eventstate':'event_key','articleeventlink':'event_key','eventmoment':'event_key','eventplace':'event'}
    indirect={'article':'article_key','gkgsource':'article_key','gdeltdiscovery':'article_key'}
    with engine.begin() as conn:
        install_sql_functions(conn)
        if engine.dialect.name=='postgresql':
            conn.exec_driver_sql("""CREATE OR REPLACE FUNCTION n2d_desk_dirty()
                RETURNS trigger LANGUAGE plpgsql AS $n2d$
                DECLARE old_key text; new_key text;
                BEGIN
                  IF TG_OP='UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN RETURN NULL; END IF;
                  IF TG_OP<>'INSERT' THEN old_key=to_jsonb(OLD)->>TG_ARGV[0]; END IF;
                  IF TG_OP<>'DELETE' THEN new_key=to_jsonb(NEW)->>TG_ARGV[0]; END IF;
                  IF TG_ARGV[1]='direct' THEN
                    INSERT INTO deskdirty("key")
                      SELECT value FROM unnest(ARRAY[old_key,new_key]) AS keys(value)
                      WHERE value IS NOT NULL GROUP BY value ORDER BY value
                      ON CONFLICT("key") DO NOTHING;
                  ELSE
                    INSERT INTO deskdirty("key") SELECT DISTINCT event_key FROM articleeventlink
                      WHERE article_key=ANY(ARRAY[old_key,new_key]) ORDER BY event_key
                      ON CONFLICT("key") DO NOTHING;
                  END IF;
                  RETURN NULL;
                END $n2d$""")
            for table,column in {**direct,**indirect}.items():
                # Replace old per-operation triggers only during schema installation.
                for op in ('insert','update','delete'):
                    conn.exec_driver_sql(f'DROP TRIGGER IF EXISTS desk_{table}_{op} ON "{table}"')
                kind='direct' if table in direct else 'article'
                conn.exec_driver_sql(f"CREATE OR REPLACE TRIGGER desk_{table}_change AFTER INSERT OR UPDATE OR DELETE ON \"{table}\" FOR EACH ROW EXECUTE FUNCTION n2d_desk_dirty('{column}','{kind}')")
            conn.exec_driver_sql("""CREATE OR REPLACE FUNCTION n2d_watch_evidence_dirty()
                RETURNS trigger LANGUAGE plpgsql AS $n2d$
                DECLARE old_case text; new_case text;
                BEGIN
                  IF TG_OP='UPDATE' AND NEW IS NOT DISTINCT FROM OLD THEN RETURN NULL; END IF;
                  IF TG_OP<>'INSERT' THEN old_case=OLD.case_id; END IF;
                  IF TG_OP<>'DELETE' THEN new_case=NEW.case_id; END IF;
                  INSERT INTO graphpending("key",token,written)
                    SELECT 'watch/'||w.watch,md5(random()::text||clock_timestamp()::text),''
                    FROM (SELECT DISTINCT watch FROM watchreview
                          WHERE case_key=ANY(ARRAY[old_case,new_case]) ORDER BY watch) w
                    ON CONFLICT("key") DO UPDATE SET token=excluded.token;
                  RETURN NULL;
                END $n2d$""")
            for table in ('researchquestion','researchclaim','researchreport'):
                for op in ('insert','update','delete'):
                    conn.exec_driver_sql(f'DROP TRIGGER IF EXISTS desk_{table}_{op} ON "{table}"')
                conn.exec_driver_sql(f'CREATE OR REPLACE TRIGGER desk_{table}_change AFTER INSERT OR UPDATE OR DELETE ON "{table}" FOR EACH ROW EXECUTE FUNCTION n2d_watch_evidence_dirty()')
        else:
            for table,column in {**direct,**indirect}.items():
                changed=' OR '.join(f'NEW."{c.name}" IS NOT OLD."{c.name}"' for c in SQLModel.metadata.tables[table].columns)
                for op in ('insert','update','delete'):
                    refs={'insert':['NEW'],'delete':['OLD'],'update':['OLD','NEW']}[op]
                    statements=[]
                    for ref in refs:
                        key=f'{ref}."{column}"'
                        lookup=f'SELECT {key}' if table in direct else f'SELECT event_key FROM articleeventlink WHERE article_key={key}'
                        statements.append(f'INSERT INTO deskdirty("key") {lookup} ON CONFLICT("key") DO NOTHING;')
                    conn.exec_driver_sql(f'DROP TRIGGER IF EXISTS desk_{table}_{op}')
                    guard=' WHEN '+changed if op=='update' else ''
                    conn.exec_driver_sql(f'CREATE TRIGGER desk_{table}_{op} AFTER {op} ON "{table}"{guard} BEGIN '+' '.join(statements)+' END')
            for table in ('researchquestion','researchclaim','researchreport'):
                changed=' OR '.join(f'NEW."{c.name}" IS NOT OLD."{c.name}"' for c in SQLModel.metadata.tables[table].columns)
                for op in ('insert','update','delete'):
                    cases={'insert':'NEW.case_id','delete':'OLD.case_id','update':'OLD.case_id,NEW.case_id'}[op]
                    guard=' WHEN '+changed if op=='update' else ''
                    conn.exec_driver_sql(f'DROP TRIGGER IF EXISTS desk_{table}_{op}')
                    conn.exec_driver_sql(f"""CREATE TRIGGER desk_{table}_{op} AFTER {op} ON "{table}"{guard} BEGIN
                        INSERT INTO graphpending("key",token,written)
                          SELECT 'watch/'||watch,hex(randomblob(16)),''
                          FROM (SELECT DISTINCT watch FROM watchreview WHERE case_key IN ({cases}))
                          WHERE 1=1 ON CONFLICT("key") DO UPDATE SET token=excluded.token; END""")
        if backfill and not conn.execute(text("SELECT 1 FROM deskmeta WHERE key='initial_index'")).first():
            conn.exec_driver_sql('INSERT INTO deskdirty("key") SELECT event_key FROM "event" WHERE 1=1 ON CONFLICT("key") DO NOTHING')
            conn.exec_driver_sql("INSERT INTO deskmeta(key,value) VALUES('initial_index','1') ON CONFLICT(key) DO NOTHING")


def enqueue_graph(s,key):
    # Preserve an in-flight writer's old written token; a new token is always pending.
    s.execute(text("""INSERT INTO graphpending(key,token,written) VALUES (:key,:token,'')
        ON CONFLICT(key) DO UPDATE SET token=EXCLUDED.token"""),dict(key=key,token=uuid4().hex))


def enqueue_dirty(s,key):
    s.execute(text('INSERT INTO deskdirty(key) VALUES (:key) ON CONFLICT(key) DO NOTHING'),dict(key=key))


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
        source.append(dict(actions=actions, key=art.article_key,title=art.title,domain=art.source_domain,provider=art.provider,
            revision=version.revision if version else 0,version_id=version.id if version else 0,
            content_hash=version.content_hash if version else '',
            collection_paths=sorted({art.provider or 'unknown'} | ({'gkg'} if gkg else set()) | ({'gdelt'} if discovery and not gkg and art.provider!='gkg' else set())),
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
    from .focus_models import FocusDefinition
    if s.get(FocusDefinition,w.key):
        from .focus_jobs import queue_event
        queue_event(s,w,card)
        return
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
                    enqueue_dirty(s,dest)
            enqueue_graph(s,'watch/'+link.watch)
            s.delete(link)
    else:
        old_sources=current.sources if current else ''
        card=s.merge(snapshot(s,event))
        for w in watches:
            before=s.get(WatchEvent,(w.key,key))
            if w.enabled:sync_auto(s,w,card)
            after=s.get(WatchEvent,(w.key,key))
            from .focus_models import FocusDefinition
            natural=s.get(FocusDefinition,w.key)
            if before or after:enqueue_graph(s,'watch/'+w.key)
            if natural and (not after or after.choice!='include'):
                # Automatic natural-topic membership is published later by
                # the judgement worker. Its notifications/research trigger
                # must use that accepted version, never a stale cached row.
                continue
            if after and after.choice!='exclude' and card.sources!=old_sources:
                from ..knowledge.service import note_change
                from ..extraction.schema import stable_id
                note_change(s,w.key,key,'updated' if before else 'added',card.title,stable_id(card.sources))
            today=utc_now_iso()[:10]
            if (w.enabled and w.auto_research and w.last_auto_day!=today and after
                    and after.choice!='exclude' and not after.reason.startswith('背景')
                    and card.priority>=2 and card.seen>=w.created and card.sources!=old_sources):
                make_review(s,w,key,'signal-'+today)
                w.last_auto_day=today;w.next_review=iso_after(w.review_days);s.add(w)
    enqueue_graph(s,'event/'+key)


def drain(engine,limit=60):
    """One bounded event per transaction; no model or graph I/O is performed here.

    Deleting the claimed row first matters: concurrent INSERT ON CONFLICT producers
    must wait for this transaction and can then re-enqueue their newer change.
    Deleting only at the end can discard an update that arrived during the snapshot.
    """
    processed=0
    postgres=engine.dialect.name=='postgresql'
    for _ in range(limit):
        with Session(engine) as s:
            if not postgres:
                # End the cheap idle read before acquiring SQLite's compatibility lock.
                if not s.exec(select(DeskDirty.key).limit(1)).first():break
                s.rollback()
                lock_news(s,'desk-drain')
            query=select(DeskDirty).order_by(DeskDirty.key).limit(1)
            if postgres:query=query.with_for_update(skip_locked=True)
            dirty=s.exec(query).first()
            if dirty is None:break
            key=dirty.key
            s.delete(dirty);s.flush()
            watch_keys=list(s.exec(select(Watch.key).order_by(Watch.key)))
            if postgres:
                lock_news(s,'desk-event:'+key,*('watch:'+k for k in watch_keys))
            watches=s.exec(select(Watch).order_by(Watch.key)).all()
            update_event(s,key,watches)
            s.commit()
            processed+=1
    return processed


def get_watch(s,key):
    row=s.get(Watch,key)
    if not row:raise KeyError('专题不存在')
    return row


def iso_after(days):
    return (datetime.now(timezone.utc)+timedelta(days=days)).isoformat(timespec='seconds').replace('+00:00','Z')


def save_watch(s,scope:Scope,key=None,expected=None,seed=''):
    ident=key or 'w_'+uuid4().hex
    lock_news(s,'watch:'+ident,'watch-name:'+scope.name)
    w=get_watch(s,key) if key else Watch(key=ident,**scope.model_dump())
    if key and expected!=w.version:raise TaskConflict('专题已被修改，请刷新后重试')
    from .focus_models import FocusDefinition
    definition=s.get(FocusDefinition,w.key) if key else None
    if definition and any(getattr(scope,field)!=getattr(w,field)
                          for field in ('name','scope','dimensions','exclude','question','kind')):
        raise TaskConflict('请通过文字描述调整此专题的关注范围')
    duplicate=s.exec(select(Watch.key).where(Watch.name==scope.name, Watch.key!=w.key)).first()
    if duplicate:raise ValueError('名称已存在')
    if seed and not s.exec(select(Event.id).where(Event.event_key==seed)).first():raise KeyError('事件不存在')
    rescope=(not key or (scope.scope,scope.dimensions,scope.exclude,scope.kind,scope.enabled)!=(w.scope,w.dimensions,w.exclude,w.kind,w.enabled))
    if key:w.version+=1
    if not key or w.auto_research!=scope.auto_research or w.review_days!=scope.review_days or w.enabled!=scope.enabled:
        w.next_review=iso_after(scope.review_days)
    for field,value in scope.model_dump().items():setattr(w,field,value)
    s.add(w);s.flush()
    from .focus_models import FocusBackfill
    backfill=s.get(FocusBackfill,w.key)
    if rescope or definition or (backfill and backfill.status in ('pending','running','paused')):
        from .focus_jobs import queue_backfill
        queue_backfill(s,w)
    elif backfill:
        backfill.scope_version=w.version
        s.add(backfill)
    if definition:
        definition.scope_version=w.version
        definition.updated=utc_now_iso()
        s.add(definition)
    if seed:link_event(s,w.key,seed,'include',commit=False)
    if not key:
        from ..extraction.models import FocusPoint
        from ..extraction.schema import stable_id
        from .rules import terms
        for label in terms(w.dimensions)[:6]:
            question=f'关于“{label}”，有哪些进展、证据和未解问题？'
            s.add(FocusPoint(key='point_'+stable_id(w.key,w.key,question.casefold()),watch=w.key,parent=w.key,
                text=question,created=utc_now_iso()))
    enqueue_graph(s,'watch/'+w.key)
    s.commit();s.refresh(w)
    return w


def link_event(s,key,event_key,action,*,commit=True):
    if commit:lock_news(s,'watch:'+key)
    w=get_watch(s,key)
    from ..events.workbench import canonical_event, require_event
    require_event(s,event_key)
    event_key=canonical_event(s,event_key)
    link=s.get(WatchEvent,(key,event_key))
    if action=='auto':
        if link:s.delete(link);s.flush()
        card=s.get(RadarEntry,event_key)
        if card and w.enabled:
            from .focus_models import FocusDefinition
            if s.get(FocusDefinition,w.key):
                from .focus_jobs import queue_event
                queue_event(s,w,card,force=True)
            else:sync_auto(s,w,card)
    elif action in ('include','exclude'):
        s.merge(WatchEvent(watch=key,event=event_key,choice=action,relevance=3,branch='人工整理',reason='手动关联' if action=='include' else '手动排除'))
        if action=='exclude':
            from sqlalchemy import delete
            from ..knowledge.models import PointMaterial
            from ..extraction.models import FocusPoint
            s.exec(delete(PointMaterial).where(PointMaterial.event==event_key,
                PointMaterial.point.in_(select(FocusPoint.key).where(FocusPoint.watch==key))))
    else:raise ValueError('无效关联操作')
    enqueue_graph(s,'watch/'+key)
    if commit:s.commit()


def memberships(s,event_key):
    return memberships_many(s,[event_key])[event_key]


def current_membership():
    """Hide obsolete automatic decisions immediately without a bulk DELETE.

    Legacy rules and manual choices remain visible. A description-based
    automatic member must have a completed judgement for the current scope.
    Indexed scalar EXISTS checks do not load source text or scan history.
    """
    from sqlalchemy import or_
    from .focus_models import FocusDefinition,FocusCandidate
    natural=select(FocusDefinition.watch).where(FocusDefinition.watch==WatchEvent.watch).correlate(WatchEvent).exists()
    accepted=select(FocusCandidate.key).join(Watch,Watch.key==FocusCandidate.watch).where(
        FocusCandidate.watch==WatchEvent.watch,FocusCandidate.event==WatchEvent.event,
        FocusCandidate.scope_version==Watch.version,FocusCandidate.status=='done',
        FocusCandidate.decision.in_(('core','background'))).correlate(WatchEvent).exists()
    return or_(WatchEvent.choice!='auto',~natural,accepted)


def memberships_many(s,event_keys):
    """One scalar query for a visible event page, including empty memberships."""
    result={key:[] for key in event_keys}
    if not result:return result
    rows=all_rows(s,select(WatchEvent.event,Watch.key,Watch.name,WatchEvent.reason)
        .select_from(WatchEvent).join(Watch,Watch.key==WatchEvent.watch)
        .where(WatchEvent.event.in_(result),WatchEvent.choice!='exclude',current_membership())
        .order_by(WatchEvent.event,Watch.name,Watch.key),name='desk.event_memberships')
    for event,key,name,reason in rows:
        result[event].append(dict(key=key,name=name,reason=reason))
    return result


def focus_entries(s,key,offset=0,limit=30,*,order='recent',branch=''):
    stmt=select(RadarEntry,WatchEvent.reason).join(WatchEvent,WatchEvent.event==RadarEntry.key).where(
        WatchEvent.watch==key,WatchEvent.choice!='exclude',current_membership())
    if branch:
        stmt=stmt.where(WatchEvent.branch==branch)
    total=count_keys(s,stmt,RadarEntry.key,name='desk.focus_entries.count')
    if order=='relevance':
        stmt=stmt.order_by(WatchEvent.relevance.desc())
    # The page displays these fields only. Large source/material JSON stays on
    # the event detail path; raiseload prevents accidental per-row lazy fetches.
    stmt=stmt.options(load_only(RadarEntry.key,RadarEntry.title,RadarEntry.band,
        RadarEntry.evidence,RadarEntry.seen,raiseload=True))
    rows=page_rows(s,stmt.order_by(RadarEntry.seen.desc(),RadarEntry.key),
        offset=offset,limit=limit,name='desk.focus_entries.page')
    return total,[(card,reason) for card,reason in rows]


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
