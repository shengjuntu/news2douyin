"""Queue and publish an official-event graph without exposing partial rebuilds."""

from collections import defaultdict
import json
import re
import time
from uuid import uuid4

from redis.exceptions import RedisError
from sqlalchemy import and_, or_, text
from sqlalchemy.orm import defer
from sqlmodel import Session, select

from ..storage.articles import lock_news
from ..storage.models import utc_now_iso
from .focus_event_models import FocusEventGraph
from .focus_events import bounds, build, has_linked_reports
from .focus_lifecycle import require_active, visible_watch
from .leases import acquire
from .models import DeskMeta, Watch

REFRESH = 30 * 60
INDEX = 'ix_officialmention_focus_document_prefix'
_GRAPH = re.compile(r'events_focus_[A-Za-z0-9_-]{1,100}_[a-f0-9]{32}')
_WATCH = re.compile(r'[A-Za-z0-9_-]{1,100}')
_LABELS = ('SourceEvent', 'GdeltActor', 'GdeltPlace', 'GdeltDocument', 'Person', 'Organization')
_EDGES = ('CODED_ACTOR1', 'CODED_ACTOR2', 'CODED_ACTION_AT', 'MENTIONED_IN', 'GKG_MENTIONS')
_PROPERTIES = {
    'SourceEvent': ('external_id', 'event_date', 'code', 'country', 'quad_class', 'goldstein', 'basis', 'observed', 'feed', 'batch', 'row'),
    'GdeltActor': ('country',), 'GdeltPlace': ('country', 'latitude', 'longitude'),
    'GdeltDocument': ('identifier', 'domain', 'gkg_record', 'feed', 'batch', 'row'),
    'Person': ('identity',), 'Organization': ('identity',),
    'MENTIONED_IN': ('mention_key', 'feed', 'batch', 'row', 'seen', 'sentence', 'confidence',
                     'first_seen', 'first_feed', 'first_batch', 'first_row'),
    'GKG_MENTIONS': ('gkg_record', 'feed', 'batch', 'row'),
}
_INDEX_LOCK = 67451157019


def _index(engine):
    if engine.dialect.name == 'sqlite':
        with engine.begin() as connection:
            if connection.execute(text("SELECT 1 FROM sqlite_master WHERE type='index' AND name=:name"),
                                  {'name': INDEX}).scalar():
                return
            connection.exec_driver_sql('CREATE INDEX IF NOT EXISTS ' + INDEX +
                                       ' ON officialmention (substr(doc_identifier, 1, 240))')
        return
    with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
        got_lock = connection.execute(text('SELECT pg_try_advisory_lock(:key)'), {'key': _INDEX_LOCK}).scalar()
        if not got_lock:
            raise RuntimeError('official_mention_index_busy')
        try:
            connection.exec_driver_sql('SET statement_timeout = 0')
            row = connection.execute(text('SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)'),
                                     {'name': INDEX}).first()
            # Recover a failed concurrent build of this projection's index.
            if row and not row[0]:
                connection.exec_driver_sql('DROP INDEX CONCURRENTLY IF EXISTS ' + INDEX)
                row = None
            if not row:
                connection.exec_driver_sql('CREATE INDEX CONCURRENTLY IF NOT EXISTS ' + INDEX +
                                           ' ON officialmention (substr(doc_identifier, 1, 240))')
        finally:
            connection.exec_driver_sql('RESET statement_timeout')
            connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': _INDEX_LOCK})


def request(session, key, *, days, until):
    if not _WATCH.fullmatch(key):
        raise KeyError('没有找到这个焦点')
    bounds(days, until)
    lock_news(session, 'watch:' + key)
    watch = require_active(session, key)
    row = session.get(FocusEventGraph, key, options=(_without_snapshot(),))
    if row and row.state in ('queued', 'building') and (row.scope_version, row.days, row.until) == (watch.version, days, until):
        return {'state': row.state}
    if row is None:
        row = FocusEventGraph(watch=key, token=uuid4().hex, scope_version=watch.version, days=days, until=until)
    elif row.state != 'error' or (row.scope_version, row.days, row.until) != (watch.version, days, until):
        _advance(row)
    row.scope_version, row.days, row.until = watch.version, days, until
    row.state, row.error = 'queued', ''
    session.add(row)
    session.commit()
    return {'state': 'queued'}


def _without_snapshot():
    return defer(FocusEventGraph.snapshot)


def _stage(watch, token):
    return 'events_focus_' + watch + '_' + token


def _advance(row):
    if row.state in ('building', 'error'):
        retired = {name for name in row.retiring.split(',') if name}
        retired.add(_stage(row.watch, row.token))
        row.retiring = ','.join(sorted(retired))
    row.token = uuid4().hex


def _drop(mirror, name):
    redis_key = mirror.prefix + '_' + name
    if mirror.client.exists(redis_key):
        mirror._projection_lease.touch()
        try:
            mirror.client.execute_command('GRAPH.DELETE', redis_key)
        except RedisError:
            if mirror.client.exists(redis_key):
                raise


def _owner(engine, mirror):
    with Session(engine) as session:
        row = session.get(DeskMeta, 'graph_owner')
        owner = row.value if row else ''
    if not owner:
        mirror.bootstrap(engine)
        with Session(engine) as session:
            owner = session.get(DeskMeta, 'graph_owner').value
    rows = mirror.read('active_global', 'MATCH (m:GraphMeta {id:$id}) RETURN m.owner LIMIT 1', id='schema')
    if not rows:
        mirror.bootstrap(engine)
        rows = mirror.read('active_global', 'MATCH (m:GraphMeta {id:$id}) RETURN m.owner LIMIT 1', id='schema')
    if not rows or rows[0][0] != owner:
        raise ValueError('graph_namespace_belongs_to_another_database')


def _mirror(mirror, name, watch, snapshot, lease):
    _drop(mirror, name)
    mirror.query(name, 'CREATE (:FocusEventProjection {focus:$watch, built_at:$at})',
                 watch=watch, at=utc_now_iso())
    # Newly staged graph: create once in sorted typed batches, then use a
    # temporary shared label index to resolve stable node IDs for all edges.
    for label in _LABELS:
        keys = _PROPERTIES.get(label, ())
        rows = [[item['id'], item['title'], item.get('url', ''),
                 *(item['details'].get(key) for key in keys)]
                for item in snapshot['nodes'] if item['kind'] == label]
        assign = ''.join(',n.' + key + '=v[' + str(i) + ']' for i, key in enumerate(keys, 3))
        for start in range(0, len(rows), 128):
            lease.touch()
            mirror.query(name, 'UNWIND $rows AS v CREATE (n:FocusGraphNode:' + label +
                         ' {id:v[0],name:v[1],url:v[2]})' + (' SET ' + assign[1:] if assign else ''),
                         rows=rows[start:start + 128])
    if not snapshot['edges']:
        return
    mirror.query(name, 'CREATE INDEX FOR (n:FocusGraphNode) ON (n.id)')
    # Graph queries can fall back to scanning while an index builds. Wait a
    # bounded time for an actual index seek before publishing a large graph.
    for _ in range(40):
        plan = mirror.explain(name, 'MATCH (n:FocusGraphNode {id:$id}) RETURN n', id=snapshot['nodes'][0]['id'])
        operators = ''.join(c for c in str(plan).lower() if c.isalpha())
        if 'indexseek' in operators or 'indexscan' in operators:
            break
        time.sleep(.15)
        lease.touch()
    else:
        raise RuntimeError('focus_event_graph_index_not_ready')
    for kind in _EDGES:
        keys = _PROPERTIES.get(kind, ())
        rows = [[item['source'], item['target'], *(item['details'].get(key) for key in keys)]
                for item in snapshot['edges'] if item['kind'] == kind]
        assign = ''.join(',r.' + key + '=v[' + str(i) + ']' for i, key in enumerate(keys, 2))
        for start in range(0, len(rows), 128):
            lease.touch()
            mirror.query(name, 'UNWIND $rows AS v MATCH (a:FocusGraphNode {id:v[0]}),'
                         '(b:FocusGraphNode {id:v[1]}) CREATE (a)-[r:' + kind + ']->(b)'
                         + (' SET ' + assign[1:] if assign else ''), rows=rows[start:start + 128])


def _pick(session):
    active = Watch.enabled.is_(True) & (FocusEventGraph.until == '')
    due = and_(FocusEventGraph.refresh_at > 0, FocusEventGraph.refresh_at <= time.time())
    return session.exec(select(FocusEventGraph, Watch)
        .join(Watch, Watch.key == FocusEventGraph.watch).where(visible_watch(),
            or_(FocusEventGraph.state.in_(('queued', 'building')),
                FocusEventGraph.scope_version != Watch.version,
                and_(FocusEventGraph.state == 'ready', active, due)))
        .options(_without_snapshot())
        .order_by(FocusEventGraph.refresh_at, FocusEventGraph.watch).limit(1)).first()


def _retire(engine, mirror):
    if not mirror.url:
        return False
    with Session(engine) as session:
        item = session.exec(select(FocusEventGraph.watch, FocusEventGraph.graph_name, FocusEventGraph.retiring)
                            .where(FocusEventGraph.retiring != '').limit(1)).first()
    if item is None:
        return False
    key, current, names = item
    with mirror.exclusive(engine) as held:
        if not held:
            return False
        _owner(engine, mirror)
        for name in names.split(','):
            if name != current and (name == 'focus_' + key or
                                    name.startswith('events_focus_' + key + '_') and _GRAPH.fullmatch(name)):
                _drop(mirror, name)
        with Session(engine) as session:
            lock_news(session, 'watch:' + key)
            row = session.get(FocusEventGraph, key, options=(_without_snapshot(),))
            if row and row.retiring == names:
                row.retiring = ''
                session.add(row)
                session.commit()
    return True


def _claim(engine, key, lease):
    with Session(engine, expire_on_commit=False) as session:
        lock_news(session, 'watch:' + key, 'lease/' + lease.key)
        lease.confirm(session)
        watch = require_active(session, key)
        row = session.get(FocusEventGraph, key, options=(_without_snapshot(),))
        scheduled = row.state == 'ready' and row.scope_version == watch.version
        if row.scope_version != watch.version or row.state == 'ready':
            _advance(row)
        row.scope_version, row.state = watch.version, 'building'
        row.error = ''
        session.commit()
        return row, watch, scheduled


def _publish(engine, lease, row, snapshot, name, *, unchanged=False):
    with Session(engine) as session:
        lock_news(session, 'watch:' + row.watch, 'lease/' + lease.key)
        lease.confirm(session)
        watch = require_active(session, row.watch)
        state = session.get(FocusEventGraph, row.watch, options=(_without_snapshot(),))
        if state.token != row.token or watch.version != row.scope_version:
            return False
        if unchanged:
            state.state, state.refresh_at = 'ready', time.time() + REFRESH
            session.commit()
            return True
        retiring = {value for value in state.retiring.split(',') if value}
        if name:
            retiring.add('focus_' + row.watch)
        if state.graph_name and state.graph_name != name:
            retiring.add(state.graph_name)
        state.graph_name = name
        state.retiring = ','.join(sorted(retiring))
        state.snapshot = snapshot
        state.state, state.published_version, state.built_at = 'ready', row.scope_version, utc_now_iso()
        state.refresh_at = time.time() + REFRESH
        session.commit()
        return True


def _failed(engine, lease, row, error):
    with Session(engine) as session:
        lock_news(session, 'watch:' + row.watch, 'lease/' + lease.key)
        lease.confirm(session)
        state = session.get(FocusEventGraph, row.watch, options=(_without_snapshot(),))
        if state and state.token == row.token:
            state.state, state.error = 'error', type(error).__name__
            session.commit()


def has_work(engine, mirror):
    with Session(engine) as session:
        if _pick(session) is not None:
            return True
        return bool(mirror.url and session.exec(select(FocusEventGraph.watch)
                    .where(FocusEventGraph.retiring != '').limit(1)).first())


def step(engine, mirror):
    if not has_work(engine, mirror):
        return False
    with acquire(engine, 'focus-event-graph', seconds=1800) as lease:
        if lease is None:
            return False
        with Session(engine) as session:
            picked = _pick(session)
            key = picked[1].key if picked else ''
        if not key:
            return _retire(engine, mirror)
        row, watch, scheduled = _claim(engine, key, lease)
        stage = _stage(key, row.token) if mirror.url else ''
        try:
            with Session(engine) as session:
                linked = has_linked_reports(session, watch)
            if linked:
                _index(engine)
            lease.touch()
            with Session(engine) as session:
                snapshot = build(session, watch, row.days, row.until)
                encoded = json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))
                same = scheduled and bool(session.exec(select(FocusEventGraph.watch).where(
                    FocusEventGraph.watch == key, FocusEventGraph.token == row.token,
                    FocusEventGraph.snapshot == encoded)).first())
            lease.touch()
            if stage:
                with mirror.exclusive(engine) as held:
                    if not held:
                        return False
                    _owner(engine, mirror)
                    if same and row.graph_name and mirror.client.exists(mirror.prefix + '_' + row.graph_name):
                        _publish(engine, lease, row, encoded, row.graph_name, unchanged=True)
                    else:
                        _mirror(mirror, stage, key, snapshot, lease)
                        if not _publish(engine, lease, row, encoded, stage):
                            _drop(mirror, stage)
            else:
                _publish(engine, lease, row, encoded, '', unchanged=same and not row.graph_name)
        except Exception as error:
            _failed(engine, lease, row, error)
            # A failed graph may have been written partially. Its same token is
            # reused on retry, and readers still reference the last publication.
            raise
        return True


def status(session, key):
    watch = require_active(session, key)
    row = session.get(FocusEventGraph, key, options=(_without_snapshot(),))
    return _status(watch, row)


def _status(watch, row):
    if row is None:
        return dict(state='unbuilt', error='', built_at='')
    state = row.state if row.scope_version == watch.version else 'queued'
    return dict(state=state, error=row.error if state == 'error' else '',
                built_at=row.built_at if row.published_version == watch.version else '',
                requested={'days':row.days, 'until':row.until})


def read(session, key, *, lens, limit):
    watch = require_active(session, key)
    row = session.get(FocusEventGraph, key)
    status = _status(watch, row)
    snapshot = json.loads(row.snapshot) if row and row.snapshot and row.published_version == watch.version else {}
    nodes, edges = snapshot.get('nodes', []), snapshot.get('edges', [])
    selected_nodes, selected_edges, clipped = _select(nodes, edges, lens, limit)
    truncated = bool(snapshot.get('truncated') or clipped)
    if snapshot and not nodes:
        empty = '这个时间范围没有可关联的 GDELT 官方事件。'
    elif snapshot and lens == 'people' and not selected_nodes:
        empty = '当前报道尚未匹配到含人物或机构的 GKG 记录。'
    else:
        empty = ''
    return dict(view='focus', focus={'key': key, 'name': watch.name}, backend='official_focus',
                **status, graph=row.graph_name if snapshot and row else '', node_limit=limit,
                window={key: snapshot[key] for key in ('date_from', 'date_to') if key in snapshot},
                counts=snapshot.get('counts', {}), truncated=truncated, nodes=selected_nodes, edges=selected_edges,
                empty_reason=empty)


def _select(nodes, edges, lens, limit):
    if lens == 'people':
        statements = [r for r in edges if r['kind'] == 'GKG_MENTIONS']
        reports = {r['source'] for r in statements}
        links = [r for r in edges if r['kind'] == 'MENTIONED_IN' and r['target'] in reports]
        included = {i for link in statements + links for i in (link['source'], link['target'])}
        nodes = [node for node in nodes if node['id'] in included]
        edges = links + statements
    by_id = {node['id']: node for node in nodes}
    outgoing = defaultdict(list)
    for edge in edges:
        outgoing[edge['source']].append(edge['target'])
    roots = [node['id'] for node in nodes if node['kind'] == 'SourceEvent']
    slots = min(len(roots), max(1, limit // 4))
    seen = dict.fromkeys(roots[:slots])
    level = list(seen)
    while level and len(seen) < limit:
        next_level = []
        rows = [iter(outgoing[ident]) for ident in level]
        while rows and len(seen) < limit:
            more = []
            for neighbors in rows:
                ident = next(neighbors, None)
                if ident is not None:
                    more.append(neighbors)
                    if ident not in seen:
                        seen[ident] = None
                        next_level.append(ident)
                        if len(seen) == limit:
                            break
            rows = more
        level = next_level
    for ident in roots[slots:]:
        if len(seen) == limit:
            break
        seen[ident] = None
    visible_edges = [edge for edge in edges if edge['source'] in seen and edge['target'] in seen]
    return [by_id[ident] for ident in seen], visible_edges[:limit * 4], (
        len(seen) < len(nodes) or len(visible_edges) > limit * 4)
