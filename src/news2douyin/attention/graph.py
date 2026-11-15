"""Bounded, rebuildable attention overlay in the existing active_global graph.

PostgreSQL remains authoritative. This is a latest-state navigation projection,
not the complete observation archive or an article/fact store. Each topic keeps
at most 30 latest source items, 30 event links and 30 focus recall links. Times
on attention nodes are *observed* times; publication and audience counts are
never inferred. A candidate/confirmed match concerns identity/relevance only,
not truth or the probability that the reported event occurred.

The GraphMirror caller owns its projection lease and conditional outbox-token
acknowledgement. No SQL transaction is retained during external graph I/O.
"""
from __future__ import annotations

import json
from math import isfinite
from uuid import uuid4

from sqlmodel import Session, select

from .models import AttentionTopic, AttentionItem, AttentionEventMatch, AttentionWatchMatch
from ..desk.models import Watch
from ..desk.focus_lifecycle import visible_watch
from ..intake.official_models import OfficialEvent


OWNER = 'attention/v1'
ITEM_LIMIT = 30
MATCH_LIMIT = 30
FOCUS_LIMIT = 30


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return value if isfinite(value) else None


def _json(value, default):
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return default
    return parsed if isinstance(parsed, type(default)) else default


def _reasons(value):
    # Keep arbitrary stored source text out of Cypher syntax and bound the
    # navigation payload, while the full explanation stays in PostgreSQL.
    return json.dumps([str(reason)[:500] for reason in _json(value, [])[:10]], ensure_ascii=False)


def _snapshot_ref(value):
    parsed = _json(value, {})
    encoded = json.dumps(parsed, ensure_ascii=False)
    return encoded if len(encoded) <= 8000 else json.dumps(
        {'truncated': True, 'preview': encoded[:7000]}, ensure_ascii=False)


def _load(engine, key):
    with Session(engine, expire_on_commit=False) as session:
        topic = session.get(AttentionTopic, key)
        if topic is None:
            return None, [], [], []
        items = list(session.exec(select(AttentionItem).where(AttentionItem.topic == key)
            .order_by(AttentionItem.last_seen.desc(), AttentionItem.key).limit(ITEM_LIMIT)
            .execution_options(n2d_query_name='attention.graph.items')))
        # Never manufacture a SourceEvent from a dangling match. GDELT's
        # compact SQL source index supplies its distinct upstream identity.
        matches = list(session.exec(select(AttentionEventMatch, OfficialEvent)
            .join(OfficialEvent, OfficialEvent.event_id == AttentionEventMatch.event_id)
            .where(AttentionEventMatch.topic == key,
                   AttentionEventMatch.status.in_(('candidate', 'confirmed', 'rejected')))
            .order_by(AttentionEventMatch.updated_at.desc(), AttentionEventMatch.key).limit(MATCH_LIMIT)
            .execution_options(n2d_query_name='attention.graph.events')))
        focuses = list(session.exec(select(AttentionWatchMatch, Watch)
            .join(Watch, Watch.key == AttentionWatchMatch.watch)
            .where(AttentionWatchMatch.topic == key, visible_watch(),
                   AttentionWatchMatch.scope_version == Watch.version)
            .order_by(AttentionWatchMatch.updated_at.desc(), AttentionWatchMatch.key).limit(FOCUS_LIMIT)
            .execution_options(n2d_query_name='attention.graph.focuses')))
    return topic, items, matches, focuses


def invalidate_focus(mirror, key, version, *, deleted=False):
    """Watch projection invalidates recall without changing Focus lifecycle.

    A constant-size root marker makes all older edges invisible immediately.
    Graph readers must require r.active AND f.attention_recall_visible AND
    r.scope_version=f.attention_scope_version (and no f.archived). At most 100
    old edges additionally receive a historical invalidation flag per pass.
    No high-degree watch can turn one projection into a whole-graph update.
    """
    mirror.query('active_global',
        'MATCH (f:Focus {id:$key}) '
        'SET f.attention_scope_version=$version,f.attention_recall_visible=$visible',
        key=key, version=version, visible=not deleted)
    mirror.query('active_global',
        'MATCH (f:Focus {id:$key})<-[r:CANDIDATE_FOCUS]-(t:AttentionTopic {projection_owner:$owner}) '
        'WHERE r.projection_owner=$owner AND r.active=true AND ($deleted OR r.scope_version<>$version) '
        'WITH r LIMIT 100 '
        'SET r.active=false,r.status=$status,r.invalid_reason=$reason',
        owner=OWNER, key=key, version=version, deleted=deleted,
        status='invalidated', reason='focus_deleted' if deleted else 'scope_changed')


def project(engine, mirror, key):
    """Replay one bounded topic using stable identities and scoped stamp pruning."""
    topic, items, matches, focuses = _load(engine, key)
    q = lambda query, **params: mirror.query('active_global', query, **params)
    if topic is None:
        # Derived items are topic-owned. Shared official events and Focus nodes
        # are deliberately not cleanup targets, even when now unreferenced.
        q('MATCH (s:AttentionSourceItem {topic_key:$key,projection_owner:$owner}) DETACH DELETE s',
          key=key, owner=OWNER)
        q('MATCH (t:AttentionTopic {key:$key,projection_owner:$owner}) DETACH DELETE t', key=key, owner=OWNER)
        return
    stamp = uuid4().hex
    score, rise = _number(topic.attention_score), _number(topic.rise_score)
    q('MERGE (t:AttentionTopic {key:$key}) '
      'SET t.projection_owner=$owner,t.title=$title,t.score=$score,t.score_known=$known,'
      't.rise_score=$rise,t.rise_known=$rise_known,t.generation=$generation,'
      't.first_seen=$first,t.last_seen=$last,t.time_kind=$time_kind,'
      't.baseline_state=$baseline,t.platform_count=$platforms,t.item_count=$items,'
      't.item_limit=$item_limit,t.match_limit=$match_limit,t.focus_limit=$focus_limit,'
      't.metric_kind=$metric_kind,t.truth_status=$truth_status',
      key=key, owner=OWNER, title=topic.title, score=score, known=score is not None,
      rise=rise, rise_known=rise is not None, generation=topic.generation,
      first=topic.first_seen, last=topic.last_seen, time_kind='observed',
      baseline=topic.baseline_state, platforms=topic.platform_count, items=topic.item_count,
      item_limit=ITEM_LIMIT, match_limit=MATCH_LIMIT, focus_limit=FOCUS_LIMIT,
      metric_kind='platform_attention_proxy', truth_status='not_assessed')
    rows = []
    for item in items:
        # Item keys are topic-scoped in the graph; a regrouped PostgreSQL item
        # cannot rewrite another topic's historical/derived projection node.
        rows.append([key+'/'+item.key, item.key, item.source_id, item.platform,
                     item.source_kind, item.audience_scope, item.native_id,
                     item.url, item.canonical_url, item.title, item.excerpt[:8000],
                     item.first_seen, item.last_seen, item.list_position,
                     _snapshot_ref(item.snapshot_ref_json)])
    if rows:
        q('MATCH (t:AttentionTopic {key:$key}) UNWIND $rows AS x '
          'MERGE (s:AttentionSourceItem {key:x[0]}) '
          'SET s.projection_owner=$owner,s.topic_key=$key,s.item_key=x[1],s.source_id=x[2],'
          's.platform=x[3],s.source_kind=x[4],s.audience_scope=x[5],s.native_id=x[6],'
          's.url=x[7],s.canonical_url=x[8],s.title=x[9],s.excerpt=x[10],'
          's.first_seen=x[11],s.last_seen=x[12],s.list_position=x[13],s.snapshot_ref_json=x[14],'
          's.time_kind=$time_kind,s.material_kind=$material_kind,s.stamp=$stamp '
          'MERGE (t)-[r:OBSERVED_ITEM]->(s) SET r.projection_owner=$owner,r.stamp=$stamp',
          key=key, owner=OWNER, rows=rows, stamp=stamp, time_kind='observed',
          material_kind='headline_metadata_not_article_evidence')
    q('MATCH (s:AttentionSourceItem {topic_key:$key,projection_owner:$owner}) '
      'WHERE s.stamp<>$stamp DETACH DELETE s', key=key, owner=OWNER, stamp=stamp)
    event_rows = []
    for match, event in matches:
        current = bool(topic.generation and match.generation == topic.generation)
        active = current and match.status in ('candidate', 'confirmed')
        event_rows.append(['gdelt/'+str(event.event_id), str(event.event_id), event.event_date,
            event.actor1_name, event.actor2_name, event.event_code, event.event_root_code,
            event.action_country_iso2, event.action_place, event.source_url, event.first_seen,
            event.last_seen, match.key, match.status, match.method, _number(match.score),
            _reasons(match.reasons_json), match.generation, active,
            '' if current else 'generation_changed'])
    if event_rows:
        q('MATCH (t:AttentionTopic {key:$key}) UNWIND $rows AS x '
          'MERGE (e:SourceEvent {key:x[0]}) '
          'SET e.provider=$provider,e.external_id=x[1],e.event_date=x[2],e.actor1_name=x[3],'
          'e.actor2_name=x[4],e.event_code=x[5],e.event_root_code=x[6],e.action_country_iso2=x[7],'
          'e.action_place=x[8],e.source_url=x[9],e.first_seen=x[10],e.last_seen=x[11] '
          'MERGE (t)-[r:CANDIDATE_MATCH {key:x[12]}]->(e) '
          'SET r.projection_owner=$owner,r.status=x[13],r.method=x[14],r.score=x[15],'
          'r.reasons_json=x[16],r.generation=x[17],r.active=x[18],r.invalid_reason=x[19],'
          'r.score_kind=$score_kind,r.truth_status=$truth_status,r.stamp=$stamp',
          key=key, owner=OWNER, rows=event_rows, provider='gdelt', stamp=stamp,
          score_kind='rule_score_not_probability', truth_status='not_assessed')
    q('MATCH (:AttentionTopic {key:$key})-[r:CANDIDATE_MATCH]->() '
      'WHERE r.projection_owner=$owner AND r.stamp<>$stamp DELETE r', key=key, owner=OWNER, stamp=stamp)
    focus_rows = [[match.key, watch.key, match.scope_version, _reasons(match.reasons_json)]
                  for match, watch in focuses]
    if focus_rows:
        # MATCH, not MERGE: only the dedicated watch projection creates Focus
        # and controls archived/enabled. A missing/archived root never revives.
        q('MATCH (t:AttentionTopic {key:$key}) UNWIND $rows AS x '
          'MATCH (f:Focus {id:x[1]}) WHERE coalesce(f.archived,false)=false '
          'SET f.attention_scope_version=x[2],f.attention_recall_visible=true '
          'MERGE (t)-[r:CANDIDATE_FOCUS {key:x[0]}]->(f) '
          'SET r.projection_owner=$owner,r.scope_version=x[2],r.reasons_json=x[3],'
          'r.method=$method,r.status=$status,r.active=true,r.stamp=$stamp,r.invalid_reason=$empty',
          key=key, owner=OWNER, rows=focus_rows, stamp=stamp, method='keyword_recall',
          status='candidate', empty='')
    q('MATCH (:AttentionTopic {key:$key})-[r:CANDIDATE_FOCUS]->() '
      'WHERE r.projection_owner=$owner AND r.stamp<>$stamp DELETE r',
      key=key, owner=OWNER, stamp=stamp)
