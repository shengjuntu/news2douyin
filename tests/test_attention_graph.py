"""Real FalkorDB checks for the bounded, source-separated attention overlay."""
import json
import time

import pytest

from sqlalchemy import event
from sqlmodel import Session, select

from news2douyin.attention.graph import ITEM_LIMIT, OWNER, invalidate_focus, project
from news2douyin.attention.models import AttentionTopic, AttentionItem, AttentionEventMatch, AttentionWatchMatch
from news2douyin.desk import graph_indexes
from news2douyin.desk.graph import GraphMirror
from news2douyin.desk.graph_queue import pick
from news2douyin.desk.models import GraphPending, Watch, WatchEvent
from news2douyin.desk.focus_models import FocusTrash
from news2douyin.desk.service import enqueue_graph
from news2douyin.intake.official_models import OfficialEvent
from test_content_foundation import app_at, live_graph


def settle(app, mirror, monkeypatch):
    monkeypatch.setattr(graph_indexes, 'POLL_INTERVAL', 0)
    for _ in range(300):
        changed = mirror.step(app.state.engine)
        if not changed and mirror.work_state != 'indexes':
            return
        if not changed:
            time.sleep(.005)
    raise AssertionError('attention projection queue did not settle')


def seed(app, *, event_match=True, count=1):
    with Session(app.state.engine) as s:
        s.add(AttentionTopic(key='topic-a', title='具体新闻进展', generation='generation-one',
            first_seen='2026-11-13T00:00:00Z', last_seen='2026-11-14T00:00:00Z', item_count=count))
        for i in range(count):
            s.add(AttentionItem(key='item-'+str(i), topic='topic-a', source_id='source-a',
                title='原始标题 '+str(i), excerpt='只是一段榜单摘要',
                last_seen='2026-11-14T00:'+str(i).zfill(2)+':00Z'))
        if event_match:
            s.add(OfficialEvent(event_id='123', event_date='20261113', feed='events',
                stamp='20261114000000', row_index=1, actor1_name='A', actor2_name='B'))
            s.add(AttentionEventMatch(key='match-a', topic='topic-a', event_id='123',
                method='conservative_rules_v1', score=8, generation='generation-one',
                reasons_json=json.dumps(['国家与动作一致'], ensure_ascii=False)))
        enqueue_graph(s, 'attention/topic-a')
        s.commit()


def test_unknowns_are_unknown_and_overlay_is_only_in_existing_global_graph(tmp_path, live_graph, monkeypatch):
    app = app_at(tmp_path); mirror = GraphMirror()
    seed(app, event_match=False)
    settle(app, mirror, monkeypatch)
    row = mirror.read('active_global', 'MATCH (t:AttentionTopic {key:$key}) '
        'RETURN t.score,t.score_known,t.rise_score,t.rise_known,t.time_kind,t.truth_status', key='topic-a')[0]
    assert row == [None, 'false', None, 'false', 'observed', 'not_assessed']
    row = mirror.read('active_global', 'MATCH (s:AttentionSourceItem) '
        'RETURN s.published_at,s.audience_count,s.audience_scope,s.source_kind,s.list_position,s.material_kind')[0]
    assert row == [None, None, 'unknown', 'unknown', None, 'headline_metadata_not_article_evidence']
    assert mirror.read('active_global', 'MATCH (e:Event) RETURN count(e)') == [[0]]
    assert mirror.read('active_global', 'MATCH (a:Article) RETURN count(a)') == [[0]]
    assert live_graph.keys('test_live_*') == ['test_live_active_global']
    for label in ('AttentionTopic', 'AttentionSourceItem', 'SourceEvent'):
        assert (label, 'key') in graph_indexes.GLOBAL_INDEXES
        assert (label, 'key') not in graph_indexes.FOCUS_INDEXES
        assert graph_indexes.ready_for(mirror, 'active_global', label, 'key')
    assert 'Index Scan' in str(mirror.explain('active_global',
        'MATCH (t:AttentionTopic {key:$key}) RETURN t.title', key='topic-a'))


def test_idempotent_bounded_items_and_source_match_lifecycle(tmp_path, live_graph, monkeypatch):
    app = app_at(tmp_path); mirror = GraphMirror()
    seed(app, count=ITEM_LIMIT+2)
    settle(app, mirror, monkeypatch)
    with Session(app.state.engine) as s:
        enqueue_graph(s, 'attention/topic-a'); s.commit()
    settle(app, mirror, monkeypatch)
    assert mirror.read('active_global', 'MATCH (:AttentionTopic)-[r:OBSERVED_ITEM]->() RETURN count(r)') == [[ITEM_LIMIT]]
    assert mirror.read('active_global', 'MATCH (s:AttentionSourceItem) RETURN count(s)') == [[ITEM_LIMIT]]
    assert mirror.read('active_global', 'MATCH (s:SourceEvent) RETURN s.key,s.provider,s.external_id') == [['gdelt/123','gdelt','123']]
    assert mirror.read('active_global', 'MATCH (:AttentionTopic)-[r:CANDIDATE_MATCH]->(:SourceEvent) '
        'RETURN r.status,r.active,r.score,r.score_kind') == [['candidate','true','8','rule_score_not_probability']]
    for status, active in (('confirmed', 'true'), ('rejected', 'false')):
        with Session(app.state.engine) as s:
            match = s.get(AttentionEventMatch, 'match-a'); match.status = status; s.add(match)
            enqueue_graph(s, 'attention/topic-a'); s.commit()
        settle(app, mirror, monkeypatch)
        assert mirror.read('active_global', 'MATCH (:AttentionTopic)-[r:CANDIDATE_MATCH]->() '
            'RETURN r.status,r.active') == [[status,active]]
    with Session(app.state.engine) as s:
        topic = s.get(AttentionTopic, 'topic-a'); topic.generation = 'generation-two'; topic.attention_score = 0
        s.add(topic)
        match = s.get(AttentionEventMatch, 'match-a'); match.status = 'confirmed'; s.add(match)
        s.add(AttentionEventMatch(key='missing-upstream', topic='topic-a', event_id='999',
            generation=topic.generation))
        for item in s.exec(select(AttentionItem).where(AttentionItem.topic=='topic-a')):
            s.delete(item)
        enqueue_graph(s, 'attention/topic-a'); s.commit()
    settle(app, mirror, monkeypatch)
    assert mirror.read('active_global', 'MATCH (t:AttentionTopic) RETURN t.score,t.score_known') == [['0','true']]
    assert mirror.read('active_global', 'MATCH (s:AttentionSourceItem) RETURN count(s)') == [[0]]
    assert mirror.read('active_global', 'MATCH (:AttentionTopic)-[r:CANDIDATE_MATCH]->() '
        'RETURN r.status,r.active,r.invalid_reason') == [['confirmed','false','generation_changed']]
    assert mirror.read('active_global', 'MATCH (s:SourceEvent) RETURN count(s)') == [[1]]
    assert mirror.read('active_global', 'MATCH (e:Event) RETURN count(e)') == [[0]]


def test_focus_recall_never_accepts_material_or_revives_archived_focus(tmp_path, live_graph, monkeypatch):
    app = app_at(tmp_path); mirror = GraphMirror()
    seed(app, event_match=False)
    with Session(app.state.engine) as s:
        s.add(Watch(key='w-active', name='范围', kind='主题', scope='范围', version=2))
        s.add(Watch(key='w-absent', name='未投影', kind='主题', scope='范围', version=1))
        s.add(Watch(key='w-deleted', name='已删除', kind='主题', scope='范围', version=2))
        s.add(FocusTrash(watch='w-deleted', name='已删除', deleted_at='2026-11-14T00:00:00Z'))
        s.add_all([AttentionWatchMatch(key='recall-active', topic='topic-a', watch='w-active', scope_version=2),
            AttentionWatchMatch(key='recall-missing', topic='topic-a', watch='w-absent', scope_version=1),
            AttentionWatchMatch(key='recall-deleted', topic='topic-a', watch='w-deleted', scope_version=2)])
        s.commit()
    mirror.query('active_global', 'CREATE (:Focus {id:$active,archived:false}),(:Focus {id:$deleted,archived:true})',
        active='w-active', deleted='w-deleted')
    project(app.state.engine,mirror,'topic-a')
    assert mirror.read('active_global', 'MATCH (f:Focus {id:$id}) RETURN count(f)', id='w-absent') == [[0]]
    settle(app, mirror, monkeypatch)
    assert mirror.read('active_global', 'MATCH (:AttentionTopic)-[r:CANDIDATE_FOCUS]->(f:Focus) '
        'RETURN f.id,r.status,r.active') == [['w-active','candidate','true']]
    assert mirror.read('active_global', 'MATCH (f:Focus {id:$id}) RETURN f.archived', id='w-deleted') == [['true']]
    with Session(app.state.engine) as s:
        assert list(s.exec(select(WatchEvent))) == []
        w = s.get(Watch,'w-active'); w.version = 3; s.add(w)
        enqueue_graph(s, 'watch/w-active'); s.commit()
    settle(app, mirror, monkeypatch)
    assert mirror.read('active_global', 'MATCH (:AttentionTopic)-[r:CANDIDATE_FOCUS]->(f:Focus {id:$id}) '
        'RETURN r.active,r.status,r.invalid_reason', id='w-active') == [['false','invalidated','scope_changed']]
    with Session(app.state.engine) as s:
        s.add(FocusTrash(watch='w-active', name='范围', deleted_at='2026-11-14T00:01:00Z'))
        enqueue_graph(s, 'watch/w-active'); enqueue_graph(s, 'attention/topic-a'); s.commit()
    settle(app, mirror, monkeypatch)
    assert mirror.read('active_global', 'MATCH (f:Focus {id:$id}) RETURN f.archived,f.enabled', id='w-active') == [['true','false']]
    assert mirror.read('active_global', 'MATCH (:AttentionTopic)-[r:CANDIDATE_FOCUS]->(f:Focus {id:$id}) '
        'WHERE r.active=true AND f.attention_recall_visible=true '
        'AND r.scope_version=f.attention_scope_version AND coalesce(f.archived,false)=false '
        'RETURN r.active', id='w-active') == []


def test_missing_topic_cleanup_only_removes_its_overlay(tmp_path, live_graph, monkeypatch):
    app = app_at(tmp_path); mirror = GraphMirror()
    seed(app); settle(app, mirror, monkeypatch)
    mirror.query('active_global', 'CREATE (:Focus {id:$id}),(:AttentionTopic {key:$key,projection_owner:$owner})',
        id='shared-focus', key='foreign-topic', owner='some-other-projection')
    mirror.query('active_global', 'MATCH (t:AttentionTopic {key:$key}),(f:Focus {id:$id}) '
        'CREATE (t)-[:CANDIDATE_FOCUS {projection_owner:$owner}]->(f)',
        key='topic-a', id='shared-focus', owner=OWNER)
    with Session(app.state.engine) as s:
        s.delete(s.get(AttentionTopic,'topic-a')); enqueue_graph(s,'attention/topic-a'); s.commit()
    settle(app, mirror, monkeypatch)
    assert mirror.read('active_global', 'MATCH (t:AttentionTopic) RETURN t.key') == [['foreign-topic']]
    assert mirror.read('active_global', 'MATCH (s:AttentionSourceItem) RETURN count(s)') == [[0]]
    assert mirror.read('active_global', 'MATCH (s:SourceEvent) RETURN s.key') == [['gdelt/123']]
    assert mirror.read('active_global', 'MATCH (f:Focus) RETURN f.id') == [['shared-focus']]


def test_high_degree_focus_is_hidden_immediately_with_bounded_edge_updates(live_graph):
    mirror=GraphMirror()
    mirror.query('active_global', 'CREATE (f:Focus {id:$key,archived:false}) '
        'WITH f UNWIND $ids AS id CREATE (t:AttentionTopic {key:id,projection_owner:$owner}) '
        'CREATE (t)-[:CANDIDATE_FOCUS {projection_owner:$owner,active:true,scope_version:1}]->(f)',
        key='high-degree', owner=OWNER, ids=['topic-'+str(i) for i in range(105)])
    invalidate_focus(mirror,'high-degree',2)
    assert mirror.read('active_global', 'MATCH (:AttentionTopic)-[r:CANDIDATE_FOCUS]->(:Focus) '
        'WHERE r.active=false RETURN count(r)') == [[100]]
    assert mirror.read('active_global', 'MATCH (:AttentionTopic)-[r:CANDIDATE_FOCUS]->(f:Focus) '
        'WHERE r.active=true AND f.attention_recall_visible=true '
        'AND r.scope_version=f.attention_scope_version AND coalesce(f.archived,false)=false RETURN count(r)') == [[0]]


def test_attention_queue_cursor_and_token_fencing(tmp_path, live_graph, monkeypatch):
    app = app_at(tmp_path); mirror = GraphMirror()
    seed(app, event_match=False)
    with Session(app.state.engine) as s:
        s.add(GraphPending(key='attention/topic-b', token='b'))
        s.commit()
        assert pick(s,include_attention=True,prefer_attention=True).key == 'attention/topic-a'
        assert pick(s,include_attention=True,prefer_attention=True,after_attention='attention/topic-a').key == 'attention/topic-b'
        assert pick(s,include_attention=True,prefer_attention=True,after_attention='attention/topic-b').key == 'attention/topic-a'
    original = mirror.query
    changed = []
    def requeue(graph, query, **params):
        result = original(graph, query, **params)
        if not changed and 'SET t.projection_owner=' in query:
            with Session(app.state.engine) as s:
                enqueue_graph(s,'attention/topic-a'); s.commit()
            changed.append(True)
        return result
    monkeypatch.setattr(mirror,'query',requeue)
    monkeypatch.setattr(graph_indexes,'POLL_INTERVAL',0)
    for _ in range(200):
        if mirror.step(app.state.engine): break
        time.sleep(.005)
    else: raise AssertionError('first projection never ran')
    assert changed
    with Session(app.state.engine) as s:
        pending = s.get(GraphPending,'attention/topic-a')
        assert pending.token != pending.written, 'old graph I/O cannot acknowledge the new token'
    settle(app,mirror,monkeypatch)
    assert mirror.metrics.snapshot()['totals']['stale'] >= 1


@pytest.mark.parametrize('with_watch',[True,False])
def test_attention_gets_a_turn_under_continuous_stale_event_work(tmp_path, live_graph, monkeypatch, with_watch):
    app=app_at(tmp_path); mirror=GraphMirror()
    seed(app,event_match=False)
    with Session(app.state.engine) as s:
        s.add(GraphPending(key='event/hot',token='one'))
        if with_watch:s.add(GraphPending(key='watch/absent',token='two'))
        s.commit()
    original=mirror.event
    def hot(data,key,places):
        original(data,key,places)
        with Session(app.state.engine) as s:
            enqueue_graph(s,'event/hot'); s.commit()
    monkeypatch.setattr(mirror,'event',hot)
    monkeypatch.setattr(graph_indexes,'POLL_INTERVAL',0)
    attempts=0
    for _ in range(200):
        if mirror.step(app.state.engine): attempts+=1
        with Session(app.state.engine) as s:
            row=s.get(GraphPending,'attention/topic-a')
            if row.token==row.written: break
        time.sleep(.005)
    else: raise AssertionError('attention was starved by a continually stale event')
    assert attempts <= 10
    with Session(app.state.engine) as s:
        row=s.get(GraphPending,'event/hot')
        assert row.token != row.written


def test_attention_projection_sql_and_cypher_inputs_are_bounded(tmp_path, live_graph, monkeypatch):
    app = app_at(tmp_path); mirror = GraphMirror()
    seed(app, count=ITEM_LIMIT+5)
    statements=[]; calls=[]
    def capture(_c,_cursor,sql,_args,context,_many):
        if str(context.execution_options.get('n2d_query_name','')).startswith('attention.graph.'):
            statements.append(sql)
    event.listen(app.state.engine,'before_cursor_execute',capture)
    original=mirror.query
    def query(graph,cypher,**params):
        calls.append((graph,cypher,params))
        return original(graph,cypher,**params)
    monkeypatch.setattr(mirror,'query',query)
    try:
        settle(app,mirror,monkeypatch)
    finally:
        event.remove(app.state.engine,'before_cursor_execute',capture)
    assert len(statements)==3 and all('LIMIT' in sql.upper() for sql in statements)
    attention_calls=[(g,q,p) for g,q,p in calls if 'AttentionTopic' in q or 'AttentionSourceItem' in q]
    assert all(g=='active_global' for g,_,_ in attention_calls)
    assert all(len(p.get('rows',[])) <= ITEM_LIMIT for _,_,p in attention_calls)
    assert all('原始标题' not in q and 'topic-a' not in q for _,q,_ in attention_calls)
