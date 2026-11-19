"""Bounded, read-only views of the projected activity and focus graphs."""

import os
import time
from collections import defaultdict
from urllib.parse import quote

from redis.exceptions import RedisError, ResponseError, TimeoutError as RedisTimeout
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
_DEPTH = 4
_KINDS = (
    'Question', 'Focus', 'OutlinePoint', 'InterestScope', 'InterestEntity', 'InterestPattern',
    'Event', 'Article', 'Organization', 'Person', 'Entity', 'Place', 'Investigation',
    'ResearchQuestion', 'Claim', 'EvidenceRef', 'Report', 'EventMention', 'Argument',
    'Referent', 'OccurrenceCandidate', 'SourceEvent', 'GdeltActor', 'GdeltPlace', 'GdeltDocument',
    'GkgSubject', 'AttentionTopic', 'AttentionSourceItem',
)
_NEIGHBOR_BATCH = 16
_NEIGHBOR_LIMIT = 16
_OUTGOING = {
    'Question': 'HAS_SUBQUESTION|DEFINED_BY|CHECKED_BY',
    'OutlinePoint': 'HAS_SUBQUESTION|RELEVANT_MATERIAL|CHECKED_BY',
    'Focus': 'WATCHES',
    'InterestScope': 'HAS_ANCHOR|HAS_PATTERN',
    'InterestEntity': 'MAPS_TO',
    'InterestPattern': 'PATTERN_SUBJECT|PATTERN_OBJECT',
    'Article': 'MENTIONS|MENTIONS_PLACE|DESCRIBES',
    'Investigation': 'ASKS|FOUND|PRODUCED',
    'ResearchQuestion': 'ANSWERED_BY',
    'Claim': 'HAS_EVIDENCE',
    'EventMention': 'HAS_ARGUMENT|HAS_MATCH_KEY',
    'Argument': 'REFERS_TO',
    'SourceEvent': 'CODED_ACTOR1|CODED_ACTOR2|CODED_ACTION_AT|MENTIONED_IN',
    'GdeltDocument': 'GKG_MENTIONS',
    'AttentionTopic': 'OBSERVED_ITEM|CANDIDATE_MATCH|CANDIDATE_FOCUS',
}
_OWNER_QUERY = 'MATCH (m:GraphMeta {id:$key}) RETURN m.owner LIMIT 1'


class _BudgetSpent(Exception):
    pass


def _milliseconds(name, default, minimum, maximum):
    try:
        value = int(os.getenv('NEWS2DOUYIN_GRAPH_EXPLORER_' + name, str(default)))
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


class _ReadBudget:
    def __init__(self, mirror, client):
        self.mirror = mirror
        self.client = client
        self.query_timeout = _milliseconds('QUERY_TIMEOUT_MS', 600, 50, 5000)
        self.deadline = time.monotonic() + _milliseconds('BUDGET_MS', 1800, 200, 10000) / 1000

    def __call__(self, graph, query, **params):
        remaining = int((self.deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise _BudgetSpent
        return self.mirror.read(graph, query, _query_timeout_ms=min(remaining, self.query_timeout),
                                _client=self.client, **params)


def _read_failure(exc):
    if isinstance(exc, _BudgetSpent):
        return 'budget'
    if isinstance(exc, RedisTimeout):
        return 'socket_timeout'
    if isinstance(exc, ResponseError):
        message = str(exc).casefold()
        if 'query timed out' in message:
            return 'query_timeout'
        if 'does not contain a graph object' in message or 'graph does not exist' in message:
            return 'pending'
        return 'query_error'
    return 'unavailable'


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
        state = 'ready'
        if not self.nodes:
            state = 'empty'
        elif reason:
            state = 'partial' if backend == 'falkordb' else 'degraded'
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


def _remember(result, included, rows):
    for raw in rows:
        native, node = _graph_node(raw)
        if node and result.add(node):
            included[native] = node['id']


def _walks(kind, focus):
    if kind == 'Event':
        if focus:
            return (('HAS_MATERIAL', True), ('CITES', False))
        return (('HAS_SOURCE|ACTION_AT|IMPACTS', False), ('TRACKS', True),
                ('RELATED_EVENT', False), ('RELATED_EVENT', True))
    outgoing = _OUTGOING.get(kind)
    return ((outgoing, False),) if outgoing else ()


def _neighbor_query(types, incoming):
    direction = f'(a)<-[r:{types}]-(b)' if incoming else f'(a)-[r:{types}]->(b)'
    # Anchor the pattern on a native node seek. Limit each typed adjacency
    # before reading properties or filtering unselected neighbors, otherwise a
    # high-degree node with few visible matches can consume the whole request.
    return ('UNWIND $ids AS ident CALL { WITH ident MATCH ' + direction +
            ' WHERE id(a)=ident RETURN r,b LIMIT $take } '
            'RETURN ident,id(r),type(r), '
            'CASE WHEN (NOT b:Event OR b.id IN $events) '
            'AND (NOT b:Focus OR coalesce(b.archived,false)=false) '
            'AND (NOT b:SourceEvent OR coalesce(b.evidence_available,true)=true) '
            'AND (NOT (type(r) IN ["CANDIDATE_MATCH","CANDIDATE_FOCUS"]) OR coalesce(r.active,false)=true) '
            'THEN 1 ELSE 0 END, '
            'r.reason,r.kind,r.weight,r.evidence,r.article,r.revision,r.quote,' + _graph_node_fields('b'))


def _expand(graph, selected, keys, result, included, read):
    frontier = list(included)
    for _ in range(_DEPTH):
        groups = defaultdict(list)
        for native in frontier:
            for walk in _walks(result.nodes[included[native]]['kind'], selected):
                groups[walk].append(native)
        frontier = []
        for (types, incoming), anchors in groups.items():
            query = _neighbor_query(types, incoming)
            for offset in range(0, len(anchors), _NEIGHBOR_BATCH):
                if len(result.edges) >= result.edge_limit:
                    result.truncated = True
                    return
                rows = read(graph, query, ids=anchors[offset:offset + _NEIGHBOR_BATCH],
                            take=_NEIGHBOR_LIMIT + 1, events=keys)
                counts = defaultdict(int)
                for anchor, edge, kind, visible, reason, rel_type, weight, evidence, article, revision, excerpt, *raw in rows:
                    counts[anchor] += 1
                    if counts[anchor] > _NEIGHBOR_LIMIT:
                        result.truncated = True
                        continue
                    if not visible:
                        continue
                    native, node = _graph_node(raw)
                    if not node:
                        continue
                    if native not in included:
                        if not result.add(node):
                            continue
                        included[native] = node['id']
                        frontier.append(native)
                    source, target = (native, anchor) if incoming else (anchor, native)
                    result.connect('edge:' + str(edge), included[source], included[target], kind,
                                   reason=reason, relation_type=rel_type, weight=weight,
                                   evidence=evidence, article=article, revision=revision, quote=excerpt)
        if not frontier:
            break
    result.truncated |= any(_walks(result.nodes[included[native]]['kind'], selected) for native in frontier)


def _read_snapshot(graph, focus, keys, result, *, read):
    fields = _graph_node_fields('n')
    included = {}
    if focus:
        rows = read(graph, f'MATCH (n:Question {{id:$key}}) WHERE coalesce(n.archived,false)=false '
                    f'RETURN {fields} LIMIT 1', key=focus['key'])
        if not rows:
            return False
        _remember(result, included, rows)
    if keys:
        _remember(result, included, read(graph, 'MATCH (n:Event) WHERE n.id IN $keys '
                                         f'RETURN {fields} LIMIT $size', keys=keys, size=len(keys)))
    if not focus:
        # Read the current official and platform roots even when there are no
        # organized events. Their slices never sort or count the whole graph.
        scopes = (
            ('SourceEvent', 24, 'coalesce(n.evidence_available,true)=true', {}),
            ('AttentionTopic', 30, 'n.projection_owner=$owner', {'owner': ATTENTION_OWNER}),
        )
        for label, divisor, visible, params in scopes:
            cap = max(1, result.limit // divisor)
            scan = cap * 3
            rows = read(graph, f'MATCH (n:{label}) WITH n LIMIT $size '
                        f'RETURN CASE WHEN {visible} THEN 1 ELSE 0 END,{fields}', size=scan + 1, **params)
            visible_rows = [row[1:] for row in rows[:scan] if row[0]]
            result.truncated |= len(rows) > scan or len(visible_rows) > cap
            _remember(result, included, visible_rows[:cap])
    _expand(graph, focus, keys, result, included, read)
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
        result = _View(limit)
        result.truncated = len(events) > cap
        try:
            socket_timeout = _milliseconds('SOCKET_TIMEOUT_MS', 1000, 100, 6000)
            with mirror.explorer_client(socket_timeout) as client:
                read = _ReadBudget(mirror, client)
                rows = read('active_global', _OWNER_QUERY, key='schema')
                if rows and rows[0][0] == owner:
                    graph = 'focus_' + focus if focus else 'active_global'
                    keys = [row[0] for row in events[:cap]]
                    if _read_snapshot(graph, selected, keys, result, read=read):
                        reason = ''
                elif rows:
                    reason = 'owner_mismatch'
        except (RedisError, _BudgetSpent) as exc:
            reason = _read_failure(exc)
        if result.nodes and not selected:
            _hide_archived_focuses(engine, result)
        if result.nodes:
            return result.payload(view, selected, 'falkordb', reason)

    result = _View(limit)
    result.truncated = len(events) > cap
    with Session(engine) as session:
        if focus:
            require_active(session, focus)
            _summary_focus(session, result, selected, question, events[:cap])
        else:
            _summary_global(session, result, events[:cap])
    return result.payload(view, selected, 'sql', reason)
