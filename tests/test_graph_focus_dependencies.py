"""Topic graph reconciliation restores associations after endpoint changes.

SQL intentionally retains stale association rows in these checks: projection
must use the current topic membership, rather than rely on a particular UI
route having already cleaned those rows up.
"""
import json

import pytest
from sqlalchemy import delete
from sqlmodel import Session

from news2douyin.desk import service as desk
from news2douyin.desk.graph_focus_models import GraphFocusReceipt, GraphFocusState
from news2douyin.desk.models import WatchEvent, WatchReview
from news2douyin.extraction.models import FocusPoint, PointReview
from news2douyin.knowledge.models import PointMaterial
from news2douyin.research.models import ResearchClaim, ResearchQuestion
from news2douyin.storage.models import utc_now_iso
from test_content_foundation import live_graph
from test_graph_focus_incremental import _topic, _settle


def _enqueue(session, watch):
    desk.enqueue_graph(session, 'watch/'+watch)
    session.commit()


def _point(watch, key='dependency-point'):
    return FocusPoint(key=key, watch=watch, parent=watch, text='Background history?', created=utc_now_iso())


def test_exclude_reinclude_restores_unchanged_point_material(tmp_path, live_graph, monkeypatch):
    app, client, watch, keys = _topic(tmp_path, monkeypatch, count=1)
    mirror = app.state.desk.graph
    event = 'e_'+keys[0]
    with Session(app.state.engine) as session:
        session.add(_point(watch))
        session.add(PointMaterial(key='same-association', point='dependency-point', event=event,
                                  reason='Retained SQL association', generation='unchanged'))
        _enqueue(session, watch)
    _settle(app, watch)
    query = ('MATCH (:OutlinePoint {id:"dependency-point"})-[:RELEVANT_MATERIAL]->'
             '(e:Event) RETURN e.id')
    assert mirror.read('focus_'+watch, query) == [[event]]
    with Session(app.state.engine) as session:
        link = session.get(WatchEvent, (watch, event)); link.choice = 'exclude'; session.add(link)
        _enqueue(session, watch)
    _settle(app, watch)
    assert mirror.read('focus_'+watch, query) == []
    with Session(app.state.engine) as session:
        # The association's key/content/generation never changed.
        material = session.get(PointMaterial, 'same-association')
        assert material is not None and material.generation == 'unchanged'
        link = session.get(WatchEvent, (watch, event)); link.choice = 'include'; session.add(link)
        _enqueue(session, watch)
    _settle(app, watch)
    assert mirror.read('focus_'+watch, query) == [[event]]
    app.state.engine.dispose()


def test_removed_recreated_point_restores_unchanged_review_link(tmp_path, live_graph, monkeypatch):
    app, client, watch, keys = _topic(tmp_path, monkeypatch, count=1)
    mirror = app.state.desk.graph
    with Session(app.state.engine) as session:
        session.add(_point(watch))
        session.add(WatchReview(key='same-ticket', watch=watch, event='', question='History?',
                                case_key='dependency-case'))
        session.add(PointReview(ticket='same-ticket', point='dependency-point'))
        _enqueue(session, watch)
    _settle(app, watch)
    query = ('MATCH (:OutlinePoint {id:"dependency-point"})-[:CHECKED_BY]->'
             '(i:Investigation) RETURN i.id')
    assert mirror.read('focus_'+watch, query) == [['same-ticket']]
    with Session(app.state.engine) as session:
        session.delete(session.get(FocusPoint, 'dependency-point'))
        _enqueue(session, watch)
    _settle(app, watch)
    assert mirror.read('focus_'+watch, query) == []
    with Session(app.state.engine) as session:
        assert session.get(PointReview, 'same-ticket').point == 'dependency-point'
        session.add(_point(watch))
        _enqueue(session, watch)
    _settle(app, watch)
    assert mirror.read('focus_'+watch, query) == [['same-ticket']]
    app.state.engine.dispose()


def test_removed_recreated_claim_restores_unchanged_answer_link(tmp_path, live_graph, monkeypatch):
    app, client, watch, keys = _topic(tmp_path, monkeypatch, count=1)
    mirror = app.state.desk.graph
    with Session(app.state.engine) as session:
        session.add(WatchReview(key='answer-ticket', watch=watch, event='', question='What happened?',
                                case_key='answer-case'))
        session.add(ResearchQuestion(question_id='same-question', case_id='answer-case', key='q',
            text='What happened?', claim_ids_json=json.dumps(['same-claim'])))
        session.add(ResearchClaim(claim_id='same-claim', case_id='answer-case', run_id='test-run',
                                  text='An attributed statement.'))
        _enqueue(session, watch)
    _settle(app, watch)
    query = ('MATCH (:ResearchQuestion {id:"same-question"})-[:ANSWERED_BY]->'
             '(c:Claim) RETURN c.id')
    assert mirror.read('focus_'+watch, query) == [['same-claim']]
    with Session(app.state.engine) as session:
        session.delete(session.get(ResearchClaim, 'same-claim'))
        _enqueue(session, watch)
    _settle(app, watch)
    assert mirror.read('focus_'+watch, query) == []
    with Session(app.state.engine) as session:
        assert json.loads(session.get(ResearchQuestion, 'same-question').claim_ids_json) == ['same-claim']
        session.add(ResearchClaim(claim_id='same-claim', case_id='answer-case', run_id='test-run',
                                  text='An attributed statement.'))
        _enqueue(session, watch)
    _settle(app, watch)
    assert mirror.read('focus_'+watch, query) == [['same-claim']]
    app.state.engine.dispose()


def test_legacy_inventory_prunes_all_business_nodes_and_stale_point_edges(tmp_path, live_graph, monkeypatch):
    app, client, watch, keys = _topic(tmp_path, monkeypatch, count=1)
    mirror = app.state.desk.graph
    with Session(app.state.engine) as session:
        session.add(_point(watch))
        _enqueue(session, watch)
    _settle(app, watch)
    graph = 'focus_'+watch
    for label in ('Event', 'OutlinePoint', 'Investigation', 'ResearchQuestion', 'Claim', 'Report'):
        mirror.query(graph, 'CREATE (n:'+label+' {id:$id})', id='legacy-'+label)
    mirror.query(graph, 'MATCH (p:OutlinePoint {id:"dependency-point"}),(e:Event {id:$e}),(i:Investigation {id:"legacy-Investigation"}) '
                 'MERGE (p)-[:RELEVANT_MATERIAL]->(e) MERGE (p)-[:CHECKED_BY]->(i)', e='e_'+keys[0])
    mirror.query('active_global', 'MATCH (f:Focus {id:$watch}) MERGE (e:Event {id:"legacy-Event"}) '
                 'MERGE (f)-[:TRACKS]->(e)', watch=watch)
    with Session(app.state.engine) as session:
        # Simulate the v0.25.1 upgrade: a populated graph without receipts.
        session.exec(delete(GraphFocusReceipt))
        session.exec(delete(GraphFocusState))
        _enqueue(session, watch)
    _settle(app, watch)
    assert mirror.read(graph, 'MATCH (n) WHERE n.id STARTS WITH "legacy-" RETURN n.id') == []
    assert mirror.read(graph, 'MATCH (:OutlinePoint {id:"dependency-point"})-[r:RELEVANT_MATERIAL|CHECKED_BY]->() RETURN type(r)') == []
    assert mirror.read(graph, 'MATCH (:Question {id:$w})-[:HAS_MATERIAL]->(e:Event) RETURN e.id', w=watch) == [['e_'+keys[0]]]
    assert mirror.read('active_global', 'MATCH (:Focus {id:$w})-[:TRACKS]->(e:Event {id:"legacy-Event"}) RETURN e.id', w=watch) == []
    app.state.engine.dispose()


@pytest.mark.parametrize('endpoint', ['/api/knowledge/rebuild', '/api/desk/rebuild-graphs'])
def test_public_rebuild_repairs_corrupt_topic_with_unchanged_sql(tmp_path, live_graph, monkeypatch, endpoint):
    app, client, watch, keys = _topic(tmp_path, monkeypatch, count=1)
    mirror = app.state.desk.graph
    _settle(app, watch)
    query = ('MATCH (:Event {id:$event})-[:CITES]->(a:Article) RETURN a.id')
    event = 'e_'+keys[0]
    assert mirror.read('focus_'+watch, query, event=event) == [[keys[0]]]
    # Damage only the rebuildable mirror. Source SQL and receipt fingerprints
    # still describe exactly the same article/event content as before.
    mirror.query('focus_'+watch, 'MATCH (:Event {id:$event})-[r:CITES]->() DELETE r', event=event)
    assert mirror.read('focus_'+watch, query, event=event) == []
    response = client.post(endpoint)
    assert response.status_code == 200, response.text
    _settle(app, watch)
    assert mirror.read('focus_'+watch, query, event=event) == [[keys[0]]]
    app.state.engine.dispose()
