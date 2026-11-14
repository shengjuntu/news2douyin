"""Real-Falkor regression checks for durable, bounded topic projection.

These exercise the public graph worker rather than mirroring its checkpoint
implementation. The graph contents, queue receipts and emitted mutations are
the externally observable contract.
"""
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.desk import graph_indexes
from news2douyin.desk import service as desk
from news2douyin.desk.graph import GraphMirror
from news2douyin.desk.models import GraphPending, Watch, WatchEvent
from news2douyin.storage.models import ArticleEventLink, Event
from test_content_foundation import app_at, article, process, drain_graph, live_graph


def _pending(app, watch):
    with Session(app.state.engine) as session:
        row = session.get(GraphPending, 'watch/'+watch)
        return row.token != row.written


def _members(mirror, watch):
    return {row[0] for row in mirror.read('focus_'+watch,
        'MATCH (e:Event) RETURN e.id')}


def _advance(app, *, until=None, steps=500):
    mirror = app.state.desk.graph
    for _ in range(steps):
        changed = mirror.step(app.state.engine)
        if until is not None and until():
            return
        if not changed and mirror.work_state == 'indexes':
            time.sleep(.005)
    raise AssertionError('Topic projection did not reach its observable checkpoint')


def _settle(app, watch):
    _advance(app, until=lambda: not _pending(app, watch))


def _topic(tmp_path, monkeypatch, count=5):
    monkeypatch.setenv('NEWS2DOUYIN_GRAPH_FOCUS_BATCH_SIZE', '2')
    monkeypatch.setattr(graph_indexes, 'POLL_INTERVAL', 0)
    app = app_at(tmp_path)
    client = TestClient(app)
    keys = [f'{number:03}' for number in range(count)]
    for key in keys:
        article(app, key, f'Alice visited London yesterday. Record {key}.')
    process(app)
    # Finish the global event queue before isolating a topic's work.
    drain_graph(app)
    response = client.post('/api/focus', json=dict(name='Durable topic', scope='Alice'))
    assert response.status_code == 201, response.text
    watch = response.json()['key']
    for key in keys:
        linked = client.post('/api/focus/'+watch+'/links',
            json=dict(event='e_'+key, action='include'))
        assert linked.status_code == 200, linked.text
    return app, client, watch, keys


def _capture_focus_mutations(monkeypatch, mirror, watch):
    calls = []
    original = mirror.query
    def capture(graph_name, query, **params):
        if graph_name == 'focus_'+watch:
            calls.append((query, params))
        return original(graph_name, query, **params)
    monkeypatch.setattr(mirror, 'query', capture)
    return calls


def _mutated_event_ids(calls, keys):
    encoded = json.dumps(calls, ensure_ascii=False)
    return {'e_'+key for key in keys if '"e_'+key+'"' in encoded}


def test_topic_material_is_bounded_and_an_event_can_run_between_batches(tmp_path, live_graph, monkeypatch):
    app, client, watch, keys = _topic(tmp_path, monkeypatch)
    mirror = app.state.desk.graph
    counts = []
    _advance(app, until=lambda: bool(_members(mirror, watch)))
    first = _members(mirror, watch)
    assert 0 < len(first) <= 2
    assert _pending(app, watch)

    # New global work is admitted after this small topic batch, without
    # waiting for all of the topic's historical materials to be copied.
    article(app, 'late', 'Bob visited Paris yesterday.')
    process(app)
    with Session(app.state.engine) as session:
        row = session.get(GraphPending, 'event/e_late')
        assert row.token != row.written
    _advance(app, until=lambda: bool(mirror.read('active_global',
        'MATCH (e:Event {id:"e_late"}) RETURN e.id')))
    assert len(_members(mirror, watch)) < len(keys)

    previous = len(_members(mirror, watch))
    for _ in range(500):
        if not _pending(app, watch):
            break
        mirror.step(app.state.engine)
        count = len(_members(mirror, watch))
        counts.append(count)
        assert 0 <= count-previous <= 2
        previous = count
    else:
        pytest.fail('Batched topic did not acknowledge its final pass')
    assert _members(mirror, watch) == {'e_'+key for key in keys}
    app.state.engine.dispose()


def test_one_updated_material_does_not_rewrite_unchanged_topic_events(tmp_path, live_graph, monkeypatch):
    app, client, watch, keys = _topic(tmp_path, monkeypatch, count=3)
    _settle(app, watch)
    mirror = app.state.desk.graph
    calls = _capture_focus_mutations(monkeypatch, mirror, watch)
    article(app, keys[1], 'Alice visited Paris yesterday. This is a corrected report.')
    process(app)
    _settle(app, watch)
    assert _mutated_event_ids(calls, keys) == {'e_'+keys[1]}, calls
    assert _members(mirror, watch) == {'e_'+key for key in keys}
    app.state.engine.dispose()


def test_retokenizing_a_topic_each_tick_does_not_reset_its_material_cursor(tmp_path, live_graph, monkeypatch):
    app, client, watch, keys = _topic(tmp_path, monkeypatch)
    mirror = app.state.desk.graph
    counts = []
    for _ in range(150):
        with Session(app.state.engine) as session:
            desk.enqueue_graph(session, 'watch/'+watch)
            session.commit()
        mirror.step(app.state.engine)
        count = len(_members(mirror, watch))
        counts.append(count)
        if count == len(keys):
            break
        if mirror.work_state == 'indexes':
            time.sleep(.005)
    assert counts[-1] == len(keys), counts
    assert any(0 < count < len(keys) for count in counts), counts
    # Once changes stop arriving, the newer token can also be confirmed.
    _settle(app, watch)
    assert not _pending(app, watch)
    app.state.engine.dispose()


def test_topic_restart_resumes_committed_material_without_rewriting_it(tmp_path, live_graph, monkeypatch):
    app, client, watch, keys = _topic(tmp_path, monkeypatch)
    old = app.state.desk.graph
    _advance(app, until=lambda: bool(_members(old, watch)))
    committed = _members(old, watch)
    assert 0 < len(committed) < len(keys)
    restarted = GraphMirror()
    app.state.desk.graph = restarted
    calls = _capture_focus_mutations(monkeypatch, restarted, watch)
    _settle(app, watch)
    assert _members(restarted, watch) == {'e_'+key for key in keys}
    assert not (_mutated_event_ids(calls, keys) & committed), calls
    app.state.engine.dispose()


def test_excluding_already_projected_member_during_a_pass_converges(tmp_path, live_graph, monkeypatch):
    app, client, watch, keys = _topic(tmp_path, monkeypatch)
    mirror = app.state.desk.graph
    _advance(app, until=lambda: bool(_members(mirror, watch)))
    removed = min(_members(mirror, watch))
    assert client.post('/api/focus/'+watch+'/links',
        json=dict(event=removed, action='exclude')).status_code == 200
    _settle(app, watch)
    assert _members(mirror, watch) == {'e_'+key for key in keys}-{removed}
    assert mirror.read('active_global',
        'MATCH (:Focus {id:$w})-[:TRACKS]->(e:Event {id:$e}) RETURN e.id', w=watch, e=removed) == []
    app.state.engine.dispose()


def test_removing_one_member_preserves_a_shared_article_and_its_actions(tmp_path, live_graph, monkeypatch):
    app, client, watch, keys = _topic(tmp_path, monkeypatch, count=1)
    with Session(app.state.engine) as session:
        session.add(Event(event_key='e_shared', event_title='Same report in another event'))
        session.add(ArticleEventLink(event_key='e_shared', article_key=keys[0]))
        session.commit()
    process(app)
    assert client.post('/api/focus/'+watch+'/links',
        json=dict(event='e_shared', action='include')).status_code == 200
    _settle(app, watch)
    mirror = app.state.desk.graph
    assert mirror.read('focus_'+watch,
        'MATCH (:Article {id:$a})-[:DESCRIBES]->(m:EventMention) RETURN m.id', a=keys[0])
    assert client.post('/api/focus/'+watch+'/links',
        json=dict(event='e_'+keys[0], action='exclude')).status_code == 200
    _settle(app, watch)
    assert _members(mirror, watch) == {'e_shared'}
    assert mirror.read('focus_'+watch,
        'MATCH (:Event {id:"e_shared"})-[:CITES]->(a:Article)-[:DESCRIBES]->(m:EventMention) RETURN a.id,m.id')
    app.state.engine.dispose()


def test_graph_write_before_checkpoint_failure_is_replayed_safely(tmp_path, live_graph, monkeypatch):
    app, client, watch, keys = _topic(tmp_path, monkeypatch, count=3)
    _settle(app, watch)
    mirror = app.state.desk.graph
    article(app, keys[1], 'Alice visited Paris yesterday. Corrected destination.')
    process(app)
    original = mirror.query
    failed = []
    def fail_after_write(graph_name, query, **params):
        result = original(graph_name, query, **params)
        if graph_name == 'focus_'+watch and 'CITES' in query and 'MERGE' in query and not failed:
            failed.append(True)
            raise RuntimeError('synthetic interruption after an external graph write')
        return result
    monkeypatch.setattr(mirror, 'query', fail_after_write)
    with pytest.raises(RuntimeError, match='synthetic interruption'):
        _advance(app, until=lambda: not _pending(app, watch))
    assert failed and _pending(app, watch)
    restarted = GraphMirror()
    app.state.desk.graph = restarted
    _settle(app, watch)
    assert _members(restarted, watch) == {'e_'+key for key in keys}
    revisions = restarted.read('focus_'+watch,
        'MATCH (:Article {id:$a})-[:DESCRIBES]->(m:EventMention) RETURN DISTINCT m.revision', a=keys[1])
    assert revisions == [[2]], revisions
    app.state.engine.dispose()


def test_lost_focus_graph_invalidates_old_material_receipts(tmp_path, live_graph, monkeypatch):
    app, client, watch, keys = _topic(tmp_path, monkeypatch, count=3)
    _settle(app, watch)
    mirror = app.state.desk.graph
    assert _members(mirror, watch) == {'e_'+key for key in keys}
    live_graph.execute_command('GRAPH.DELETE', mirror.prefix+'_focus_'+watch)
    mirror.bootstrap(app.state.engine)
    assert _pending(app, watch)
    _settle(app, watch)
    assert _members(mirror, watch) == {'e_'+key for key in keys}
    assert mirror.read('focus_'+watch,
        'MATCH (e:Event)-[:CITES]->(:Article)-[:DESCRIBES]->(m:EventMention) RETURN count(DISTINCT e)') == [[len(keys)]]
    app.state.engine.dispose()


def test_scope_change_discards_obsolete_automatic_members_mid_pass(tmp_path, live_graph, monkeypatch):
    from news2douyin.desk.focus_models import FocusCandidate, FocusDefinition
    app, client, watch, keys = _topic(tmp_path, monkeypatch)
    mirror = app.state.desk.graph
    _advance(app, until=lambda: bool(_members(mirror, watch)))
    assert 0 < len(_members(mirror, watch)) < len(keys)
    with Session(app.state.engine) as session:
        row = session.get(Watch, watch)
        original_version = row.version
        session.add(FocusDefinition(watch=watch, description='Changed topic scope', spec_json='{}',
            scope_version=original_version+1))
        for key in keys:
            link = session.get(WatchEvent, (watch, 'e_'+key))
            link.choice = 'auto'
            session.add(link)
            session.add(FocusCandidate(key='old-'+key, watch=watch, event='e_'+key,
                scope_version=original_version, event_fingerprint='old', status='done', decision='core'))
        row.version += 1
        row.scope = 'Bob'
        session.add(row)
        desk.enqueue_graph(session, 'watch/'+watch)
        session.commit()
    _settle(app, watch)
    assert _members(mirror, watch) == set()
    with Session(app.state.engine) as session:
        row = session.get(Watch, watch)
        session.add(FocusCandidate(key='new-accepted', watch=watch, event='e_'+keys[0],
            scope_version=row.version, event_fingerprint='new', status='done', decision='core'))
        desk.enqueue_graph(session, 'watch/'+watch)
        session.commit()
    _settle(app, watch)
    assert _members(mirror, watch) == {'e_'+keys[0]}
    app.state.engine.dispose()


def test_first_inventory_prunes_obsolete_legacy_business_nodes_in_bounded_windows(tmp_path, live_graph, monkeypatch):
    app, client, watch, keys = _topic(tmp_path, monkeypatch, count=3)
    mirror = app.state.desk.graph
    # A previous release left business nodes but has no SQL projection
    # receipts. Adoption must discover them without treating them as current.
    mirror.query('focus_'+watch,
        'CREATE (e:Event {id:"legacy-event"}), (p:OutlinePoint {id:"legacy-point"}), '
        '(q:ResearchQuestion {id:"legacy-question"}), (c:Claim {id:"legacy-claim"}), '
        '(r:Report {id:"legacy-report"}), (i:Investigation {id:"legacy-review"})')
    mirror.query('active_global',
        'MERGE (f:Focus {id:$w}) MERGE (e:Event {id:"legacy-event"}) MERGE (f)-[:TRACKS]->(e)', w=watch)
    mirror.query('focus_'+watch,
        'UNWIND range(0,259) AS i CREATE (:Noise {id:"noise-"+toString(i)})')
    inventory = []
    original_read = mirror.read
    def capture_read(graph_name, query, **params):
        result = original_read(graph_name, query, **params)
        if graph_name == 'focus_'+watch and 'id(n)>=$lo' in query:
            inventory.append((params, len(result)))
        return result
    monkeypatch.setattr(mirror, 'read', capture_read)
    _settle(app, watch)
    assert _members(mirror, watch) == {'e_'+key for key in keys}
    assert mirror.read('focus_'+watch,
        'MATCH (n) WHERE n.id STARTS WITH "legacy-" RETURN n.id') == []
    assert mirror.read('active_global',
        'MATCH (:Focus {id:$w})-[:TRACKS]->(:Event {id:"legacy-event"}) RETURN 1', w=watch) == []
    assert len(inventory) >= 3, inventory
    assert all(params['hi']-params['lo'] <= 1024 and count <= 128 for params, count in inventory)
    app.state.engine.dispose()
