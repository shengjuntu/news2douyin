"""Interest patterns remain non-factual and graph paths only nominate material."""
import json

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.desk import focus_jobs, intent
from news2douyin.desk.focus_models import FocusCandidate
from news2douyin.desk.models import Watch, WatchEvent
from news2douyin.knowledge import service as knowledge
from test_content_foundation import app_at, article, process, drain_graph, live_graph


def create_topic(app, client, monkeypatch):
    spec = intent.validate_spec(dict(name='特朗普外交', entities=[dict(name='特朗普', type='person', core=True)],
        relations=[dict(subject='特朗普', relation='会谈', object='', keywords=['met', 'meeting', '会谈'])],
        questions=['会谈对象与后续行动有什么变化？']))
    monkeypatch.setattr(intent, 'interpret', lambda text: dict(spec=spec.model_dump(), method='model', message='测试材料'))
    draft = client.post('/api/focus/interpret', json={'description':'持续关注特朗普的会谈对象与后续行动'}).json()['key']
    assert focus_jobs.build_step(app.state.engine)
    result = client.post('/api/focus/from-description', json={'draft':draft})
    assert result.status_code == 201
    return result.json()['key']


def test_real_interest_graph_does_not_invent_events(tmp_path, live_graph, monkeypatch):
    app = app_at(tmp_path); client = TestClient(app)
    watch = create_topic(app, client, monkeypatch)
    drain_graph(app)
    graph = app.state.desk.graph
    assert graph.read('focus_'+watch, 'MATCH (s:InterestScope)-[:HAS_PATTERN]->(p:InterestPattern) RETURN p.relation') == [['会谈']]
    assert graph.read('focus_'+watch, 'MATCH (p:InterestPattern)-[:PATTERN_SUBJECT]->(e:InterestEntity) RETURN e.name') == [['特朗普']]
    assert graph.read('focus_'+watch, 'MATCH (m:EventMention) RETURN count(m)') == [[0]]
    assert graph.read('active_global', 'MATCH (e:Event) RETURN count(e)') == [[0]]
    app.state.engine.dispose()


def test_real_graph_neighbor_waits_for_topic_judgement(tmp_path, live_graph, monkeypatch):
    app = app_at(tmp_path); client = TestClient(app)
    article(app, 'anchor', 'Donald Trump met Alice in Paris today.', '外交会谈')
    article(app, 'neighbor', 'Alice visited London today.', '另一次访问')
    process(app)
    watch = create_topic(app, client, monkeypatch)

    def judge(spec, card, route, **kwargs):
        source = card.sources[0]
        return dict(decision='core' if card.key == 'e_anchor' else 'background', method='model',
            reason='受控测试中的相关材料', evidence=[dict(article=source['key'], revision=source['revision'], quote=source['content'])],
            questions=spec.questions[:1])
    monkeypatch.setattr(intent, 'judge', judge)
    while focus_jobs.scan_step(app.state.engine):
        pass
    assert focus_jobs.judge_step(app.state.engine)
    with Session(app.state.engine) as s:
        assert s.get(WatchEvent, (watch, 'e_anchor')) is not None
        assert s.get(WatchEvent, (watch, 'e_neighbor')) is None
    drain_graph(app)
    with Session(app.state.engine) as s:
        candidate = s.exec(select(FocusCandidate).where(FocusCandidate.watch == watch,
            FocusCandidate.event == 'e_neighbor', FocusCandidate.status != 'superseded')).one()
        assert 'graph' in json.loads(candidate.recall_json)['channels']
        assert s.get(WatchEvent, (watch, 'e_neighbor')) is None
    assert focus_jobs.judge_step(app.state.engine)
    assert client.post('/api/focus/'+watch+'/links',json={'event':'e_neighbor','action':'exclude'}).status_code==200
    with Session(app.state.engine) as s:
        from news2douyin.knowledge.models import PointMaterial
        assert not s.exec(select(PointMaterial).where(PointMaterial.event=='e_neighbor')).all()
    assert client.post('/api/focus/'+watch+'/links',json={'event':'e_neighbor','action':'auto'}).status_code==200
    assert focus_jobs.judge_step(app.state.engine)
    with Session(app.state.engine) as s:
        assert s.get(WatchEvent, (watch, 'e_neighbor')).relevance == 1
        w = s.get(Watch, watch); w.version += 1; s.add(w); s.commit()
        # Old accepted candidates cannot masquerade as the new scope.
        assert knowledge.graph_view(s, watch=watch)['nodes'] == []
    app.state.engine.dispose()
