"""Bounded topic mirror passes with durable cursors and material fingerprints.

A pass keeps its captured outbox token even when new work arrives. Only the
final conditional outbox UPDATE may acknowledge it. Changes behind a cursor
therefore cause another pass, while unchanged material skips all graph writes.
No SQL transaction is retained across graph I/O. A checkpoint is saved only
under the graph lease; a crash before it commits safely replays the batch.
"""
import hashlib
import json
import os
import time
from uuid import uuid4

from sqlalchemy import delete
from sqlalchemy.orm import load_only
from sqlmodel import Session, select

from .graph_focus_models import GraphFocusReceipt, GraphFocusState
from .models import DeskMeta, RadarEntry, Watch, WatchEvent, WatchReview
from ..storage.models import utc_now_iso
from ..storage.articles import lock_news
from ..extraction import graph as extraction_graph

PHASES = ('header', 'points', 'events', 'point_material', 'reviews', 'point_reviews',
          'questions', 'claims', 'answers', 'reports', 'prune', 'finish', 'complete')


def _integer(name, default, low, high):
    raw = os.getenv(name, str(default))
    try: value = int(raw)
    except ValueError: raise ValueError(name+' must be an integer') from None
    if not low <= value <= high: raise ValueError(name+' is outside the supported range')
    return value


def _digest(data):
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode()).hexdigest()


def _key(session, expression):
    # These are ordinary keyset pages, not a prefix sentinel range. Keep
    # comparisons and ORDER BY in the existing PK/index collation.
    return expression


def _query(watch, phase):
    """Return one query and its stable business key; never load all topic text."""
    from .service import current_membership
    from ..extraction.models import FocusPoint, PointReview
    from ..knowledge.models import PointMaterial
    from ..research.models import ResearchQuestion, ResearchClaim, ResearchReport
    if phase == 'points':
        return select(FocusPoint).where(FocusPoint.watch == watch), FocusPoint.key
    if phase == 'events':
        return select(RadarEntry, WatchEvent).join(WatchEvent, WatchEvent.event == RadarEntry.key).where(
            WatchEvent.watch == watch, WatchEvent.choice != 'exclude', current_membership()).options(
                load_only(RadarEntry.key, RadarEntry.title, RadarEntry.sources)), WatchEvent.event
    if phase == 'point_material':
        return select(PointMaterial).join(FocusPoint, FocusPoint.key == PointMaterial.point).join(
            WatchEvent, WatchEvent.event == PointMaterial.event).join(RadarEntry, RadarEntry.key == WatchEvent.event).where(
            FocusPoint.watch == watch, WatchEvent.watch == watch, WatchEvent.choice != 'exclude', current_membership()), PointMaterial.key
    if phase == 'reviews':
        return select(WatchReview).where(WatchReview.watch == watch), WatchReview.key
    if phase == 'point_reviews':
        return select(PointReview).join(WatchReview, WatchReview.key == PointReview.ticket).join(
            FocusPoint, FocusPoint.key == PointReview.point).where(WatchReview.watch == watch,
            FocusPoint.watch == watch), PointReview.ticket
    model = {'questions': ResearchQuestion, 'claims': ResearchClaim,
             'answers': ResearchQuestion, 'reports': ResearchReport}[phase]
    key = {'questions': ResearchQuestion.question_id, 'claims': ResearchClaim.claim_id,
           'answers': ResearchQuestion.question_id, 'reports': ResearchReport.report_id}[phase]
    cases = select(WatchReview.case_key).where(WatchReview.watch == watch)
    return select(model).where(model.case_id.in_(cases)), key


def _page(session, watch, phase, cursor, through, size):
    query, column = _query(watch, phase)
    key = _key(session, column)
    if cursor: query = query.where(key > cursor)
    if through: query = query.where(key <= through)
    return session.exec(query.order_by(key).limit(size).execution_options(
        n2d_query_name='desk.graph_focus.page.'+phase)).all()


def _upper(session, watch, phase):
    query, column = _query(watch, phase)
    return session.execute(query.with_only_columns(column).order_by(_key(session, column).desc()).limit(1)
        .execution_options(n2d_query_name='desk.graph_focus.highwater.'+phase)).scalar() or ''


def _data(phase, item):
    if phase == 'events':
        card, link = item
        return card.key, [card.title, card.sources, link.reason, link.branch, link.relevance], {}
    fields = {
        'points': ('key', ('parent', 'text')),
        'point_material': ('key', ('point', 'event', 'reason')),
        'reviews': ('key', ('status', 'case_key')),
        'point_reviews': ('ticket', ('point',)),
        'questions': ('question_id', ('case_id', 'text', 'status')),
        'claims': ('claim_id', ('case_id', 'text', 'kind', 'evidence_json')),
        'answers': ('question_id', ('claim_ids_json',)),
        'reports': ('report_id', ('case_id', 'version', 'title')),
    }
    key, names = fields[phase]
    locator = {name: getattr(item, name) for name in ('point', 'event') if hasattr(item, name)}
    return getattr(item, key), [getattr(item, name) for name in names], locator


def _project(mirror, w, phase, item, stamp, *, first=False):
    local = lambda cypher, **params: mirror.query('focus_'+w.key, cypher, **params)
    root = lambda cypher, **params: mirror.query('active_global', cypher, **params)
    if phase == 'events':
        card, link = item
        root('MATCH (f:Focus {id:$w}) MERGE (e:Event {id:$e}) MERGE (f)-[r:TRACKS]->(e) '
             'SET r.reason=$why,r.branch=$b,r.relevance=$p,r.stamp=$stamp',
             w=w.key, e=card.key, why=link.reason, b=link.branch, p=link.relevance, stamp=stamp)
        sources = json.loads(card.sources)
        local('MATCH (q:Question {id:$w}) MERGE (e:Event {id:$e}) SET e.title=$t,e.stamp=$v '
              'MERGE (q)-[r:HAS_MATERIAL]->(e) SET r.reason=$why '
              'WITH e UNWIND $sources AS src MERGE (a:Article {id:src[0]}) SET a.url=src[1] '
              'MERGE (e)-[c:CITES]->(a) SET c.stamp=$v',
              w=w.key, e=card.key, t=card.title, why=link.reason,
              sources=[[src['key'], src['url']] for src in sources], v=stamp)
        for source in sources:
            extraction_graph.project(local, source['key'], source.get('actions', []), stamp)
        local('MATCH (:Event {id:$e})-[r:CITES]->() WHERE r.stamp<>$v DELETE r', e=card.key, v=stamp)
    elif phase == 'points':
        if first:
            # One-time adoption removes legacy associations before the later
            # material/review phases restore their current SQL contents.
            local('MATCH (:OutlinePoint {id:$id})-[r:RELEVANT_MATERIAL|CHECKED_BY]->() DELETE r', id=item.key)
        local('MERGE (p:OutlinePoint {id:$id}) SET p.text=$text '
              'WITH p OPTIONAL MATCH ()-[old:HAS_SUBQUESTION]->(p) DELETE old', id=item.key, text=item.text)
        label = 'Question' if item.parent == w.key else 'OutlinePoint'
        local('MERGE (p:'+label+' {id:$parent}) WITH p MATCH (n:OutlinePoint {id:$id}) '
              'MERGE (p)-[:HAS_SUBQUESTION]->(n)', parent=item.parent, id=item.key)
    elif phase == 'point_material':
        local('MATCH (p:OutlinePoint {id:$p}),(e:Event {id:$e}) MERGE (p)-[r:RELEVANT_MATERIAL]->(e) '
              'SET r.reason=$why', p=item.point, e=item.event, why=item.reason)
    elif phase == 'reviews':
        local('MATCH (q:Question {id:$w}) MERGE (i:Investigation {id:$id}) '
              'SET i.status=$status,i.case_id=$case MERGE (q)-[:CHECKED_BY]->(i)',
              w=w.key, id=item.key, status=item.status, case=item.case_key)
    elif phase == 'point_reviews':
        local('MATCH (i:Investigation {id:$id}) OPTIONAL MATCH (:OutlinePoint)-[old:CHECKED_BY]->(i) DELETE old', id=item.ticket)
        local('MATCH (p:OutlinePoint {id:$point}),(i:Investigation {id:$id}) MERGE (p)-[:CHECKED_BY]->(i)', point=item.point, id=item.ticket)
    elif phase == 'questions':
        local('MATCH (i:Investigation {case_id:$case}) MERGE (q:ResearchQuestion {id:$id}) '
              'SET q.text=$text,q.status=$status MERGE (i)-[:ASKS]->(q)',
              case=item.case_id, id=item.question_id, text=item.text, status=item.status)
    elif phase == 'claims':
        local('MATCH (i:Investigation {case_id:$case}) MERGE (c:Claim {id:$id}) '
              'SET c.text=$text,c.kind=$kind MERGE (i)-[:FOUND]->(c) '
              'WITH c OPTIONAL MATCH (c)-[old:HAS_EVIDENCE]->() DELETE old',
              case=item.case_id, id=item.claim_id, text=item.text, kind=item.kind)
        for ev in json.loads(item.evidence_json):
            local('MATCH (c:Claim {id:$id}) MERGE (r:EvidenceRef {id:$ref}) '
                  'SET r.source=$source,r.paragraph=$para MERGE (c)-[:HAS_EVIDENCE {kind:$kind}]->(r)',
                  id=item.claim_id, ref=ev['source_id']+':'+ev['paragraph_id'],
                  source=ev['source_id'], para=ev['paragraph_id'], kind=ev['relation'])
    elif phase == 'answers':
        local('MATCH (q:ResearchQuestion {id:$id}) OPTIONAL MATCH (q)-[old:ANSWERED_BY]->() DELETE old', id=item.question_id)
        local('MATCH (q:ResearchQuestion {id:$id}) UNWIND $claims AS ck MATCH (c:Claim {id:ck}) '
              'MERGE (q)-[:ANSWERED_BY]->(c)', id=item.question_id, claims=json.loads(item.claim_ids_json))
    elif phase == 'reports':
        local('MATCH (i:Investigation {case_id:$case}) MERGE (r:Report {id:$id}) '
              'SET r.version=$version,r.title=$title MERGE (i)-[:PRODUCED]->(r)',
              case=item.case_id, id=item.report_id, version=item.version, title=item.title)


def _remove(mirror, watch, receipt):
    local = lambda cypher, **params: mirror.query('focus_'+watch, cypher, **params)
    key = receipt.item
    if receipt.kind == 'events':
        mirror.query('active_global', 'MATCH (:Focus {id:$w})-[r:TRACKS]->(:Event {id:$e}) DELETE r', w=watch, e=key)
        local('MATCH (e:Event {id:$e}) DETACH DELETE e', e=key)
    elif receipt.kind == 'point_material':
        locator = json.loads(receipt.locator)
        local('MATCH (:OutlinePoint {id:$p})-[r:RELEVANT_MATERIAL]->(:Event {id:$e}) DELETE r',
              p=locator['point'], e=locator['event'])
    elif receipt.kind == 'point_reviews':
        locator = json.loads(receipt.locator)
        local('MATCH (:OutlinePoint {id:$p})-[r:CHECKED_BY]->(:Investigation {id:$id}) DELETE r', p=locator['point'], id=key)
    elif receipt.kind in ('points', 'reviews', 'questions', 'claims', 'reports'):
        label = {'points':'OutlinePoint', 'reviews':'Investigation', 'questions':'ResearchQuestion',
                 'claims':'Claim', 'reports':'Report'}[receipt.kind]
        local('MATCH (n:'+label+' {id:$id}) DETACH DELETE n', id=key)
    elif receipt.kind == 'answers':
        local('MATCH (:ResearchQuestion {id:$id})-[r:ANSWERED_BY]->() DELETE r', id=key)


def _header(mirror, w, intent):
    from .rules import terms, ALIASES
    stamp = uuid4().hex
    root = lambda cypher, **params: mirror.query('active_global', cypher, **params)
    local = lambda cypher, **params: mirror.query('focus_'+w.key, cypher, **params)
    root('MERGE (f:Focus {id:$id}) SET f.name=$name,f.enabled=$enabled,f.archived=false', id=w.key, name=w.name, enabled=w.enabled)
    aliases = list(dict.fromkeys(a for word in terms(w.scope) for a in (word, *ALIASES.get(word, ()))))
    root('MATCH (f:Focus {id:$id}) UNWIND $aliases AS a MERGE (n:Entity {id:a}) '
         'MERGE (f)-[r:WATCHES]->(n) SET r.stamp=$stamp', id=w.key, aliases=aliases, stamp=stamp)
    root('MATCH (:Focus {id:$id})-[r:WATCHES]->() WHERE r.stamp<>$stamp DELETE r', id=w.key, stamp=stamp)
    local('MERGE (q:Question {id:$id}) SET q.text=$text,q.scope=$scope,q.archived=false', id=w.key, text=w.question or w.name, scope=w.scope)
    if intent: mirror.intent(local, w, intent, stamp)


def force_rebuild(session, mirror):
    # A durable generation marker survives an in-flight batch checkpoint.
    # The HTTP action schedules work; it never scans or deletes receipts.
    session.merge(DeskMeta(key='graph_focus_rebuild/'+mirror.prefix, value=uuid4().hex))


def reset(engine, mirror, watch):
    """Only a verified graph loss invalidates receipts; upgrades never do."""
    with Session(engine) as s:
        for model in (GraphFocusReceipt, GraphFocusState):
            s.exec(delete(model).where(model.namespace == mirror.prefix, model.watch == watch))
        s.commit()


def _save(engine, mirror, state, receipts=(), removed=()):
    # One UPSERT per batch, not SELECT+UPDATE for each material. All checkpoint
    # records and removals commit together after confirming the current lease.
    from sqlalchemy import tuple_
    if engine.dialect.name == 'postgresql':
        from sqlalchemy.dialects.postgresql import insert
    else:
        from sqlalchemy.dialects.sqlite import insert
    with Session(engine) as s:
        lease = mirror._projection_lease
        lock_news(s, 'lease/'+lease.key)
        lease.confirm(s)
        state.updated = utc_now_iso()
        table = GraphFocusState.__table__
        values = {column.name: getattr(state, column.name) for column in table.columns}
        statement = insert(table).values(values)
        s.execute(statement.on_conflict_do_update(index_elements=['namespace', 'watch'],
            set_={column.name: getattr(statement.excluded, column.name) for column in table.columns
                  if column.name not in ('namespace', 'watch')}).execution_options(
                n2d_query_name='desk.graph_focus.checkpoint'))
        if receipts:
            table = GraphFocusReceipt.__table__
            values = [{column.name: getattr(row, column.name) for column in table.columns} for row in receipts]
            statement = insert(table).values(values)
            s.execute(statement.on_conflict_do_update(index_elements=['namespace', 'watch', 'kind', 'item'],
                set_={name: getattr(statement.excluded, name) for name in ('fingerprint', 'generation', 'locator')})
                .execution_options(n2d_query_name='desk.graph_focus.receipts'))
        if removed:
            s.exec(delete(GraphFocusReceipt).where(GraphFocusReceipt.namespace == mirror.prefix,
                GraphFocusReceipt.watch == state.watch,
                tuple_(GraphFocusReceipt.kind, GraphFocusReceipt.item).in_([(r.kind, r.item) for r in removed]))
                .execution_options(n2d_query_name='desk.graph_focus.remove_receipts'))
        s.commit()


def _next(state):
    state.phase = PHASES[PHASES.index(state.phase)+1]
    state.cursor = state.through_key = ''


def step(engine, mirror, w, token):
    """Do one bounded phase. Caller owns/relinquishes the graph lease per call."""
    batch = _integer('NEWS2DOUYIN_GRAPH_FOCUS_BATCH_SIZE', 8, 1, 100)
    budget = _integer('NEWS2DOUYIN_GRAPH_FOCUS_BATCH_MS', 500, 10, 10000)/1000
    started = time.perf_counter()
    load_started = time.perf_counter()
    with Session(engine, expire_on_commit=False, autoflush=False) as s:
        state = s.get(GraphFocusState, (mirror.prefix, w.key))
        if state is None: state = GraphFocusState(namespace=mirror.prefix, watch=w.key)
        else: s.expunge(state)
        marker = s.get(DeskMeta, 'graph_focus_rebuild/'+mirror.prefix)
        rebuild_epoch = marker.value if marker else ''
        if state.rebuild_epoch != rebuild_epoch:
            # Explicit evidence rebuild also inventories extra legacy business
            # nodes; a mere material digest change would miss unknown nodes.
            state.adopted, state.adopt_cursor, state.adopt_through = False, 0, -1
        if (state.phase == 'complete' and state.token == token and state.scope_version == w.version
                and state.rebuild_epoch == rebuild_epoch):
            return dict(complete=True, token=state.token, phase='complete', scanned=state.scanned,
                changed=state.changed, removed=state.removed, batches=state.batches)
        if state.scope_version != w.version or state.rebuild_epoch != rebuild_epoch or state.phase == 'complete' or not state.token:
            state.token, state.scope_version, state.generation = token, w.version, uuid4().hex
            state.rebuild_epoch = rebuild_epoch
            state.phase = 'header' if state.adopted else 'adopt'
            state.cursor = state.through_key = ''
            state.scanned = state.changed = state.removed = state.batches = 0
        phase = state.phase
        state.batches += 1
        if phase == 'header':
            from .focus_jobs import definition
            intent = definition(s, w.key)
            digest = _digest([rebuild_epoch, w.name, w.scope, w.question, w.enabled, w.version, intent])
            receipt = s.get(GraphFocusReceipt, (mirror.prefix, w.key, 'header', w.key))
        elif phase in PHASES[1:-3]:
            if not state.through_key: state.through_key = _upper(s, w.key, phase)
            items = _page(s, w.key, phase, state.cursor, state.through_key, batch) if state.through_key else []
            material = [(item, *_data(phase, item)) for item in items]
            keys = [row[1] for row in material]
            previous_rows = s.exec(select(GraphFocusReceipt).where(GraphFocusReceipt.namespace == mirror.prefix,
                GraphFocusReceipt.watch == w.key, GraphFocusReceipt.kind == phase,
                GraphFocusReceipt.item.in_(keys)).execution_options(
                    n2d_query_name='desk.graph_focus.fingerprints')).all() if keys else []
            previous_by_key = {row.item: row for row in previous_rows}
            prepared = []
            for item, key, data, locator in material:
                if phase == 'answers':
                    from ..research.models import ResearchClaim
                    claim_ids = json.loads(item.claim_ids_json)
                    present = list(s.exec(select(ResearchClaim.claim_id).where(ResearchClaim.claim_id.in_(claim_ids),
                        ResearchClaim.case_id == item.case_id).order_by(ResearchClaim.claim_id))) if claim_ids else []
                    data.append(present)
                prepared.append((item, key, _digest([rebuild_epoch, data]), locator, previous_by_key.get(key)))
        elif phase == 'prune':
            obsolete = s.exec(select(GraphFocusReceipt).where(GraphFocusReceipt.namespace == mirror.prefix,
                GraphFocusReceipt.watch == w.key, GraphFocusReceipt.generation != state.generation)
                .order_by(GraphFocusReceipt.kind, GraphFocusReceipt.item).limit(batch)
                .execution_options(n2d_query_name='desk.graph_focus.prune')).all()
            existing = {}
            by_kind = {}
            for receipt in obsolete: by_kind.setdefault(receipt.kind, []).append(receipt.item)
            for kind, keys in by_kind.items():
                if kind == 'header':
                    found = set(keys)
                else:
                    query, column = _query(w.key, kind)
                    found = set(s.execute(query.with_only_columns(column).where(column.in_(keys))
                        .execution_options(n2d_query_name='desk.graph_focus.recheck')).scalars())
                existing.update({(kind, key): key in found for key in keys})
    mirror._lease_state.load_ms = getattr(mirror._lease_state, 'load_ms', 0.)+(time.perf_counter()-load_started)*1000
    # The SQL session has ended before the first graph operation.
    receipts, removed = [], []
    if phase == 'adopt':
        # Inventory native-ID windows once, never an open property range/top-k
        # scan. Existing business nodes become receipts before reconciliation.
        graph = 'focus_'+w.key
        timeout = _integer('NEWS2DOUYIN_GRAPH_FOCUS_QUERY_TIMEOUT_MS', 1000, 10, 10000)
        if state.adopt_through < 0:
            rows = mirror.read(graph, 'MATCH (n) RETURN max(id(n))', _query_timeout_ms=timeout)
            state.adopt_through = int(rows[0][0]) if rows and rows[0][0] is not None else -1
        if state.adopt_cursor <= state.adopt_through:
            query = ('MATCH (n) WHERE id(n)>=$lo AND id(n)<$hi '
                     "RETURN id(n),CASE WHEN 'Event' IN labels(n) THEN 'events' "
                     "WHEN 'OutlinePoint' IN labels(n) THEN 'points' "
                     "WHEN 'Investigation' IN labels(n) THEN 'reviews' "
                     "WHEN 'ResearchQuestion' IN labels(n) THEN 'questions' "
                     "WHEN 'Claim' IN labels(n) THEN 'claims' "
                     "WHEN 'Report' IN labels(n) THEN 'reports' ELSE '' END,n.id "
                     'ORDER BY id(n) LIMIT $limit')
            lo, hi, limit = state.adopt_cursor, min(state.adopt_cursor+1024, state.adopt_through+1), 128
            plans = getattr(mirror, '_focus_adopt_plans', set())
            if graph not in plans:
                plan = mirror.explain(graph, query, lo=lo, hi=hi, limit=limit)
                if 'nodebyidseek' not in ''.join(c for c in str(plan).lower() if c.isalpha()):
                    raise ValueError('topic_inventory_native_id_seek_unavailable')
                plans.add(graph)
                mirror._focus_adopt_plans = plans
            rows = mirror.read(graph, query, lo=lo, hi=hi, limit=limit, _query_timeout_ms=timeout)
            for _, kind, key in rows:
                if kind and isinstance(key, str):
                    receipts.append(GraphFocusReceipt(namespace=mirror.prefix, watch=w.key, kind=kind, item=key))
            state.adopt_cursor = int(rows[-1][0])+1 if len(rows) == limit else hi
        if state.adopt_cursor > state.adopt_through:
            state.adopted = True
            state.phase, state.cursor = 'header', ''
    elif phase == 'header':
        if not receipt or receipt.fingerprint != digest:
            _header(mirror, w, intent)
            state.changed += 1
        receipts.append(GraphFocusReceipt(namespace=mirror.prefix, watch=w.key, kind='header', item=w.key,
            fingerprint=digest, generation=state.generation))
        _next(state)
    elif phase in PHASES[1:-3]:
        processed = 0
        for item, key, digest, locator, previous in prepared:
            if not previous or previous.fingerprint != digest:
                # If an edge's endpoints changed, remove its prior association.
                if previous and previous.locator != json.dumps(locator, sort_keys=True) and phase in ('point_material', 'point_reviews'):
                    _remove(mirror, w.key, previous)
                _project(mirror, w, phase, item, uuid4().hex, first=not previous or not previous.fingerprint)
                state.changed += 1
            receipts.append(GraphFocusReceipt(namespace=mirror.prefix, watch=w.key, kind=phase, item=key,
                fingerprint=digest, generation=state.generation, locator=json.dumps(locator, sort_keys=True)))
            state.cursor = key
            state.scanned += 1
            processed += 1
            if time.perf_counter()-started >= budget: break
        if processed == len(prepared) and len(prepared) < batch: _next(state)
    elif phase == 'prune':
        for receipt in obsolete:
            if existing[(receipt.kind, receipt.item)]:
                # A current member inserted behind the cursor is retained.
                # The changed outbox token guarantees a subsequent pass.
                receipt.generation = state.generation
                receipts.append(receipt)
            else:
                _remove(mirror, w.key, receipt)
                removed.append(receipt)
                state.removed += 1
            if time.perf_counter()-started >= budget: break
        if not obsolete: _next(state)
    elif phase == 'finish':
        # Rebind anchors after this pass's Referents exist. Query intent remains
        # separate from factual material and never invents a reported action.
        from .focus_jobs import definition
        with Session(engine) as s: intent = definition(s, w.key)
        if intent:
            from ..extraction.schema import entity_key, referent
            for entity in intent.get('spec', {}).get('entities', []):
                ref = referent('actor', entity['name']) or 'name:'+entity.get('type', 'unknown')+':'+entity_key(entity['name'])
                mirror.query('focus_'+w.key, 'MATCH (i:InterestEntity {id:$id}),(r:Referent {id:$ref}) MERGE (i)-[:MAPS_TO]->(r)',
                             id=w.key+'/'+entity['id'], ref=ref)
        _next(state)
    save_started = time.perf_counter()
    _save(engine, mirror, state, receipts, removed)
    mirror._lease_state.ack_ms = getattr(mirror._lease_state, 'ack_ms', 0.)+(time.perf_counter()-save_started)*1000
    return dict(complete=state.phase == 'complete', token=state.token, phase=state.phase,
        scanned=state.scanned, changed=state.changed, removed=state.removed, batches=state.batches)


def summary(session, mirror):
    """Bounded status; never read material or contact FalkorDB from HTTP."""
    rows = session.exec(select(GraphFocusState).where(GraphFocusState.namespace == mirror.prefix,
        GraphFocusState.phase != 'complete').order_by(GraphFocusState.updated.desc()).limit(10)
        .execution_options(n2d_query_name='desk.graph_focus.status')).all()
    return dict(active=len(rows), limited=len(rows) == 10, topics=[dict(watch=r.watch, phase=r.phase,
        scanned=r.scanned, changed=r.changed, removed=r.removed, batches=r.batches, updated=r.updated) for r in rows])
