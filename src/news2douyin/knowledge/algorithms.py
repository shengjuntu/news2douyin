"""Bounded, deterministic graph algorithms. Similarity never establishes causation."""
from collections import defaultdict
from datetime import datetime
from itertools import combinations
from math import log1p
from ..extraction.schema import stable_id


def pagerank(nodes, edges, iterations=40, damping=.85, seeds=None):
    nodes = sorted(nodes)
    if not nodes:
        return {}
    personal = {n: float((seeds or {}).get(n, 0)) for n in nodes}
    mass = sum(personal.values())
    personal = {n: personal[n]/mass if mass else 1/len(nodes) for n in nodes}
    rank = dict(personal)
    adjacency = {n: {} for n in nodes}
    for a, b, weight in edges:
        if a != b and a in adjacency and b in adjacency and weight > 0:
            adjacency[a][b] = adjacency[a].get(b, 0) + weight
            adjacency[b][a] = adjacency[b].get(a, 0) + weight
    totals = {n: sum(adj.values()) for n, adj in adjacency.items()}
    for _ in range(iterations):
        dangling = sum(rank[n] for n in nodes if not totals[n])
        updated = {n: (1-damping+damping*dangling)*personal[n] for n in nodes}
        for n, adj in adjacency.items():
            for target, weight in adj.items():
                updated[target] += damping*rank[n]*weight/totals[n]
        delta = sum(abs(updated[n]-rank[n]) for n in nodes)
        rank = updated
        if delta < 1e-10:
            break
    return rank


def communities(nodes, edges):
    """Deterministic weighted label propagation; these are related groups, not event IDs."""
    adjacent = {n: {} for n in nodes}
    labels = {n: n for n in nodes}
    for a, b, weight in edges:
        adjacent[a][b] = weight
        adjacent[b][a] = weight
    for _ in range(20):
        changed = False
        for n in sorted(nodes):
            scores = defaultdict(float)
            for other, weight in adjacent[n].items():
                scores[labels[other]] += weight
            if not scores:
                continue
            maximum = max(scores.values())
            tied = sorted(k for k, v in scores.items() if abs(v-maximum) < 1e-9)
            chosen = labels[n] if labels[n] in tied else tied[0]
            if chosen != labels[n]:
                labels[n] = chosen
                changed = True
        if not changed:
            break
    groups = defaultdict(list)
    for n, label in labels.items():
        groups[label].append(n)
    return {n: stable_id('community', sorted(group)) for group in groups.values() for n in group}


def analyze(events, mentions, participants):
    """Input rows come from FalkorDB, not keyword-index tables.

    Mentions: event, mention, kind, status, day, signature, article, duplicate_root, domain.
    Participants: event, entity ID, normalized name, role, mention ID.
    """
    nodes = {row[0] for row in events}
    dates = {r[0]: r[2] or '' for r in events}
    by_entity, by_signature = defaultdict(dict), defaultdict(set)
    material = defaultdict(list)
    for row in mentions:
        event, mention, kind, status, day, signature, article, root, domain = row
        if event not in nodes:
            continue
        material[event].append(row)
        if signature:
            by_signature[signature].add(event)
    for event, entity, name, role, mention in participants:
        if event in nodes and role in ('actor', 'speaker', 'participant'):
            by_entity[entity].setdefault(event, []).append(mention)
    links = {}
    def add(a, b, weight, reason):
        a, b = sorted((a, b))
        if a == b:
            return
        item = links.setdefault((a,b), {'weight': 0., 'evidence': []})
        item['weight'] += weight
        if len(item['evidence']) < 8:
            item['evidence'].append(reason)
    for signature, members in by_signature.items():
        if len(members) <= 60:
            for a, b in combinations(sorted(members), 2):
                add(a,b,3.,dict(kind='same_action_candidate',signature=signature))
    def near(a, b):
        try:
            return abs((datetime.fromisoformat(dates[a].replace('Z','+00:00'))-
                        datetime.fromisoformat(dates[b].replace('Z','+00:00'))).days) <= 30
        except (ValueError, TypeError):
            return False
    for entity, members in by_entity.items():
        # Very common names cannot create quadratic cliques or dominate the ranking.
        if 2 <= len(members) <= 60:
            for a, b in combinations(sorted(members), 2):
                if near(a,b):
                    add(a,b,1/log1p(len(members)),dict(kind='shared_participant',entity=entity,
                        mentions=sorted(set(members[a]+members[b]))[:8]))
    edges = [(a,b,min(v['weight'],5.)) for (a,b),v in links.items()]
    rank = pagerank(nodes,edges)
    groups = communities(nodes,edges)
    maximum = max(rank.values(),default=1)
    degrees=defaultdict(int)
    for a,b,_ in edges:degrees[a]+=1;degrees[b]+=1
    results = {}
    for n in sorted(nodes):
        rows = material[n]
        roots = {r[7] or r[6] for r in rows}
        rows=[r for r in rows if r[1]]
        statuses = sorted({r[3] for r in rows})
        actions = {r[2] for r in rows if r[3]=='reported'}
        impact = max([{'attack':3,'decision':2,'signing':2,'protest':2,'meeting':1,'visit':1}.get(k,0) for k in actions],default=0)
        signatures = {r[5] or r[1] for r in rows}
        # Novelty counts changes of action, not number of articles/reposts.
        novelty = min(len(signatures),3)
        centrality = rank[n]/maximum if len(nodes)>1 and degrees[n] else 0
        evidence = min(len(roots),3)
        score = round(impact*15 + novelty*6 + evidence*4 + centrality*15,2)
        results[n] = dict(score=score,band='优先看' if score>=60 else '优先核查' if score>=35 else '其他进展',community=groups[n],impact=impact,novelty=novelty,
            centrality=round(centrality,4),source_groups=len(roots),statuses=statuses,
            mentions=len({r[1] for r in rows}),relations=degrees[n],
            reason=f'行动影响 {impact}/3 · 不同陈述 {novelty}/3 · 来源去重组 {len(roots)} · 关联度 {centrality:.2f}',
            caveat='来源去重组不等于独立证实；关联群组不等于同一事件。')
    return results, links, edges
