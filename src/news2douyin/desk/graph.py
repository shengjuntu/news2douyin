"""Rebuildable FalkorDB projections. All graph writes are replayable by stable keys."""
import json
import os
import re
import time
from contextlib import contextmanager
from threading import local
from uuid import uuid4
from sqlmodel import Session, select
from sqlalchemy import update, delete
from ..storage.articles import lock_news
from .models import GraphPending, RadarEntry, Watch, WatchEvent, WatchReview, EventPlace
from .rules import terms, ALIASES
from ..extraction import graph as extraction_graph

class GraphMirror:
    def __init__(self):
        self.url=os.getenv('NEWS2DOUYIN_FALKOR_URL','')
        self.prefix=os.getenv('NEWS2DOUYIN_GRAPH_PREFIX','n2d')
        if not re.fullmatch('[A-Za-z0-9_-]{1,48}',self.prefix):
            raise ValueError('NEWS2DOUYIN_GRAPH_PREFIX 只支持 1–48 个字母、数字、下划线或短横线')
        self.client=None
        self.last_error=''
        self.bootstrapped=False
        self.last_ok=''
        self._lease_state=local()
        self._projection_lease=None
        self._lease_seconds=max(30,min(3600,float(os.getenv('NEWS2DOUYIN_GRAPH_LEASE_SEC','120'))))
        from .graph_queue import GraphQueueMetrics
        self.metrics=GraphQueueMetrics()
        self._event_streak=0
        self._event_cursor=''
        self._watch_cursor=''
        self._queue_index_state={'state':'checking'}
        self._queue_index_checked=False
        self._last_bootstrap=0.0
        self.work_state='checking'
        self._gc_graph_cursor=''
        self._gc_global_turn=True

    @property
    def _projection_lease(self):
        return getattr(self._lease_state,'value',None)

    @_projection_lease.setter
    def _projection_lease(self,value):
        self._lease_state.value=value

    @contextmanager
    def exclusive(self,engine):
        """Lease external graph work without retaining a SQL transaction."""
        if self._projection_lease is not None:
            self._projection_lease.touch()
            yield True
            return
        from .leases import acquire
        with acquire(engine,'graph-projection/'+self.prefix,self._lease_seconds) as lease:
            if lease is None:
                yield False
                return
            self._projection_lease=lease
            try:yield True
            finally:self._projection_lease=None

    def _execute(self,command,graph,query,params,timeout=None):
        if self._projection_lease:self._projection_lease.touch()
        if not self.url:
            raise ValueError('FalkorDB 未配置')
        if self.client is None:
            from redis import Redis
            self.client=Redis.from_url(self.url,decode_responses=True,socket_connect_timeout=2,socket_timeout=10)
        prefix=' '.join(key+'='+json.dumps(val,ensure_ascii=False) for key,val in params.items())
        args=[command,self.prefix+'_'+graph,('CYPHER '+prefix+' ' if prefix else '')+query]
        if timeout is not None:
            args.extend(['TIMEOUT',max(1,int(timeout))])
        started=time.perf_counter()
        try:
            result=self.client.execute_command(*args)
        finally:
            self._lease_state.graph_ms=getattr(self._lease_state,'graph_ms',0.)+(time.perf_counter()-started)*1000
        if self._projection_lease:self._projection_lease.touch()
        return result

    def query(self,graph,query,*,_query_timeout_ms=None,**params):
        return self._execute('GRAPH.QUERY',graph,query,params,_query_timeout_ms)

    def read(self,graph,query,*,_query_timeout_ms=None,**params):
        result=self._execute('GRAPH.RO_QUERY',graph,query,params,_query_timeout_ms)
        return result[1] if len(result)>2 else []

    def explain(self,graph,query,**params):
        return self._execute('GRAPH.EXPLAIN',graph,query,params)

    def bootstrap(self,engine):
        with self.exclusive(engine) as acquired:
            if not acquired:return False
            self._bootstrap(engine)
            return True

    def _reset_gc_cursor(self,engine,graph):
        # Native node IDs belong to a graph instance, not a business identity.
        # A lost/recreated graph must not inherit cursors from the old space.
        from .graph_maintenance_models import GraphGcState
        with Session(engine) as s:
            s.exec(delete(GraphGcState).where(GraphGcState.namespace==self.prefix,GraphGcState.graph==graph))
            s.commit()
        self._gc_seek_plans={item for item in getattr(self,'_gc_seek_plans',set()) if item[0]!=graph}

    def _bootstrap(self,engine):
        from .models import DeskMeta
        from ..storage.models import utc_now_iso
        with Session(engine) as s:
            owner=s.get(DeskMeta,'graph_owner')
            if not owner:
                lock_news(s,'graph-owner')
                owner=s.get(DeskMeta,'graph_owner')
                if not owner:
                    owner=DeskMeta(key='graph_owner',value=uuid4().hex);s.add(owner);s.commit()
            ident=owner.value
        # Also detects a lost or newly configured graph after an app restart.
        self.query('active_global','MERGE (m:GraphMeta {id:$id})',id='schema')
        rows=self.read('active_global','MATCH (m:GraphMeta {id:$id}) RETURN m.owner,m.schema',id='schema')
        actual=rows[0][0] if rows else None
        if actual and actual!=ident:
            raise ValueError('graph_namespace_belongs_to_another_database')
        if not actual:self._reset_gc_cursor(engine,'active_global')
        if not actual or not rows or rows[0][1]!='2.0':
            with Session(engine) as s:
                lock_news(s,'graph-analysis')
                s.exec(update(GraphPending).where(GraphPending.written!='').values(written='')
                    .execution_options(synchronize_session=False))
                old=s.get(DeskMeta,'graph_analysis')
                if old:s.delete(old)
                s.commit()
            self.query('active_global','MATCH (m:GraphMeta {id:$id}) SET m.owner=$owner,m.schema=$schema',
                id='schema',owner=ident,schema='2.0')
            from .graph_indexes import reset
            reset(self)
        # Each focus is a separate graph key and can be lost independently.
        with Session(engine) as s:
            watches=list(s.exec(select(Watch.key)))
        if watches:
            with self.client.pipeline(transaction=False) as pipe:
                for key in watches:pipe.exists(self.prefix+'_focus_'+key)
                present=pipe.execute()
            missing=[key for key,exists in zip(watches,present) if not exists]
            if missing:
                from .service import enqueue_graph
                with Session(engine) as s:
                    lock_news(s,*('graph-queue/watch/'+key for key in sorted(missing)))
                    for key in missing:enqueue_graph(s,'watch/'+key)
                    s.commit()
                for key in missing:
                    self._reset_gc_cursor(engine,'focus_'+key)
        self.last_ok=utc_now_iso()
        self.bootstrapped=True
        self._last_bootstrap=time.monotonic()

    def event(self,data,key,places):
        q=lambda query,**params:self.query('active_global',query,**params)
        if not data:
            q('MATCH (e:Event {id:$k}) DETACH DELETE e',k=key)
            return
        stamp=uuid4().hex
        sources=json.loads(data.sources)
        source_rows=[[src['key'],src['domain'],src['title'],src.get('duplicate_of') or src['key']] for src in sources]
        # The Event MERGE precedes UNWIND, so zero-source events still exist;
        # cleanup remains in separate, unconditional scoped statements below.
        q('MERGE (e:Event {id:$k}) SET e.title=$title,e.seen=$seen '
          'WITH e UNWIND $sources AS src MERGE (s:Article {id:src[0]}) '
          'SET s.domain=src[1],s.title=src[2],s.duplicate_root=src[3] '
          'MERGE (e)-[r:HAS_SOURCE]->(s) SET r.stamp=$v',k=key,title=data.title,seen=data.seen,sources=source_rows,v=stamp)
        for src in sources:
            people=[x.casefold().strip() for x in src['persons'] if x.strip()]
            orgs=[x.casefold().strip() for x in src['orgs'] if x.strip()]
            entities=list(dict.fromkeys(people+orgs))
            if entities:
                q('MATCH (s:Article {id:$a}) UNWIND $xs AS name MERGE (n:Entity {id:name}) '
                  'MERGE (s)-[r:MENTIONS]->(n) SET r.stamp=$v',a=src['key'],xs=entities,v=stamp)
            q('MATCH (:Article {id:$a})-[r:MENTIONS]->() WHERE r.stamp<>$v DELETE r',a=src['key'],v=stamp)
            for label, names in (('Person', people), ('Organization', orgs)):
                if names:q('UNWIND $xs AS name MATCH (n:Entity {id:name}) SET n:'+label, xs=names)
            if src['mention_iso']:
                q('MATCH (a:Article {id:$a}) UNWIND $xs AS code MERGE (p:Place {iso:code}) '
                  'MERGE (a)-[r:MENTIONS_PLACE]->(p) SET r.stamp=$v',a=src['key'],xs=src['mention_iso'],v=stamp)
            q('MATCH (:Article {id:$a})-[r:MENTIONS_PLACE]->() WHERE r.stamp<>$v DELETE r',a=src['key'],v=stamp)
            extraction_graph.project(q,src['key'],src.get('actions',[]),stamp)
        for place in places:
            relation='ACTION_AT' if place.role=='action' else 'IMPACTS'
            q('MATCH (e:Event {id:$k}) MERGE (p:Place {iso:$iso}) MERGE (e)-[r:'+relation+
              ' {id:$id}]->(p) SET r.stamp=$v,r.article=$a,r.revision=$rev,r.quote=$quote',k=key,iso=place.iso,
              id=place.key,v=stamp,a=place.article,rev=place.revision,quote=place.quote)
        for relation in ('ACTION_AT','IMPACTS'):
            q('MATCH (:Event {id:$k})-[r:'+relation+']->() WHERE r.stamp<>$v DELETE r',k=key,v=stamp)
        q('MATCH (:Event {id:$k})-[r:HAS_SOURCE]->() WHERE r.stamp<>$v DELETE r',k=key,v=stamp)
        # Whole-graph orphan GC is independent maintenance, never part of an
        # event's success/ack path. Scoped stamp pruning above stays mandatory.

    def watch(self,w,events,reviews,questions,claims,reports,points=(),point_reviews=(),point_material=(),intent=None):
        graph='focus_'+w.key
        root=lambda query,**ps:self.query('active_global',query,**ps)
        local=lambda query,**ps:self.query(graph,query,**ps)
        stamp=uuid4().hex
        root('MERGE (f:Focus {id:$k}) SET f.name=$n,f.enabled=$on',k=w.key,n=w.name,on=w.enabled)
        aliases=list(dict.fromkeys([a for k in terms(w.scope) for a in (k,*ALIASES.get(k,()))]))
        root('MATCH (f:Focus {id:$k}) UNWIND $xs AS a MERGE (n:Entity {id:a}) MERGE (f)-[r:WATCHES]->(n) SET r.stamp=$v',k=w.key,xs=aliases,v=stamp)
        local('MERGE (q:Question {id:$k}) SET q.text=$t,q.scope=$scope',k=w.key,t=w.question or w.name,scope=w.scope)
        if intent:self.intent(local,w,intent,stamp)
        for point in points:
            local('MERGE (p:OutlinePoint {id:$id}) SET p.text=$text',id=point.key,text=point.text)
        local('MATCH (p:OutlinePoint) WHERE NOT p.id IN $ids DETACH DELETE p',ids=[p.key for p in points])
        local('MATCH ()-[r:HAS_SUBQUESTION]->() DELETE r')
        for point in points:
            parent_type = 'Question' if point.parent==w.key else 'OutlinePoint'
            local('MATCH (p:'+parent_type+' {id:$p}),(n:OutlinePoint {id:$id}) MERGE (p)-[:HAS_SUBQUESTION]->(n)',
                  p=point.parent,id=point.key)
        for c,l in events:
            root('MATCH (f:Focus {id:$w}) MERGE (e:Event {id:$e}) MERGE (f)-[r:TRACKS]->(e) SET r.stamp=$v,r.reason=$why,r.branch=$b,r.relevance=$p',
                 w=w.key,e=c.key,v=stamp,why=l.reason,b=l.branch,p=l.relevance)
            local('MATCH (q:Question {id:$w}) MERGE (e:Event {id:$e}) SET e.title=$t,e.stamp=$v '
                  'MERGE (q)-[r:HAS_MATERIAL]->(e) SET r.reason=$why',w=w.key,e=c.key,t=c.title,v=stamp,why=l.reason)
            for source in json.loads(c.sources):
                local('MATCH (e:Event {id:$e}) MERGE (a:Article {id:$a}) SET a.url=$url '
                      'MERGE (e)-[r:CITES]->(a) SET r.stamp=$v',
                      e=c.key,a=source['key'],url=source['url'],v=stamp)
                extraction_graph.project(local,source['key'],source.get('actions',[]),stamp)
        for relation in ('WATCHES','TRACKS'):
            root('MATCH (:Focus {id:$w})-[r:'+relation+']->() WHERE r.stamp<>$v DELETE r',w=w.key,v=stamp)
        local('MATCH (e:Event) WHERE e.stamp<>$v DETACH DELETE e',v=stamp)
        local('MATCH ()-[r:CITES]->() WHERE r.stamp<>$v DELETE r',v=stamp)
        # Focus graphs use the same bounded maintenance path as the global
        # graph; a growing topic must not reintroduce per-update global GC.
        local('MATCH (:OutlinePoint)-[r:RELEVANT_MATERIAL]->() DELETE r')
        for item in point_material:
            local('MATCH (p:OutlinePoint {id:$p}),(e:Event {id:$e}) MERGE (p)-[r:RELEVANT_MATERIAL]->(e) '
                  'SET r.reason=$reason',p=item.point,e=item.event,reason=item.reason)
        for r in reviews:
            local('MATCH (q:Question {id:$w}) MERGE (c:Investigation {id:$k}) SET c.status=$s,c.case_id=$c '
                  'MERGE (q)-[:CHECKED_BY]->(c)',w=w.key,k=r.key,s=r.status,c=r.case_key)

        for point,ticket in point_reviews:
            local('MATCH (p:OutlinePoint {id:$p}),(i:Investigation {id:$i}) MERGE (p)-[:CHECKED_BY]->(i)',
                  p=point,i=ticket)
        for question in questions:
            local('MATCH (i:Investigation {case_id:$case}) MERGE (q:ResearchQuestion {id:$id}) '
                  'SET q.text=$text,q.status=$s MERGE (i)-[:ASKS]->(q)',
                  case=question.case_id,id=question.question_id,text=question.text,s=question.status)
        for claim in claims:
            local('MATCH (i:Investigation {case_id:$case}) MERGE (c:Claim {id:$id}) '
                  'SET c.text=$text,c.kind=$kind MERGE (i)-[:FOUND]->(c)',
                  case=claim.case_id,id=claim.claim_id,text=claim.text,kind=claim.kind)
            local('MATCH (:Claim {id:$id})-[r:HAS_EVIDENCE]->() DELETE r',id=claim.claim_id)
            for ev in json.loads(claim.evidence_json):
                local('MATCH (c:Claim {id:$id}) MERGE (r:EvidenceRef {id:$ref}) '
                      'SET r.source=$source,r.paragraph=$para MERGE (c)-[b:HAS_EVIDENCE {kind:$kind}]->(r)',
                      id=claim.claim_id,ref=ev['source_id']+':'+ev['paragraph_id'],source=ev['source_id'],
                      para=ev['paragraph_id'],kind=ev['relation'])
        for question in questions:
            local('MATCH (q:ResearchQuestion {id:$id}) OPTIONAL MATCH (q)-[old:ANSWERED_BY]->() DELETE old',
                  id=question.question_id)
            local('MATCH (q:ResearchQuestion {id:$id}) UNWIND $ids AS ck MATCH (c:Claim {id:ck}) MERGE (q)-[:ANSWERED_BY]->(c)',
                  id=question.question_id,ids=json.loads(question.claim_ids_json))
        for report in reports:
            local('MATCH (i:Investigation {case_id:$case}) MERGE (r:Report {id:$id}) SET r.version=$version,r.title=$title '
                  'MERGE (i)-[:PRODUCED]->(r)',case=report.case_id,id=report.report_id,version=report.version,title=report.title)

    def intent(self,query,w,intent,stamp):
        """Project query intent separately from reported actions and claims."""
        from ..extraction.schema import stable_id, referent, entity_key
        spec=intent.get('spec',{})
        query('MERGE (s:InterestScope {id:$id}) SET s.description=$d,s.version=$v,'
              's.method=$m,s.time_start=$start,s.time_end=$end,s.includes=$yes,s.excludes=$no '
              'WITH s MATCH (q:Question {id:$id}) MERGE (q)-[:DEFINED_BY]->(s)',
              id=w.key,d=intent.get('description',spec.get('description','')),v=w.version,
              m=intent.get('method','rules'),start=spec.get('time_start',''),end=spec.get('time_end',''),
              yes=json.dumps(spec.get('include',[]),ensure_ascii=False),no=json.dumps(spec.get('exclude',[]),ensure_ascii=False))
        identifiers={}
        for entity in spec.get('entities',[]):
            key=w.key+'/'+entity['id']
            for name in (entity['id'],entity['name']):identifiers[name]=key
            query('MATCH (s:InterestScope {id:$watch}) MERGE (e:InterestEntity {id:$id}) '
                  'SET e.name=$name,e.entity_type=$type,e.aliases=$aliases,e.core=$core,e.stamp=$stamp '
                  'MERGE (s)-[:HAS_ANCHOR]->(e)',watch=w.key,id=key,name=entity['name'],type=entity.get('type','unknown'),
                  aliases=json.dumps(entity.get('aliases',[]),ensure_ascii=False),core=entity.get('core',True),stamp=stamp)
            curated=referent('actor',entity['name'])
            ref=curated or 'name:'+entity.get('type','unknown')+':'+entity_key(entity['name'])
            query('MATCH (i:InterestEntity {id:$id}),(r:Referent {id:$ref}) '
                  'MERGE (i)-[:MAPS_TO]->(r)',id=key,ref=ref)
        for relation in spec.get('relations',[]):
            key=w.key+'/'+stable_id('interest-pattern',relation)
            query('MATCH (s:InterestScope {id:$watch}) MERGE (p:InterestPattern {id:$id}) '
                  'SET p.subject=$subject,p.relation=$relation,p.object=$object,p.keywords=$keywords,p.stamp=$stamp '
                  'MERGE (s)-[:HAS_PATTERN]->(p)',watch=w.key,id=key,subject=relation.get('subject',''),
                  relation=relation.get('relation',''),object=relation.get('object',''),
                  keywords=json.dumps(relation.get('keywords',[]),ensure_ascii=False),stamp=stamp)
            for field,edge in (('subject','PATTERN_SUBJECT'),('object','PATTERN_OBJECT')):
                target=identifiers.get(relation.get(field,''))
                if target:query('MATCH (p:InterestPattern {id:$p}),(e:InterestEntity {id:$e}) MERGE (p)-[:'+edge+']->(e)',p=key,e=target)
        query('MATCH (:InterestScope {id:$watch})-[:HAS_PATTERN]->(p:InterestPattern) WHERE p.stamp<>$stamp DETACH DELETE p',watch=w.key,stamp=stamp)
        query('MATCH (:InterestScope {id:$watch})-[:HAS_ANCHOR]->(e:InterestEntity) WHERE e.stamp<>$stamp DETACH DELETE e',watch=w.key,stamp=stamp)

    def neighbors(self,key):
        if not self.url:return []
        if self.client is None:
            from redis import Redis
            self.client=Redis.from_url(self.url,decode_responses=True,socket_connect_timeout=2,socket_timeout=6)
        q=('CYPHER id='+json.dumps(key)+' MATCH (e:Event {id:$id})-[:HAS_SOURCE]->(:Article)-[:MENTIONS]->'
           '(n:Entity)<-[:MENTIONS]-(:Article)<-[:HAS_SOURCE]-(other:Event) WHERE other.id<>e.id '
           'RETURN DISTINCT other.id,n.id ORDER BY other.id,n.id LIMIT 20')
        result=self.client.execute_command('GRAPH.RO_QUERY',self.prefix+'_active_global',q)
        return result[1]

    def step(self,engine):
        if not self.url:return False
        if not self._queue_index_checked:
            # SQL index builds run outside both SQL transactions and the graph
            # lease. A large existing outbox must not block graph maintenance.
            from .graph_queue import ensure_queue_index
            self._queue_index_state=ensure_queue_index(engine)
            self._queue_index_checked=self._queue_index_state.get('state')=='ready'
            if not self._queue_index_checked:
                self.work_state='indexes'
                return False
        with self.exclusive(engine) as acquired:
            if not acquired:return False
            return self._step(engine)

    def prepare_indexes(self,engine,graph='active_global'):
        from .graph_indexes import ensure_step
        ready=ensure_step(self,engine,graph)
        if not ready:self.work_state='indexes'
        return ready

    def maintenance_step(self,engine):
        """One separately checkpointed orphan-maintenance block."""
        if not self.url or not self.bootstrapped:return False
        from . import graph_gc
        # Rotate known topic graphs without loading their articles/versions.
        if self._gc_global_turn:
            graph='active_global'
        else:
            with Session(engine) as s:
                watch=s.exec(select(Watch.key).where(Watch.key>self._gc_graph_cursor).order_by(Watch.key).limit(1)).first()
                if not watch:
                    watch=s.exec(select(Watch.key).order_by(Watch.key).limit(1)).first()
            if watch:
                graph='focus_'+watch
                self._gc_graph_cursor=watch
            else:graph='active_global'
        self._gc_global_turn=not self._gc_global_turn
        with self.exclusive(engine) as acquired:
            if not acquired:return False
            # Do not recreate a lost focus here: bootstrap must first notice
            # that loss and enqueue its material for replay.
            if not self.client or not self.client.exists(self.prefix+'_'+graph):return False
            previous=self.work_state
            try:
                if not self.prepare_indexes(engine,graph):return False
                return graph_gc.step(engine,self,graph)
            finally:
                self.work_state=previous

    def _step(self,engine):
        if not self.bootstrapped or time.monotonic()-self._last_bootstrap>=30:
            self.bootstrap(engine)
        if not self.prepare_indexes(engine):return False
        self.work_state='idle'
        started=time.perf_counter()
        self._lease_state.graph_ms=0.
        from .graph_queue import pick
        with Session(engine,expire_on_commit=False) as s:
            row=pick(s,prefer_watch=self._event_streak>=8,
                after_event=self._event_cursor,after_watch=self._watch_cursor)
            if not row:return False
            kind,key=row.key.split('/',1)
            card=s.get(RadarEntry,key) if kind=='event' else None
            places=s.exec(select(EventPlace).where(EventPlace.event==key)).all() if kind=='event' else []
            if kind=='watch':
                w=s.get(Watch,key)
        if kind=='watch':
            graph='focus_'+key
            if not self.client.exists(self.prefix+'_'+graph):
                self.query(graph,'RETURN 1')
                from .graph_indexes import reset
                reset(self,graph)
            if not self.prepare_indexes(engine,graph):
                # A single large/failed topic index may wait independently;
                # it must not pin the preferred slot and halt global events.
                self._watch_cursor=row.key
                self._event_streak=0
                return False
            # A large topic's material is read only once its own indexes are
            # ready. Index polling must never reload all of its source text.
            with Session(engine,expire_on_commit=False) as s:
                w=s.get(Watch,key)
                from .service import current_membership
                from .focus_jobs import definition
                intent=definition(s,key)
                events=s.exec(select(RadarEntry,WatchEvent).join(WatchEvent,WatchEvent.event==RadarEntry.key)
                       .where(WatchEvent.watch==key,WatchEvent.choice!='exclude',current_membership())).all()
                from ..extraction.outline import points as get_points, history
                points=get_points(s,key)
                from ..knowledge.models import PointMaterial
                point_material=s.exec(select(PointMaterial).where(PointMaterial.point.in_([p.key for p in points]))).all()
                point_reviews=[(link.point,link.ticket) for link,_ in history(s,key)]
                from ..research.models import ResearchQuestion, ResearchClaim, ResearchReport
                reviews=s.exec(select(WatchReview).where(WatchReview.watch==key)).all()
                cases=select(WatchReview.case_key).where(WatchReview.watch==key)
                questions,claims,reports=[s.exec(select(model).where(model.case_id.in_(cases))).all()
                    for model in (ResearchQuestion,ResearchClaim,ResearchReport)]
        self.work_state='projection'
        try:
            if kind=='event':self.event(card,key,places)
            else:self.watch(w,events,reviews,questions,claims,reports,points,point_reviews,point_material,intent=intent)
            with Session(engine) as s:
                lease=self._projection_lease
                lock_news(s,'graph-queue/'+row.key,'lease/'+lease.key)
                lease.confirm(s)
                # A newer event/source version or a reclaimed lease cannot be acknowledged.
                result=s.exec(update(GraphPending).where(GraphPending.key==row.key,GraphPending.token==row.token)
                    .values(written=row.token).execution_options(synchronize_session=False))
                acknowledged=result.rowcount==1
                if acknowledged:
                    from .models import DeskMeta
                    epoch=s.get(DeskMeta,'graph_projection_epoch_1')
                    if epoch is None:epoch=DeskMeta(key='graph_projection_epoch_1')
                    epoch.value=uuid4().hex
                    s.add(epoch)
                s.commit()
        except Exception:
            elapsed=(time.perf_counter()-started)*1000
            graph_ms=getattr(self._lease_state,'graph_ms',0.)
            self.metrics.record(kind,elapsed,failed=True,graph_ms=graph_ms,sql_ms=max(0.,elapsed-graph_ms))
            raise
        finally:
            # A perpetually changing first key must not starve other events or
            # topics. Cursor fairness counts attempts; success metrics do not.
            if kind=='event':
                self._event_cursor=row.key
                self._event_streak+=1
            else:
                self._watch_cursor=row.key
                self._event_streak=0
        elapsed=(time.perf_counter()-started)*1000
        graph_ms=getattr(self._lease_state,'graph_ms',0.)
        self.metrics.record(kind,elapsed,acknowledged=acknowledged,stale=not acknowledged,
            graph_ms=graph_ms,sql_ms=max(0.,elapsed-graph_ms))
        self.last_error=''
        return True
