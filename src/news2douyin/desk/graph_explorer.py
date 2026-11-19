"""Bounded, read-only views of the projected activity and focus graphs."""

from urllib.parse import quote

from redis.exceptions import RedisError
from sqlalchemy import func
from sqlmodel import Session, select

from ..attention.graph import OWNER as ATTENTION_OWNER
from ..extraction.models import FocusPoint
from ..knowledge.models import GraphRelation, PointMaterial
from ..selection.source_content import source_url
from .focus_lifecycle import require_active, visible_watch
from .models import DeskMeta, EventPlace, GraphPending, RadarEntry, Watch, WatchEvent, WatchReview
from .places import countries
from .service import current_membership

DEFAULT_LIMIT = 240
MAX_LIMIT = 800
QUERY_TIMEOUT_MS = 600
_DEPTH = 4
_KINDS = (
    'Question', 'Focus', 'OutlinePoint', 'InterestScope', 'InterestEntity', 'InterestPattern',
    'Event', 'Article', 'Organization', 'Person', 'Entity', 'Place', 'Investigation',
    'ResearchQuestion', 'Claim', 'EvidenceRef', 'Report', 'EventMention', 'Argument',
    'Referent', 'OccurrenceCandidate', 'SourceEvent', 'GdeltActor', 'GdeltPlace', 'GdeltDocument',
    'GkgSubject', 'AttentionTopic', 'AttentionSourceItem',
)
_RELATIONS = (
    'TRACKS', 'WATCHES', 'RELATED_EVENT', 'HAS_SOURCE', 'MENTIONS', 'MENTIONS_PLACE',
    'ACTION_AT', 'IMPACTS', 'HAS_SUBQUESTION', 'DEFINED_BY', 'HAS_ANCHOR', 'HAS_PATTERN',
    'MAPS_TO', 'PATTERN_SUBJECT', 'PATTERN_OBJECT', 'HAS_MATERIAL', 'RELEVANT_MATERIAL',
    'CITES', 'CHECKED_BY', 'ASKS', 'FOUND', 'HAS_EVIDENCE', 'ANSWERED_BY', 'PRODUCED',
    'DESCRIBES', 'HAS_ARGUMENT', 'REFERS_TO', 'HAS_MATCH_KEY', 'CODED_ACTOR1', 'CODED_ACTOR2',
    'CODED_ACTION_AT', 'MENTIONED_IN', 'GKG_MENTIONS', 'OBSERVED_ITEM', 'CANDIDATE_MATCH', 'CANDIDATE_FOCUS',
)
_OWNER_QUERY = 'MATCH (m:GraphMeta {id:$key}) RETURN m.owner LIMIT 1'


def _member_events(focus):
    return (select(RadarEntry.key, RadarEntry.title, RadarEntry.seen, WatchEvent.reason)
            .select_from(RadarEntry).join(WatchEvent, WatchEvent.event == RadarEntry.key)
            .where(WatchEvent.watch == focus, WatchEvent.choice != 'exclude', current_membership()))


def focuses(session):
    """List every available focus in name order."""
    rows = session.exec(select(Watch.key, Watch.name).where(visible_watch())
                        .order_by(func.lower(Watch.name), Watch.key))
    return [dict(key=key, name=name) for key, name in rows]


def _node(kind, key, title, url='', **details):
    return dict(id=kind + ':' + key, title=str(title or key)[:500], kind=kind,
                url=url, details=_details(details))


def _details(values):
    return {name: value[:1000] if isinstance(value, str) else value
            for name, value in values.items() if value is not None and value != ''}


def _local_url(kind, key, case_key=''):
    resource = {'Question': 'focus', 'Focus': 'focus', 'Event': 'events', 'Article': 'articles'}.get(kind)
    if resource:
        return '/' + resource + '/' + quote(key, safe='')
    if kind == 'Investigation' and case_key:
        return '/research/' + quote(case_key, safe='')
    if kind == 'SourceEvent' and key.startswith('gdelt/'):
        return '/radar?source=gdelt&event=' + quote(key[6:], safe='')
    if kind == 'AttentionTopic':
        return '/radar?source=attention&attention_topic=' + quote(key, safe='')
    return ''


class _View:
    def __init__(self, limit):
        self.limit = limit
        self.edge_limit = limit * 3
        self.nodes = {}
        self.edges = {}
        self.truncated = False

    def add(self, node):
        key = node['id']
        if key not in self.nodes and len(self.nodes) >= self.limit:
            self.truncated = True
            return False
        self.nodes.setdefault(key, node)
        return True

    def connect(self, key, source, target, kind, **details):
        if source not in self.nodes or target not in self.nodes:
            return
        if key not in self.edges and len(self.edges) >= self.edge_limit:
            self.truncated = True
            return
        self.edges.setdefault(key, dict(id=key, source=source, target=target, kind=kind,
                                        details=_details(details)))

    def payload(self, view, focus, backend, reason=''):
        state = 'empty' if not self.nodes else 'degraded' if reason else 'ready'
        return dict(view=view, focus=focus, backend=backend, state=state, reason=reason,
                    nodes=list(self.nodes.values()), edges=list(self.edges.values()),
                    truncated=self.truncated, node_limit=self.limit, edge_limit=self.edge_limit)


def _select_events(session, focus, cap):
    if focus:
        query = _member_events(focus)
    else:
        query = select(RadarEntry.key, RadarEntry.title, RadarEntry.seen)
    return session.exec(query.order_by(RadarEntry.seen.desc(), RadarEntry.key).limit(cap + 1)).all()


def _focus_snapshot(session, key):
    watch = require_active(session, key)
    return dict(key=key, name=watch.name), watch.question or watch.name


def _pending(session, focus):
    row = session.get(GraphPending, 'watch/' + focus)
    return bool(row and row.written != row.token)


def _summary_focus(session, result, focus, question, events):
    key = focus['key']
    root = _node('Question', key, question, _local_url('Question', key))
    result.add(root)
    # Reserve room for current events even for full outlines at smaller limits.
    cap = min(64, (result.limit - 1) // 2)
    rows = session.exec(select(FocusPoint.key, FocusPoint.parent, FocusPoint.text)
                        .where(FocusPoint.watch == key).order_by(FocusPoint.created, FocusPoint.key)
                        .limit(cap + 1)).all()
    points = {point: (parent, text) for point, parent, text in rows[:cap]}
    result.truncated |= len(rows) > cap
    for point, (_, text) in points.items():
        result.add(_node('OutlinePoint', point, text))
    for point, (parent, _) in points.items():
        source = root['id'] if parent == key else 'OutlinePoint:' + parent
        result.connect('point:' + point, source, 'OutlinePoint:' + point, 'HAS_SUBQUESTION')
    shown = []
    for event, title, seen, reason in events:
        target = _node('Event', event, title, _local_url('Event', event), seen=seen)
        if not result.add(target):
            break
        shown.append(event)
        result.connect('member:' + event, root['id'], target['id'], 'HAS_MATERIAL', reason=reason)
    if points and shown:
        rows = session.exec(select(PointMaterial.key, PointMaterial.point, PointMaterial.event, PointMaterial.reason)
                            .where(PointMaterial.point.in_(points), PointMaterial.event.in_(shown))
                            .order_by(PointMaterial.key).limit(result.edge_limit + 1)).all()
        result.truncated |= len(rows) > result.edge_limit
        for relation, point, event, reason in rows[:result.edge_limit]:
            result.connect(relation, 'OutlinePoint:' + point, 'Event:' + event,
                           'RELEVANT_MATERIAL', reason=reason)
    available = result.limit - len(result.nodes)
    rows = session.exec(select(WatchReview.key, WatchReview.question, WatchReview.case_key, WatchReview.status)
                        .where(WatchReview.watch == key).order_by(WatchReview.created.desc(), WatchReview.key)
                        .limit(available + 1)).all()
    result.truncated |= len(rows) > available
    for review, title, case, status in rows[:available]:
        target = _node('Investigation', review, title, _local_url('Investigation', review, case), status=status)
        result.add(target)
        result.connect('review:' + review, root['id'], target['id'], 'CHECKED_BY')


def _summary_global(session, result, events):
    for event, title, seen in events:
        result.add(_node('Event', event, title, _local_url('Event', event), seen=seen))
    keys = [row[0] for row in events]
    if not keys:
        return
    relations = session.exec(select(GraphRelation.key, GraphRelation.source, GraphRelation.target,
                                     GraphRelation.weight, GraphRelation.evidence)
                             .where(GraphRelation.source.in_(keys), GraphRelation.target.in_(keys))
                             .order_by(GraphRelation.weight.desc(), GraphRelation.key)
                             .limit(result.edge_limit + 1)).all()
    result.truncated |= len(relations) > result.edge_limit
    for key, source, target, weight, evidence in relations[:result.edge_limit]:
        result.connect(key, 'Event:' + source, 'Event:' + target, 'RELATED_EVENT',
                       weight=weight, evidence=evidence)
    places = session.exec(select(EventPlace.key, EventPlace.event, EventPlace.iso, EventPlace.role,
                                  EventPlace.article, EventPlace.revision, EventPlace.quote)
                          .where(EventPlace.event.in_(keys)).order_by(EventPlace.key)
                          .limit(result.edge_limit + 1)).all()
    result.truncated |= len(places) > result.edge_limit
    for key, event, iso, role, article, revision, excerpt in places[:result.edge_limit]:
        target = _node('Place', iso, countries().get(iso, iso), code=iso)
        if result.add(target):
            result.connect(key, 'Event:' + event, target['id'], 'ACTION_AT' if role == 'action' else 'IMPACTS',
                           article=article, revision=revision, quote=excerpt)


def _graph_node_fields(var):
    keys = ('id', 'iso', 'title', 'name', 'text', 'trigger', 'url', 'case_id', 'seen', 'status', 'kind',
            'quote', 'scope', 'description', 'role', 'identity_status', 'domain', 'key',
            'identifier', 'last_seen', 'actor1_name', 'event_code', 'actor2_name', 'provider', 'source_url', 'platform')
    return ','.join([f'id({var})', f'labels({var})', *(f'{var}.{key}' for key in keys)])


def _graph_node(row):
    (native, labels, business, iso, title, name, text, trigger, url, case_key, seen, status,
     type_name, excerpt, scope, description, role, identity, domain, key, identifier, last_seen,
     actor1, event_code, actor2, provider, publisher_url, platform) = row
    if isinstance(labels, str):
        labels = {label.strip().strip('"') for label in labels.strip('[]').split(',')}
    kind = next((kind for kind in _KINDS if kind in labels), '')
    key = business or key or iso
    if not kind or not key:
        return native, None
    key = str(key)
    coded = ' · '.join(value for value in (actor1, event_code, actor2) if value)
    label = title or name or text or trigger or description or coded or identifier or countries().get(iso, iso) or key
    url = url or identifier
    external = source_url(url) if isinstance(url, str) and '\\' not in url else ''
    local = _local_url(kind, key, str(case_key) if case_key else '')
    return native, _node(kind, key, label, external or local, seen=seen or last_seen, status=status,
                         type=type_name, quote=excerpt, scope=scope, role=role, identity=identity, domain=domain,
                         code=iso, provider=provider, platform=platform, source_url=publisher_url)


def _read(mirror, graph, query, **params):
    return mirror.read(graph, query, _query_timeout_ms=QUERY_TIMEOUT_MS, **params)


def _read_snapshot(mirror, graph, focus, keys, result):
    fields = _graph_node_fields('n')
    seeds = []
    if focus:
        rows = _read(mirror, graph,
                     f'MATCH (n:Question {{id:$key}}) WHERE coalesce(n.archived,false)=false RETURN {fields} LIMIT 1',
                     key=focus['key'])
        if not rows:
            return False
        seeds.extend(rows)
    if keys:
        seeds.extend(_read(mirror, graph, 'UNWIND $keys AS key MATCH (n:Event {id:key}) '
                           f'RETURN {fields} LIMIT $size', keys=keys, size=len(keys)))
    if not focus:
        # These bounded label scans expose the actual official and platform
        # roots in the same graph, including when there are no organized events.
        scopes = (
            ('SourceEvent', 24, 'coalesce(n.evidence_available,true)=true', {}),
            ('AttentionTopic', 30, 'n.projection_owner=$owner', {'owner': ATTENTION_OWNER}),
        )
        for label, divisor, visible, params in scopes:
            cap = max(1, result.limit // divisor)
            rows = _read(mirror, graph, f'MATCH (n:{label}) WHERE {visible} RETURN {fields} LIMIT $size',
                         size=cap + 1, **params)
            result.truncated |= len(rows) > cap
            seeds.extend(rows[:cap])
    if not seeds:
        return False
    included = {}
    for raw in seeds:
        native, node = _graph_node(raw)
        if node and result.add(node):
            included[native] = node['id']
    # An Event can be incident on many nodes. Only the selected recent
    # Event seeds may enter the response via another neighborhood.
    if focus:
        roots = [native for native, key in included.items() if key.startswith('Question:')]
        event_nodes = [native for native, key in included.items() if key.startswith('Event:')]
        if event_nodes:
            rows = _read(mirror, graph,
                         'UNWIND $events AS ident MATCH (e) WHERE id(e)=ident '
                         'MATCH (q:Question {id:$focus})-[r:HAS_MATERIAL]->(e) '
                         'RETURN id(r),id(q),id(e),r.reason LIMIT $size',
                         events=event_nodes, focus=focus['key'], size=len(event_nodes))
            for native, left, right, reason in rows:
                if left in included and right in included:
                    result.connect('edge:' + str(native), included[left], included[right], 'HAS_MATERIAL', reason=reason)
        frontier = roots + event_nodes
    else:
        frontier = list(included)
    nodes_fields = _graph_node_fields('b')
    query = ('UNWIND $ids AS ident MATCH (a) WHERE id(a)=ident '
             'MATCH (a)-[r]-(b) WHERE type(r) IN $types AND (NOT b:Event OR b.id IN $events) '
             'AND (startNode(r)=a OR type(r) IN $reverse) '
             'AND (NOT b:Focus OR coalesce(b.archived,false)=false) '
             'AND (NOT b:SourceEvent OR coalesce(b.evidence_available,true)=true) '
             'AND (NOT type(r) IN $candidates OR coalesce(r.active,false)=true) '
             'RETURN id(a),id(r),id(startNode(r)),type(r),'
             'r.reason,r.kind,r.weight,r.evidence,r.article,r.revision,r.quote,'
             f'{nodes_fields} LIMIT $size')
    candidates = ['CANDIDATE_MATCH', 'CANDIDATE_FOCUS']
    for _ in range(_DEPTH):
        if not frontier:
            break
        rows = _read(mirror, graph, query, ids=frontier, types=list(_RELATIONS), events=keys,
                     reverse=['TRACKS', 'RELATED_EVENT'], candidates=candidates, size=result.edge_limit + 1)
        result.truncated |= len(rows) > result.edge_limit
        frontier = []
        for a, edge, start, kind, reason, relation_type, weight, evidence, article, revision, excerpt, *raw in rows[:result.edge_limit]:
            native, node = _graph_node(raw)
            if a not in included or not node:
                continue
            if native not in included:
                if not result.add(node):
                    continue
                included[native] = node['id']
                frontier.append(native)
            source, target = (a, native) if start == a else (native, a)
            result.connect('edge:' + str(edge), included[source], included[target], kind, reason=reason,
                           relation_type=relation_type, weight=weight, evidence=evidence, article=article,
                           revision=revision, quote=excerpt)
    result.truncated |= bool(frontier)
    return bool(result.nodes)


def _hide_archived_focuses(engine, result):
    prefix = 'Focus:'
    keys = [node['id'][len(prefix):] for node in result.nodes.values() if node['kind'] == 'Focus']
    if not keys:
        return
    with Session(engine) as session:
        visible = set(session.exec(select(Watch.key).where(Watch.key.in_(keys), visible_watch())))
    hidden = {prefix + key for key in keys if key not in visible}
    for ident in hidden:
        del result.nodes[ident]
    result.edges = {ident: edge for ident, edge in result.edges.items()
                    if edge['source'] not in hidden and edge['target'] not in hidden}


def explore(engine, mirror, *, view='global', focus='', limit=DEFAULT_LIMIT):
    """Return the requested visualization without writing either database."""
    if view not in ('global', 'focus'):
        raise ValueError('图谱视图无效')
    if type(limit) is not int or not 1 <= limit <= MAX_LIMIT:
        raise ValueError('图谱节点数量超出范围')
    if view == 'global' and focus:
        raise ValueError('全局图不接受焦点编号')

    cap = min(80, max(1, limit // 3))
    with Session(engine) as session:
        selected = question = None
        if view == 'focus':
            selected, question = _focus_snapshot(session, focus)
        events = _select_events(session, focus, cap)
        pending = bool(focus and _pending(session, focus))
        owner = session.get(DeskMeta, 'graph_owner')
        owner = owner.value if owner else ''
    reason = 'unconfigured' if not mirror.url else 'pending'
    if mirror.url and owner and not pending:
        try:
            rows = _read(mirror, 'active_global', _OWNER_QUERY, key='schema')
            if rows and rows[0][0] == owner:
                result = _View(limit)
                result.truncated = len(events) > cap
                graph = 'focus_' + focus if focus else 'active_global'
                if _read_snapshot(mirror, graph, selected, [row[0] for row in events[:cap]], result):
                    if not selected:
                        _hide_archived_focuses(engine, result)
                    return result.payload(view, selected, 'falkordb')
            elif rows:
                reason = 'unavailable'
        except RedisError:
            # The UI can still show committed SQL facts during graph outages,
            # and no graph endpoint/driver error is disclosed to the client.
            reason = 'unavailable'

    result = _View(limit)
    result.truncated = len(events) > cap
    with Session(engine) as session:
        if focus:
            require_active(session, focus)
            _summary_focus(session, result, selected, question, events[:cap])
        else:
            _summary_global(session, result, events[:cap])
    return result.payload(view, selected, 'sql', reason)
