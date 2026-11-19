from sqlmodel import Session
import pytest

from news2douyin.desk.focus_models import FocusTrash
from news2douyin.desk.graph import GraphMirror
from news2douyin.desk.graph_explorer import explore, focuses
from news2douyin.desk.models import DeskMeta, EventPlace, GraphPending, RadarEntry, Watch, WatchEvent
from news2douyin.extraction.models import FocusPoint
from news2douyin.knowledge.models import GraphRelation, PointMaterial
from news2douyin.storage.db import create_schema, make_engine
from test_postgres_pipeline import pipeline_falkor  # existing optional FalkorDB fixture


@pytest.fixture
def store(tmp_path):
    engine = make_engine('sqlite:///' + str(tmp_path / 'explore.db'))
    create_schema(engine)
    with Session(engine) as session:
        session.add_all([
            Watch(key='focus1', name='A 新闻', kind='事件', scope='rail'),
            Watch(key='focus2', name='Z 新闻', kind='事件', scope='wind', enabled=False),
            Watch(key='hidden', name='0 Archived', kind='事件', scope='wind'),
            FocusTrash(watch='hidden', name='0 Archived', deleted_at='2026-11-11T01:00:00Z'),
            DeskMeta(key='graph_owner', value='test-owner'),
            RadarEntry(key='event1', title='Rail story', seen='2026-11-13T01:00:00Z',
                       priority=20, band='线索', reason='', evidence='', headings='', material='',
                       sources='[]', geography='{}'),
            RadarEntry(key='event2', title='Wind story', seen='2026-11-12T01:00:00Z',
                       priority=10, band='线索', reason='', evidence='', headings='', material='',
                       sources='[]', geography='{}'),
            WatchEvent(watch='focus1', event='event1', choice='include', reason='编辑采用'),
            WatchEvent(watch='focus1', event='event2', choice='exclude'),
            FocusPoint(key='point1', watch='focus1', parent='focus1', text='铁路问题', created='2026-11-12T01:00:00Z'),
            PointMaterial(key='point-event', point='point1', event='event1', reason='待核', generation='test'),
            GraphRelation(key='related-event', source='event1', target='event2', kind='related',
                          weight=0.625, evidence='[]', generation='test'),
            EventPlace(key='event-place', event='event1', role='action', iso='ke',
                       article='source1', revision=2, quote='Mombasa railway'),
        ])
        session.commit()
    yield engine
    engine.dispose()


def test_sql_graphs_use_current_membership_and_caps(store, monkeypatch):
    monkeypatch.delenv('NEWS2DOUYIN_FALKOR_URL', raising=False)
    with Session(store) as session:
        assert focuses(session) == [
            dict(key='focus1', name='A 新闻'),
            dict(key='focus2', name='Z 新闻'),
        ]
    mirror = GraphMirror()
    focus = explore(store, mirror, view='focus', focus='focus1')
    assert focus['state'] == 'degraded' and focus['reason'] == 'unconfigured'
    assert {row['kind'] for row in focus['nodes']} == {'Question', 'OutlinePoint', 'Event'}
    assert {row['kind'] for row in focus['edges']} == {'HAS_SUBQUESTION', 'HAS_MATERIAL', 'RELEVANT_MATERIAL'}
    assert 'Event:event2' not in {row['id'] for row in focus['nodes']}
    assert next(row['details']['reason'] for row in focus['edges'] if row['kind'] == 'HAS_MATERIAL') == '编辑采用'
    with pytest.raises(KeyError):
        explore(store, mirror, view='focus', focus='hidden')
    global_map = explore(store, mirror, limit=6)
    assert global_map['backend'] == 'sql'
    assert {row['kind'] for row in global_map['edges']} == {'ACTION_AT', 'RELATED_EVENT'}
    limited = explore(store, mirror, limit=1)
    assert len(limited['nodes']) == limited['node_limit'] == 1
    assert len(limited['edges']) == 0 and limited['truncated']


def test_falkor_live_relationships_both_views_and_owner(store, pipeline_falkor):
    mirror = GraphMirror()
    mirror.query('active_global', 'CREATE (:GraphMeta {id:"schema",owner:"test-owner"})')
    mirror.query('active_global', '''
        CREATE (e:Event {id:"event1",title:"Rail from FalkorDB",seen:"2026-11-13"}),
               (other:Event {id:"event2",title:"Wind"}),
               (a:Article {id:"source1",title:"Mombasa story",url:"https://example.org/rail"}),
               (p:Person:Entity {id:"person1",name:"Reporter"}),
               (f:Focus {id:"focus1",name:"Focus label",archived:false}),
               (old:Focus {id:"hidden",name:"Old",archived:false}),
               (g:SourceEvent {key:"gdelt/123",actor1_name:"A",event_code:"010",actor2_name:"B",provider:"gdelt"}),
               (d:GdeltDocument {key:"doc1",identifier:"https://example.org/gdelt"}),
               (x:AttentionTopic {key:"notice",title:"Platform headline",projection_owner:"attention/v1"}),
               (old_g:SourceEvent {key:"gdelt/456",evidence_available:false}),
               (old_x:AttentionTopic {key:"foreign",title:"Foreign topic"}),
               (e)-[:HAS_SOURCE]->(a),(a)-[:MENTIONS]->(p),(e)-[:RELATED_EVENT {weight:0.5}]->(other),
               (f)-[:TRACKS]->(e),(old)-[:TRACKS]->(e),(g)-[:MENTIONED_IN]->(d),
               (x)-[:CANDIDATE_MATCH {active:false}]->(g),(x)-[:CANDIDATE_FOCUS {active:true}]->(f),
               (x)-[:CANDIDATE_MATCH {active:true}]->(old_g)
    ''')
    mirror.query('focus_focus1', '''
        CREATE (q:Question {id:"focus1",text:"Focus root",archived:false}),
               (p:OutlinePoint {id:"point1",text:"Focus outline"}),
               (e:Event {id:"event1",title:"Focus material"}),
               (old:Event {id:"event2",title:"Excluded material"}),
               (a:Article {id:"source1",url:"https://example.org/source"}),
               (q)-[:HAS_SUBQUESTION]->(p),(q)-[:HAS_MATERIAL {reason:"evidence"}]->(e),
               (q)-[:HAS_MATERIAL]->(old),(e)-[:CITES]->(a),(p)-[:RELEVANT_MATERIAL]->(e)
    ''')

    real_read = mirror.read
    calls = []

    def trace(graph, query, **params):
        calls.append((graph, params['_query_timeout_ms']))
        return real_read(graph, query, **params)

    mirror.read = trace

    def deny_write(*args, **kwargs):
        raise AssertionError('graph explorer must not use a writing graph command')

    mirror.query = deny_write
    global_map = explore(store, mirror, limit=30)
    assert global_map['backend'] == 'falkordb', global_map
    assert {row['kind'] for row in global_map['nodes']} == {
        'Focus', 'Event', 'Article', 'Person', 'SourceEvent', 'GdeltDocument', 'AttentionTopic',
    }
    assert 'CANDIDATE_MATCH' not in {row['kind'] for row in global_map['edges']}
    assert {'CANDIDATE_FOCUS', 'MENTIONED_IN'} <= {row['kind'] for row in global_map['edges']}
    assert {'Focus:hidden', 'SourceEvent:gdelt/456', 'AttentionTopic:foreign'}.isdisjoint(
        row['id'] for row in global_map['nodes'])
    assert 'Reporter' in {row['title'] for row in global_map['nodes']}
    assert [(row['source'], row['target']) for row in global_map['edges'] if row['kind'] == 'TRACKS'] == [
        ('Focus:focus1', 'Event:event1')
    ]
    focus = explore(store, mirror, view='focus', focus='focus1')
    assert focus['backend'] == 'falkordb', focus
    assert {row['kind'] for row in focus['nodes']} == {'Question', 'OutlinePoint', 'Event', 'Article'}
    assert len([row for row in focus['edges'] if row['kind'] == 'HAS_MATERIAL']) == 1
    assert ('focus_focus1', 600) in calls
    assert {row['url'] for row in focus['nodes'] if row['kind'] == 'Article'} == {'https://example.org/source'}

    client, prefix = pipeline_falkor
    graph_key = prefix + '_focus_focus2'
    assert not client.exists(graph_key)
    empty_focus = explore(store, mirror, view='focus', focus='focus2')
    assert empty_focus['backend'] == 'sql' and empty_focus['focus']['key'] == 'focus2'
    assert not client.exists(graph_key)

    with Session(store) as session:
        session.add(GraphPending(key='watch/focus1', token='new', written='old'))
        session.commit()
    focus = explore(store, mirror, view='focus', focus='focus1')
    assert focus['backend'] == 'sql' and focus['reason'] == 'pending'

    with Session(store) as session:
        owner = session.get(DeskMeta, 'graph_owner')
        owner.value = 'another-owner'
        session.commit()
    global_map = explore(store, mirror)
    assert global_map['backend'] == 'sql' and global_map['reason'] == 'unavailable'
