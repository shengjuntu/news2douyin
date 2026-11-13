"""Real FalkorDB checks for asynchronous readiness and independent maintenance."""
import json
import time

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.desk import graph_gc, graph_indexes, service as desk
from news2douyin.desk.graph_maintenance_models import GraphGcState
from news2douyin.desk.models import GraphPending
from news2douyin.storage.articles import record_version
from news2douyin.storage.models import Article, ArticleEventLink, Event
from test_content_foundation import app_at, article, process, drain_graph, live_graph


def gc_cycle(app):
    graph=app.state.desk.graph
    previous={}
    with Session(app.state.engine) as s:
        for row in s.exec(select(GraphGcState)):
            previous[row.label]=row.cycle
            row.next_due=0;s.add(row)
        s.commit()
    for _ in range(100):
        graph_gc.step(app.state.engine,graph)
        status=graph_gc.summary(app.state.engine,graph)
        assert not status.get('error'),status
        if status.get('complete_labels')==status.get('labels'):
            with Session(app.state.engine) as s:
                rows=s.exec(select(GraphGcState)).all()
                if rows and all(row.cycle>previous.get(row.label,0) for row in rows):
                    return status
    raise AssertionError('The tiny synthetic graph did not complete a GC cycle')


def test_missing_startup_indexes_are_added_without_rebuilding_graph(tmp_path,live_graph):
    app=app_at(tmp_path)
    article(app,'indexed','Alice visited London yesterday.');process(app);drain_graph(app)
    graph=app.state.desk.graph
    assert graph_indexes.ready_for(graph,'active_global','Event')
    query='MATCH (e:Event {id:"e_indexed"}) RETURN e.id'
    plan=graph.client.execute_command('GRAPH.EXPLAIN',graph.prefix+'_active_global',query)
    assert 'Index Scan' in str(plan),plan
    before=graph.read('active_global','MATCH (e:Event)-[:HAS_SOURCE]->(a:Article) RETURN e.id,a.id')
    graph.query('active_global','DROP INDEX ON :Event(id)')
    graph_indexes.reset(graph)
    seen=[];original=graph.query
    def record(name,cypher,**params):
        seen.append(cypher)
        return original(name,cypher,**params)
    graph.query=record
    try:
        for _ in range(1000):
            with graph.exclusive(app.state.engine) as acquired:
                assert acquired
                ready=graph_indexes.ensure_step(graph,app.state.engine)
            if ready:break
            time.sleep(.01)
        else:raise AssertionError(graph_indexes.status_summary(graph))
    finally:graph.query=original
    assert sum(cypher.startswith('CREATE INDEX') for cypher in seen)==1,seen
    assert graph.read('active_global','MATCH (e:Event)-[:HAS_SOURCE]->(a:Article) RETURN e.id,a.id')==before
    assert not any('DELETE' in cypher for cypher in seen)
    app.state.engine.dispose()


def test_shared_source_is_preserved_until_final_parent_is_removed(tmp_path,live_graph):
    app=app_at(tmp_path)
    with Session(app.state.engine) as s:
        item=Article(article_key='shared',title='Shared source',content='Alice visited London yesterday.')
        s.add(item);record_version(s,item)
        for key in ('e_first','e_second'):
            s.add(Event(event_key=key,event_title=key))
            s.add(ArticleEventLink(event_key=key,article_key=item.article_key))
        s.commit()
    process(app);drain_graph(app)
    with Session(app.state.engine) as s:
        link=s.exec(select(ArticleEventLink).where(ArticleEventLink.event_key=='e_first')).one()
        s.delete(link);desk.enqueue_dirty(s,'e_first');s.commit()
    process(app);drain_graph(app);gc_cycle(app)
    graph=app.state.desk.graph
    assert graph.read('active_global','MATCH (:Event {id:"e_second"})-[:HAS_SOURCE]->(a:Article {id:"shared"}) RETURN a.id')==[['shared']]
    with Session(app.state.engine) as s:
        link=s.exec(select(ArticleEventLink).where(ArticleEventLink.event_key=='e_second')).one()
        s.delete(link);desk.enqueue_dirty(s,'e_second');s.commit()
    process(app);drain_graph(app)
    # Detaching a source does not launch whole-graph GC in event(). The
    # independent sweep subsequently collects it and its detached children.
    assert graph.read('active_global','MATCH (a:Article {id:"shared"}) RETURN a.id')==[['shared']]
    status=gc_cycle(app)
    assert status['deleted']>=1
    assert graph.read('active_global','MATCH (a:Article {id:"shared"}) RETURN a.id')==[]
    assert graph.read('active_global','MATCH (m:EventMention) RETURN count(m)')==[[0]]
    app.state.engine.dispose()


def test_gc_keeps_interest_references_and_failures_do_not_change_event_ack(tmp_path,live_graph,monkeypatch):
    app=app_at(tmp_path)
    article(app,'base','Alice visited London yesterday.');process(app);drain_graph(app)
    graph=app.state.desk.graph
    graph.query('active_global','CREATE (i:InterestEntity {id:"interest-only"}) '
        'CREATE (r:Referent {id:"mapped-reference"}) CREATE (i)-[:MAPS_TO]->(r) '
        'CREATE (:Referent {id:"unreferenced"})')
    gc_cycle(app)
    assert graph.read('active_global','MATCH (:InterestEntity)-[:MAPS_TO]->(r:Referent) RETURN r.id')==[['mapped-reference']]
    assert graph.read('active_global','MATCH (r:Referent {id:"unreferenced"}) RETURN r.id')==[]
    with Session(app.state.engine) as s:
        before={row.key:row.written for row in s.exec(select(GraphPending))}
        for row in s.exec(select(GraphGcState)):
            row.next_due=0;s.add(row)
        s.commit()
    original=graph.read
    def failed(name,query,**params):
        if 'max(id(n))' in query:raise TimeoutError('private endpoint must not escape')
        return original(name,query,**params)
    monkeypatch.setattr(graph,'read',failed)
    assert graph_gc.step(app.state.engine,graph) is False
    assert 'private' not in graph_gc.summary(app.state.engine,graph)['error']
    with Session(app.state.engine) as s:
        assert {row.key:row.written for row in s.exec(select(GraphPending))}==before
    monkeypatch.setattr(graph,'read',original)
    article(app,'after','Bob visited Paris yesterday.');process(app);drain_graph(app)
    with Session(app.state.engine) as s:
        row=s.get(GraphPending,'event/e_after');assert row.token==row.written
    app.state.engine.dispose()


def test_web_get_does_not_build_indexes_and_stays_usable_while_checking(tmp_path,monkeypatch):
    app=app_at(tmp_path);client=TestClient(app)
    mirror=app.state.desk.graph
    mirror.url='redis://test.invalid'
    def forbidden(*args,**kwargs):
        pytest.fail('A read request tried to build a graph index')
    monkeypatch.setattr(graph_indexes,'ensure_step',forbidden)
    for path in ('/radar','/focus','/knowledge','/api/knowledge/status'):
        started=time.perf_counter();response=client.get(path)
        assert response.status_code==200,(path,response.text)
        assert time.perf_counter()-started<2
    app.state.engine.dispose()


def test_a_topic_index_build_cannot_pin_the_global_event_queue(tmp_path,live_graph,monkeypatch):
    app=app_at(tmp_path);client=TestClient(app)
    article(app,'base','Alice visited London yesterday.');process(app);drain_graph(app)
    watch=client.post('/api/focus',json={'name':'Pending topic indexes','scope':'Alice'}).json()['key']
    article(app,'later','Bob visited Paris yesterday.');process(app)
    graph=app.state.desk.graph;graph._event_streak=8
    original=graph.prepare_indexes
    def building(engine,name='active_global'):
        if name=='focus_'+watch:
            graph.work_state='indexes'
            return False
        return original(engine,name)
    monkeypatch.setattr(graph,'prepare_indexes',building)
    from sqlalchemy import event as sql_event
    statements=[]
    def record(_connection,_cursor,statement,*args):statements.append(statement.lower())
    sql_event.listen(app.state.engine,'before_cursor_execute',record)
    try:
        assert graph.step(app.state.engine) is False
    finally:
        sql_event.remove(app.state.engine,'before_cursor_execute',record)
    assert not any('radarentry.material' in statement for statement in statements)
    assert graph.step(app.state.engine) is True
    with Session(app.state.engine) as s:
        assert s.get(GraphPending,'event/e_later').written==s.get(GraphPending,'event/e_later').token
        assert s.get(GraphPending,'watch/'+watch).written!=s.get(GraphPending,'watch/'+watch).token
    app.state.engine.dispose()
