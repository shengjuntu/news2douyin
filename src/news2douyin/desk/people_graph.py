"""Focus-scoped network over the same source evidence as the person timelines."""
from collections import defaultdict
from itertools import combinations
from urllib.parse import quote, urlencode

from ..extraction.schema import LABELS, stable_id
from .people_evidence import time_order
from .people_view import _source_path, empty_reason, read_focus_evidence

# A long participant list is still useful in timelines, without drawing a clique.
MAX_JOINT_PEOPLE = 8
EDGE_LIMIT = 320
ACTIVITY_PREVIEW = 5
EVIDENCE_PREVIEW = 3
EDGE_LABELS = {
    'member_of': '成员关系', 'serves_at': '任职', 'represents': '代表',
    'joint_action': '共同参与行动', 'action_context': '同一行动材料提及',
    'joint_participation': '同一行动的参与方',
}
DIRECTED = {'member_of', 'serves_at', 'represents'}


def _excerpt(text, phrase):
    if len(text) <= 600:
        return text
    position = text.find(phrase) if phrase else 0
    start = max(0, min(position - 80, len(text) - 600))
    return ('…' if start else '') + text[start:start + 600] + ('…' if start + 600 < len(text) else '')


def _evidence(action, phrase=''):
    event = action['event_key']
    return dict(evidence_key=action['evidence_key'], evidence=_excerpt(action['quote'], phrase),
                source_path=_source_path(action), event_title=action['event_title'],
                event_path='/events/' + quote(event, safe='') if event else '',
                time=action['time'], time_kind=action['time_kind'],
                status=action['status'], status_label=LABELS[action['status']])


def _collect_links(projected, entities, activities):
    # Map each unique source action to its literal supporting phrase when available.
    links = defaultdict(dict)
    for row in projected['relations']:
        key = (row['person_key'], row['organization_key'], row['relation'], row['status'])
        links[key][row['action_key']] = row['literal_phrase']

    crowded = 0
    for action in projected['actions']:
        participants = set()
        for arg in action['arguments']:
            key = arg['entity_key']
            activities[key][action['key']].add(arg['role'])
            if arg['participation'] and entities[key]['kind'] == 'Person':
                participants.add(key)
        if len(participants) > MAX_JOINT_PEOPLE:
            crowded += 1
            continue
        for left, right in combinations(sorted(participants), 2):
            links[left, right, 'joint_participation', action['status']][action['key']] = ''
    return links, crowded


def people_graph(session, focus, *, days=30, until='', limit=240):
    """Read only current focus membership, article revisions and sourced actions."""
    if type(limit) is not int or not 1 <= limit <= 800:
        raise ValueError('人物图节点上限须在1至800之间')
    projected, window = read_focus_evidence(session, focus, days, until)
    entities = {row['key']: row for row in projected['entities']}
    actions = {row['key']: row for row in projected['actions']}
    times = {key: (time_order(row['time']), key) for key, row in actions.items()}
    activities = defaultdict(lambda: defaultdict(set))
    links, crowded = _collect_links(projected, entities, activities)

    # Prioritize literal relations, then the number of distinct source actions. Admit both
    # endpoints together so that a small display cap still yields usable edges.
    ranked = sorted(links, key=lambda k: (k[2] not in DIRECTED, -len(links[k]), k))
    shown = {}
    edges = []
    edge_limit = min(EDGE_LIMIT, limit * 3)
    for key in ranked:
        source, target, kind, status = key
        if len(edges) == edge_limit:
            break
        missing = (source not in shown) + (target not in shown)
        if len(shown) + missing > limit:
            continue
        shown[source] = shown[target] = None
        keys = sorted(links[key], key=times.__getitem__, reverse=True)
        label = EDGE_LABELS[kind]
        if status != 'reported':
            label = LABELS[status] + ' · ' + label
        edges.append(dict(id='people_edge:' + stable_id(*key), source=source, target=target,
                          kind=kind, label=label, directed=kind in DIRECTED, status=status,
                          evidence_count=len(keys),
                          evidence=[_evidence(actions[k], links[key][k]) for k in keys[:EVIDENCE_PREVIEW]]))

    for key in sorted(activities, key=lambda k: (-len(activities[k]), entities[k]['name'].casefold(), k)):
        if len(shown) >= limit:
            break
        shown[key] = None

    nodes = []
    focus_key = focus if isinstance(focus, str) else focus.key
    for key in shown:
        entity = entities[key]
        url = ''
        recent = []
        if entity['kind'] == 'Person':
            url = '/focus/' + quote(focus_key, safe='') + '?' + urlencode({
                'view': 'people', 'person': key, 'people_days': days, 'people_until': window['date_to']})
            action_keys = sorted(activities[key], key=times.__getitem__, reverse=True)
            for action_key in action_keys[:ACTIVITY_PREVIEW]:
                row = _evidence(actions[action_key])
                roles = '、'.join(LABELS[role] for role in sorted(activities[key][action_key]))
                recent.append(dict(row, title=row['event_title'], role_label=roles))
        nodes.append(dict(id=key, kind=entity['kind'], title=entity['name'], url=url,
                          details={'identity': entity['identity_label']}, provisional=entity['provisional'],
                          activities=recent, activity_count=len(activities[key])))

    truncated = bool(window['truncated'] or crowded or len(shown) < len(activities) or len(edges) < len(links))
    window.update(crowded_actions=crowded, max_joint_people=MAX_JOINT_PEOPLE)
    return dict(view='focus', lens='people', focus=focus_key, backend='focus_evidence',
                state='ready' if nodes else 'empty', reason='', days=days, window=window,
                nodes=nodes, edges=edges, node_limit=limit, edge_limit=edge_limit,
                truncated=truncated, empty_reason='' if nodes else empty_reason(window))
