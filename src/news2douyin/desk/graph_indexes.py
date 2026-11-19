"""Background, additive FalkorDB range-index readiness checks.

Call ``ensure_step`` only after the graph namespace's owner was verified and
while holding GraphMirror's global projection lease. It issues at most one
CREATE per call and never waits for a build to finish. Status readers do no I/O.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import time


AUDIT_INTERVAL = 300.0
POLL_INTERVAL = .5
ERROR_INTERVAL = 5.0

_BASE_INDEXES = tuple((label, 'id') for label in (
    'GraphMeta', 'Event', 'Article', 'Entity', 'EventMention', 'Argument',
    'Referent', 'OccurrenceCandidate', 'Focus')) + (('Place', 'iso'),)
GLOBAL_INDEXES = _BASE_INDEXES + tuple((label, 'key') for label in (
    'AttentionTopic', 'AttentionSourceItem', 'SourceEvent', 'GdeltDocument', 'GdeltActor', 'GdeltPlace', 'GkgSubject'))
FOCUS_INDEXES = _BASE_INDEXES + tuple((label, 'id') for label in (
    'Question', 'OutlinePoint', 'Investigation', 'ResearchQuestion', 'Claim',
    'EvidenceRef', 'Report', 'InterestScope', 'InterestEntity', 'InterestPattern'))

# Explicit columns make the wire protocol independent of the order in which
# db.indexes() exposes its other fields (options/language/stopwords/info).
INVENTORY_QUERY = ('CALL db.indexes() YIELD label, properties, types, entitytype, status '
                   'RETURN label, properties, types, entitytype, status')
_RANGE_TYPES = {'RANGE', 'EXACT-MATCH', 'EXACT_MATCH', 'EXACT MATCH'}
_READY = {'OPERATIONAL'}
_FAILED = {'FAILED', 'FAILURE', 'ERROR'}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')


def required_indexes(graph):
    if graph == 'active_global':
        return GLOBAL_INDEXES
    if isinstance(graph, str) and re.fullmatch(r'focus_[A-Za-z0-9_-]{1,100}', graph):
        return FOCUS_INDEXES
    raise ValueError('unsupported_graph_index_namespace')


def _states(mirror):
    states = getattr(mirror, '_graph_index_states', None)
    if states is None:
        states = {}
        mirror._graph_index_states = states
    return states


def _new_state(graph):
    return dict(graph=graph, state='checking', ready=0, required=len(required_indexes(graph)),
                current='', error='', checked_at='', updated='', ready_keys=frozenset(),
                next_check=0.0, checked_monotonic=0.0)


def _public(state):
    return {key: state[key] for key in ('graph', 'state', 'ready', 'required',
                                       'current', 'error', 'checked_at', 'updated')}


def _strings(value):
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _strings(item)]
    return []


def _mapping(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, dict):
        return value
    if isinstance(value, (list, tuple)) and value:
        # Redis RESP2 maps may arrive as pairs or an alternating flat sequence.
        if all(isinstance(item, (list, tuple)) and len(item) == 2
               and isinstance(item[0], str) for item in value):
            return dict(value)
        if len(value) % 2 == 0 and all(isinstance(value[i], str) for i in range(0, len(value), 2)):
            # A plain list of type names is not a property -> type map.
            if not any(value[i].upper() in _RANGE_TYPES | {'FULLTEXT', 'VECTOR'}
                       for i in range(0, len(value), 2)):
                return dict(zip(value[::2], value[1::2]))
    return None


def _range_property(property_name, types):
    if isinstance(types, str):
        # FalkorDB 4.22 RESP2 renders the property/type map as a string, e.g.
        # "{id: [RANGE]}" rather than returning a JSON object.
        entry = re.search(r'(?:^|[{,]\s*)[\'"]?'+re.escape(property_name)+
                          r'[\'"]?\s*:\s*\[([^\]]*)\]', types)
        if entry:
            return bool({item.strip(' \t\'"').upper() for item in entry[1].split(',')} & _RANGE_TYPES)
    mapping = _mapping(types)
    values = mapping.get(property_name, []) if mapping is not None else types
    return bool({item.upper() for item in _strings(values)} & _RANGE_TYPES)


def _inventory(rows):
    """Return node range-index statuses; full-text/vector indexes do not qualify."""
    result = {}
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) != 5:
            raise ValueError('invalid_graph_index_inventory')
        label, properties, types, entity_type, status = row
        if str(entity_type).upper() != 'NODE':
            continue
        if not isinstance(label, str) or not isinstance(status, str):
            raise ValueError('invalid_graph_index_inventory')
        if isinstance(properties, str):
            value = properties.strip()
            properties = ([item.strip(' \t\'"') for item in value[1:-1].split(',') if item.strip()]
                          if value.startswith('[') and value.endswith(']') else [value])
        if not isinstance(properties, (list, tuple)):
            raise ValueError('invalid_graph_index_inventory')
        for property_name in properties:
            if not isinstance(property_name, str):
                raise ValueError('invalid_graph_index_inventory')
            if _range_property(property_name, types):
                key = (label, property_name)
                # Some servers expose multiple records per label/property. A
                # ready node range index is sufficient even if another type is
                # simultaneously building.
                previous = result.get(key)
                if previous not in _READY:
                    result[key] = status.strip().upper()
    return result


def reset(mirror, graph=None):
    """Invalidate cached readiness after a reconnect, graph loss or user retry."""
    if graph is not None:
        required_indexes(graph)
    states = _states(mirror)
    if graph is None:
        states.clear()
    else:
        states.pop(graph, None)


def ensure_step(mirror, engine=None, graph='active_global'):
    """Check/add at most one missing index and return verified readiness.

    This function never creates a graph. The caller must create a trusted
    focus graph only after finding its SQL Watch and verifying the global
    graph owner. The ``engine`` argument is deliberately unused: no SQL
    Session or transaction is held during index commands.
    """
    required = required_indexes(graph)
    states = _states(mirror)
    state = dict(states.get(graph) or _new_state(graph))
    now = time.monotonic()
    mirror._index_last_work = False
    if not getattr(mirror, 'url', ''):
        state.update(state='unconfigured', ready=0, current='', error='', next_check=0.0,
                     ready_keys=frozenset(), checked_at=_now(), updated=_now())
        states[graph] = state
        return False
    if not getattr(mirror, 'bootstrapped', False):
        state.update(state='checking', current='等待图归属检查', ready=0,
                     ready_keys=frozenset(), next_check=0.0, updated=_now())
        states[graph] = state
        return False
    if now < state['next_check']:
        return state['state'] == 'ready'
    try:
        indexes = _inventory(mirror.read(graph, INVENTORY_QUERY))
        ready_keys = frozenset(key for key, status in indexes.items() if status in _READY)
        ready = sum(key in ready_keys for key in required)
        state.update(ready=ready, ready_keys=ready_keys, required=len(required),
                     error='', current='', checked_at=_now(), updated=_now(),
                     checked_monotonic=now)
        building = [key for key in required if key in indexes and key not in ready_keys]
        failed = [key for key in building if set(re.findall(r'[A-Z]+', indexes[key])) & _FAILED]
        if failed:
            state.update(state='error', current='.'.join(failed[0]),
                         error='图索引构建失败，请检查图服务后重试', next_check=now + ERROR_INTERVAL)
        elif building:
            state.update(state='building', current='.'.join(building[0]), next_check=now + POLL_INTERVAL)
        else:
            missing = [key for key in required if key not in ready_keys]
            if missing:
                label, property_name = missing[0]
                # Names originate solely in required_indexes(), never user
                # data. The older spelling is supported by FalkorDB 4.x.
                mirror.query(graph, 'CREATE INDEX ON :'+label+'('+property_name+')')
                mirror._index_last_work = True
                state.update(state='building', current=label+'.'+property_name,
                             next_check=now + POLL_INTERVAL)
            else:
                state.update(state='ready', next_check=now + AUDIT_INTERVAL)
    except Exception as exc:
        # Driver messages can contain credentials/URLs/Cypher. A safe type is
        # enough for diagnostics and preserves the index work for retry.
        state.update(state='error', error='图索引检查未完成（'+type(exc).__name__+'）',
                     ready_keys=frozenset(), ready=0, next_check=now + ERROR_INTERVAL,
                     checked_at=_now(), updated=_now())
    states[graph] = state
    return state['state'] == 'ready'


def ready_for(mirror, graph, label, property_name='id'):
    """A cheap cached guard used by bounded GC; it never sends graph commands."""
    state = _states(mirror).get(graph)
    if not state or state['state'] not in ('ready', 'building'):
        return False
    if time.monotonic() - state['checked_monotonic'] >= AUDIT_INTERVAL:
        return False
    return (label, property_name) in state['ready_keys']


def status_summary(mirror):
    """Snapshot-only status suitable for GET endpoints and server rendering."""
    states = list(_states(mirror).values())
    if not getattr(mirror, 'url', ''):
        return dict(state='unconfigured', ready=0, required=len(GLOBAL_INDEXES), current='',
                    error='', checked_at='', updated='', graphs=[])
    if not states:
        return dict(state='checking', ready=0, required=len(GLOBAL_INDEXES), current='',
                    error='', checked_at='', updated='', graphs=[])
    priority = {'error':0, 'checking':1, 'building':2, 'unconfigured':3, 'ready':4}
    active = min(states, key=lambda item:(priority.get(item['state'], 1), item['graph']))
    result = _public(active)
    result['graphs'] = [_public(item) for item in sorted(states, key=lambda item:item['graph'])]
    return result
