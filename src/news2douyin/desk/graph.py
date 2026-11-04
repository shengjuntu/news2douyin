"""Rebuildable FalkorDB projections. All graph writes are replayable by stable keys."""
import json
import os
import re
from uuid import uuid4
from sqlmodel import Session, select
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

    def query(self,graph,query,**params):
        if not self.url:
            raise ValueError('FalkorDB 未配置')
        if self.client is None:
            from redis import Redis
            self.client=Redis.from_url(self.url,decode_responses=True,socket_connect_timeout=2,socket_timeout=10)
        prefix=' '.join(key+'='+json.dumps(val,ensure_ascii=False) for key,val in params.items())
        return self.client.execute_command('GRAPH.QUERY',self.prefix+'_'+graph,
            ('CYPHER '+prefix+' ' if prefix else '')+query)

    def read(self,graph,query,**params):
        if self.client is None:
            self.query(graph,'RETURN 1')
        prefix=' '.join(key+'='+json.dumps(val,ensure_ascii=False) for key,val in params.items())
        result=self.client.execute_command('GRAPH.RO_QUERY',self.prefix+'_'+graph,
            ('CYPHER '+prefix+' ' if prefix else '')+query)
        return result[1] if len(result)>2 else []

    def bootstrap(self,engine):
        from .models import DeskMeta
        from ..storage.models import utc_now_iso
        with Session(engine) as s:
            lock_news(s)
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
        if not actual or not rows or rows[0][1]!='2.0':
            with Session(engine) as s:
                lock_news(s)
                for row in s.exec(select(GraphPending)):
                    row.written='';s.add(row)
                old=s.get(DeskMeta,'graph_analysis')
                if old:s.delete(old)
                s.commit()
            self.query('active_global','MATCH (m:GraphMeta {id:$id}) SET m.owner=$owner,m.schema=$schema',
                id='schema',owner=ident,schema='2.0')
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
                    lock_news(s)
                    for key in missing:enqueue_graph(s,'watch/'+key)
                    s.commit()
        self.last_ok=utc_now_iso()
        self.bootstrapped=True

    def event(self,data,key,places):
        q=lambda query,**params:self.query('active_global',query,**params)
        if not data:
            q('MATCH (e:Event {id:$k}) DETACH DELETE e',k=key)
            return
        q('MERGE (e:Event {id:$k}) SET e.title=$title,e.seen=$seen',k=key,title=data.title,seen=data.seen)
        stamp=uuid4().hex
        for src in json.loads(data.sources):
            q('MATCH (e:Event {id:$k}) MERGE (s:Article {id:$a}) SET s.domain=$d,s.title=$t,s.duplicate_root=$root '
              'MERGE (e)-[r:HAS_SOURCE]->(s) SET r.stamp=$v',k=key,a=src['key'],d=src['domain'],t=src['title'],
              root=src.get('duplicate_of') or src['key'],v=stamp)
            people=[x.casefold().strip() for x in src['persons'] if x.strip()]
            orgs=[x.casefold().strip() for x in src['orgs'] if x.strip()]
            entities=list(dict.fromkeys(people+orgs))
            q('MATCH (s:Article {id:$a}) UNWIND $xs AS name MERGE (n:Entity {id:name}) '
              'MERGE (s)-[r:MENTIONS]->(n) SET r.stamp=$v',a=src['key'],xs=entities,v=stamp)
            q('MATCH (:Article {id:$a})-[r:MENTIONS]->() WHERE r.stamp<>$v DELETE r',a=src['key'],v=stamp)
            for label, names in (('Person', people), ('Organization', orgs)):
                q('UNWIND $xs AS name MATCH (n:Entity {id:name}) SET n:'+label, xs=names)
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
        q('MATCH (a:Article) WHERE NOT (a)<-[:HAS_SOURCE]-() DETACH DELETE a')
        extraction_graph.clean(q)

    def watch(self,w,events,reviews,questions,claims,reports,points=(),point_reviews=(),point_material=()):
        graph='focus_'+w.key
        root=lambda query,**ps:self.query('active_global',query,**ps)
        local=lambda query,**ps:self.query(graph,query,**ps)
        stamp=uuid4().hex
        root('MERGE (f:Focus {id:$k}) SET f.name=$n,f.enabled=$on',k=w.key,n=w.name,on=w.enabled)
        aliases=list(dict.fromkeys([a for k in terms(w.scope) for a in (k,*ALIASES.get(k,()))]))
        root('MATCH (f:Focus {id:$k}) UNWIND $xs AS a MERGE (n:Entity {id:a}) MERGE (f)-[r:WATCHES]->(n) SET r.stamp=$v',k=w.key,xs=aliases,v=stamp)
        local('MERGE (q:Question {id:$k}) SET q.text=$t,q.scope=$scope',k=w.key,t=w.question or w.name,scope=w.scope)
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
        local('MATCH (a:Article) WHERE NOT (a)<-[:CITES]-() DETACH DELETE a')
        extraction_graph.clean(local)
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
        if not self.bootstrapped:self.bootstrap(engine)
        with Session(engine,expire_on_commit=False) as s:
            row=s.exec(select(GraphPending).where(GraphPending.token!=GraphPending.written)
                       .order_by(GraphPending.key).limit(1)).first()
            if not row:return False
            kind,key=row.key.split('/',1)
            card=s.get(RadarEntry,key) if kind=='event' else None
            places=s.exec(select(EventPlace).where(EventPlace.event==key)).all() if kind=='event' else []
            if kind=='watch':
                w=s.get(Watch,key)
                events=s.exec(select(RadarEntry,WatchEvent).join(WatchEvent,WatchEvent.event==RadarEntry.key)
                       .where(WatchEvent.watch==key,WatchEvent.choice!='exclude')).all()
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
        if kind=='event':self.event(card,key,places)
        else:self.watch(w,events,reviews,questions,claims,reports,points,point_reviews,point_material)
        with Session(engine) as s:
            lock_news(s)
            current=s.get(GraphPending,row.key)
            current.written=row.token if current.token==row.token else ''
            s.add(current);s.commit()
        self.last_error=''
        return True
