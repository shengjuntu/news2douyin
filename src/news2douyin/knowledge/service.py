"""Publish complete graph analyses, reconcile focus membership and keep change history."""
import json
from collections import defaultdict
from sqlalchemy import delete, func
from sqlmodel import Session, select
from ..desk.models import DeskMeta, GraphPending, RadarEntry, Watch, WatchEvent
from ..desk import service as desk
from ..desk.focus_lifecycle import visible_watch, is_deleted, require_active
from ..desk.rules import match, hits, terms, norm, focus_context
from ..extraction.schema import stable_id
from ..extraction.models import FocusPoint
from ..storage.articles import lock_news
from ..storage.models import utc_now_iso
from .models import GraphMetric, GraphRelation, FocusChange, FocusVisit, PointMaterial
from .algorithms import analyze, pagerank


def initialize(engine):
    with Session(engine) as s:
        lock_news(s,'knowledge-schema')
        if not s.get(DeskMeta,'knowledge_schema_2'):
            for key in s.exec(select(RadarEntry.key)):
                desk.enqueue_graph(s,'event/'+key)
            for key in s.exec(select(Watch.key).where(visible_watch())):
                desk.enqueue_graph(s,'watch/'+key)
            s.add(DeskMeta(key='knowledge_schema_2',value='2.0'))
        s.commit()


def signature(s):
    # Conditional projection acknowledgements update this watermark in the
    # same SQL transaction. Never materialize a half-million-row outbox merely
    # to decide whether the graph analysis is current.
    epoch=s.get(DeskMeta,'graph_projection_epoch_1')
    pending=s.exec(select(GraphPending.key).where(GraphPending.token!=GraphPending.written,
        ~GraphPending.key.startswith('official/')).limit(1)).first()
    return epoch.value if epoch else 'projection-epoch-0', pending is not None


def status(s, graph):
    pending, analysis_pending = s.exec(select(func.count(),func.count().filter(~GraphPending.key.startswith('official/')))
        .select_from(GraphPending).where(GraphPending.token!=GraphPending.written)
        .execution_options(n2d_query_name='knowledge.status.pending_count')).one()
    stamp = s.get(DeskMeta,'graph_analysis')
    analysis = json.loads(stamp.value) if stamp else {}
    state = 'unconfigured' if not graph.url else 'error' if graph.last_error else 'pending' if pending or not analysis or not graph.last_ok else 'ready'
    from ..desk.graph_indexes import status_summary
    from ..desk.graph_gc import summary as gc_summary
    from ..desk.graph_focus import summary as focus_summary
    indexes=status_summary(graph)
    gc=gc_summary(s,graph)
    if getattr(graph,'_maintenance_error','') and not gc.get('error') and gc.get('state') not in ('paused','disabled','unconfigured'):
        gc=dict(gc,state='error',error='图维护等待重试（'+graph._maintenance_error+'）')
    return dict(required=True,state=state,pending=pending,error=graph.last_error,
                analyzed_at=analysis.get('at',''),count=analysis.get('count',0),
                truncated=analysis.get('truncated',False),generation=analysis.get('generation',''),
                indexes=indexes,gc=gc,projection=graph.metrics.snapshot(pending=pending),
                focus_projection=focus_summary(s,graph),
                queue_index=dict(graph._queue_index_state),
                analysis_state='waiting_for_projection' if analysis_pending else 'ready' if analysis else 'not_analyzed')


def note_change(s, watch, event, kind, text, version):
    key=stable_id(watch,event,kind,version)
    if not s.get(FocusChange,key):
        s.add(FocusChange(key=key,watch=watch,event=event,kind=kind,text=text))


def reconcile(s, metrics, edges, generation):
    cards={c.key:c for c in s.exec(select(RadarEntry).where(RadarEntry.key.in_(metrics)))}
    adjacency=defaultdict(list)
    for a,b,weight in edges:
        adjacency[a].append((b,weight));adjacency[b].append((a,weight))
    for w in s.exec(select(Watch).where(Watch.enabled==True,visible_watch())):
        from ..desk.focus_models import FocusDefinition
        intent_definition=s.get(FocusDefinition,w.key)
        links={r.event:r for r in s.exec(select(WatchEvent).where(WatchEvent.watch==w.key,desk.current_membership()))}
        seeds={k:3 if l.choice=='include' else 1 for k,l in links.items()
               if k in cards and l.choice!='exclude' and not l.reason.startswith(('图关联','背景'))
               and (not intent_definition or l.choice=='include' or l.relevance>=2)}
        related=pagerank(cards,edges,seeds=seeds) if seeds else {}
        # Limit expansion by personalized rank, instead of using rank as a mere nonzero flag.
        expansion=set(sorted((k for k in cards if k not in seeds and any(p in seeds for p,_ in adjacency[k])),
            key=lambda k:(-related.get(k,0),k))[:30])
        if intent_definition:
            # FalkorDB-derived paths only nominate candidates. They do not
            # bypass the topic's evidence-based model judgement or override
            # manual choices. The existing visualization window is not the
            # historical backfill's coverage boundary.
            from ..desk.focus_jobs import queue_event
            for key in sorted(expansion):
                if links.get(key) and links[key].choice in ('include','exclude'):continue
                via=sorted((p,weight) for p,weight in adjacency[key] if p in seeds)
                if via:
                    queue_event(s,w,cards[key],recall_extra=dict(channels=['graph'],
                        paths=[dict(seed=p,event=key,weight=weight) for p,weight in via[:3]],
                        reason='与核心事件存在图关联，相关性待核'))
            continue
        changed=False
        for key, card in cards.items():
            old=links.get(key)
            if old and old.choice in ('exclude','include'):
                continue
            direct=match(w,card)
            via=sorted((p,weight) for p,weight in adjacency[key] if p in seeds)
            # One-hop bounded expansion. Scope dimensions/exclusions remain hard gates;
            # person watches require an explicit role match and never expand by association.
            text=card.headings+'\n'+card.material
            allowed=(w.kind!='人物' and not hits(terms(w.exclude),text)
                     and (not terms(w.dimensions) or hits(terms(w.dimensions),text)))
            reason=direct
            if not reason and key in expansion and via and allowed and related.get(key,0)>0:
                reason='图关联 · 与「'+cards[via[0][0]].title[:80]+'」共享有出处的行动参与者或行动匹配线索'
            if reason:
                relevance,branch=focus_context(w,card,reason) if direct else (1,'关联进展 · 待核')
                if not old or (old.reason,old.branch,old.relevance)!=(reason,branch,relevance):
                    s.merge(WatchEvent(watch=w.key,event=key,choice='auto',reason=reason,
                        branch=branch,relevance=relevance,first_seen=old.first_seen if old else utc_now_iso()))
                    note_change(s,w.key,key,'related' if not direct else 'added',card.title,generation)
                    changed=True
            elif old:
                s.delete(old);changed=True
                note_change(s,w.key,key,'removed','关联依据已变化：'+card.title,generation)
        s.flush()
        members={r.event for r in s.exec(select(WatchEvent).where(WatchEvent.watch==w.key,WatchEvent.choice!='exclude'))}
        points=s.exec(select(FocusPoint).where(FocusPoint.watch==w.key)).all()
        for point in points:
            matches=set()
            # Explicit word/bigram relevance is a routing hint, never a support relationship.
            from ..extraction.survey import terms as tokens
            wanted=tokens(point.text)
            if wanted:
                ranked=sorted(((len(wanted & tokens(cards[k].headings+' '+cards[k].material)),k)
                    for k in members if k in cards),reverse=True)
                matches={k for score,k in ranked[:20] if score>=2}
            current={r.event:r for r in s.exec(select(PointMaterial).where(PointMaterial.point==point.key))}
            for k in matches-current.keys():
                s.add(PointMaterial(key=stable_id(point.key,k),point=point.key,event=k,
                    reason='问题词项与材料重合，相关性待核',generation=generation));changed=True
            for k in current.keys()-matches:
                s.delete(current[k]);changed=True
        if changed:
            desk.enqueue_graph(s,'watch/'+w.key)


def refresh(engine, graph):
    with graph.exclusive(engine) as acquired:
        if not acquired:return False
        return _refresh(engine,graph)


def _refresh(engine, graph):
    with Session(engine) as s:
        token,pending=signature(s)
        previous=s.get(DeskMeta,'graph_analysis')
        if pending or (previous and json.loads(previous.value).get('token')==token):
            return False
    # A read-only graph query is deliberately the only source for this analysis.
    rows=graph.read('active_global','MATCH (e:Event) RETURN e.id,e.title,e.seen ORDER BY e.seen DESC,e.id LIMIT 5001')
    truncated=len(rows)>5000
    events=rows[:5000]
    ids=[row[0] for row in events]
    mentions=graph.read('active_global',
        'MATCH (e:Event)-[:HAS_SOURCE]->(a:Article) WHERE e.id IN $ids '
        'OPTIONAL MATCH (a)-[:DESCRIBES]->(m:EventMention) OPTIONAL MATCH (m)-[:HAS_MATCH_KEY]->(g:OccurrenceCandidate) '
        'RETURN e.id,m.id,m.kind,m.status,m.date,g.id,a.id,a.duplicate_root,a.domain',ids=ids)
    participants=graph.read('active_global',
        'MATCH (e:Event)-[:HAS_SOURCE]->(:Article)-[:DESCRIBES]->(m:EventMention)'
        '-[:HAS_ARGUMENT]->(a:Argument)-[:REFERS_TO]->(r:Referent) WHERE e.id IN $ids '
        'RETURN e.id,r.id,r.name,a.role,m.id',ids=ids)
    metrics,links,edges=analyze(events,mentions,participants)
    generation=stable_id(token,metrics,links_to_json(links))
    # Store graph results first, and publish the SQL read cache only after all writes succeed.
    graph.query('active_global','MATCH ()-[r:RELATED_EVENT]->() DELETE r')
    edge_rows=[[a,b,min(link['weight'],5.),json.dumps(link['evidence'],ensure_ascii=False)] for (a,b),link in sorted(links.items())]
    for start in range(0,len(edge_rows),200):
        graph.query('active_global','UNWIND $rows AS row MATCH (a:Event {id:row[0]}),(b:Event {id:row[1]}) '
            'MERGE (a)-[r:RELATED_EVENT]->(b) SET r.weight=row[2],r.evidence=row[3],r.generation=$g',
            rows=edge_rows[start:start+200],g=generation)
    metric_rows=[[key,m['score'],m['community']] for key,m in sorted(metrics.items())]
    for start in range(0,len(metric_rows),200):
        graph.query('active_global','UNWIND $rows AS row MATCH (e:Event {id:row[0]}) '
            'SET e.score=row[1],e.community=row[2],e.analysis_generation=$g',rows=metric_rows[start:start+200],g=generation)
    with Session(engine) as s:
        lease=graph._projection_lease
        watches=list(s.exec(select(Watch.key).where(visible_watch()).order_by(Watch.key)))
        lock_news(s,'graph-analysis','lease/'+lease.key,*('watch:'+key for key in watches))
        lease.confirm(s)
        current,pending=signature(s)
        if current!=token or pending:
            return True
        s.exec(delete(GraphMetric));s.exec(delete(GraphRelation))
        for key,data in metrics.items():
            s.add(GraphMetric(event=key,generation=generation,score=data['score'],community=data['community'],data=json.dumps(data,ensure_ascii=False)))
        for (a,b),link in links.items():
            s.add(GraphRelation(key=stable_id(a,b),source=a,target=b,kind='related',weight=min(link['weight'],5.),
                evidence=json.dumps(link['evidence'],ensure_ascii=False),generation=generation))
        reconcile(s,metrics,edges,generation)
        s.merge(DeskMeta(key='graph_analysis',value=json.dumps(dict(token=token,generation=generation,
            at=utc_now_iso(),count=len(events),truncated=truncated))))
        s.commit()
    return True


def update_outline(engine):
    """Persist questions actually emitted by research agents; do not invent conclusions."""
    from ..research.models import ResearchQuestion
    from ..desk.models import WatchReview
    with Session(engine) as s:
        rows=s.exec(select(ResearchQuestion,WatchReview).join(WatchReview,WatchReview.case_key==ResearchQuestion.case_id)
            .order_by(ResearchQuestion.updated_at.desc()).limit(120)).all()
        if not rows:return
        missing=[]
        for q,ticket in rows:
            key='point_'+stable_id(ticket.watch,ticket.watch,' '.join(q.text.split()).casefold())
            if not s.get(FocusPoint,key):missing.append((key,q,ticket))
        if not missing:return
        grouped=defaultdict(list)
        for key,q,ticket in missing:grouped[ticket.watch].append((key,q.text,q.updated_at))
    for watch,items in sorted(grouped.items()):
        with Session(engine) as s:
            lock_news(s,'watch:'+watch)
            if not s.get(Watch,watch) or is_deleted(s,watch):continue
            count=s.exec(select(func.count()).select_from(FocusPoint).where(FocusPoint.watch==watch)).one()
            changed=False
            for key,text,created in items:
                if count>=64:break
                if s.get(FocusPoint,key):continue
                s.add(FocusPoint(key=key,watch=watch,parent=watch,text=text[:400],created=created))
                count+=1;changed=True
            if changed:desk.enqueue_graph(s,'watch/'+watch)
            s.commit()


def links_to_json(links):
    return [(a,b,v) for (a,b),v in sorted(links.items())]


def graph_view(s, event='', watch=''):
    if watch:require_active(s,watch)
    stmt=select(GraphMetric)
    if event:
        relations=s.exec(select(GraphRelation).where((GraphRelation.source==event)|(GraphRelation.target==event))).all()
        keys={event}|{r.source for r in relations}|{r.target for r in relations}
        stmt=stmt.where(GraphMetric.event.in_(keys))
    elif watch:
        keys=set(s.exec(select(WatchEvent.event).where(WatchEvent.watch==watch,WatchEvent.choice!='exclude',desk.current_membership())))
        stmt=stmt.where(GraphMetric.event.in_(keys))
        relations=s.exec(select(GraphRelation).where(GraphRelation.source.in_(keys),GraphRelation.target.in_(keys))).all()
    else:
        rows=s.exec(stmt.order_by(GraphMetric.score.desc()).limit(60)).all()
        keys={r.event for r in rows}
        relations=s.exec(select(GraphRelation).where(GraphRelation.source.in_(keys),GraphRelation.target.in_(keys))).all()
        stmt=stmt.where(GraphMetric.event.in_(keys))
    metrics=s.exec(stmt.order_by(GraphMetric.score.desc()).limit(60)).all()
    keys={m.event for m in metrics}
    titles={c.key:c.title for c in s.exec(select(RadarEntry).where(RadarEntry.key.in_(keys)))}
    return dict(nodes=[dict(id=m.event,title=titles.get(m.event,m.event),**json.loads(m.data)) for m in metrics],
        edges=[dict(source=r.source,target=r.target,weight=r.weight,evidence=json.loads(r.evidence))
               for r in relations if r.source in keys and r.target in keys],limit=60)
