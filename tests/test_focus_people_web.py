"""The extracted activity timeline remains available from the focus tab."""
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from news2douyin.desk import service as desk
from news2douyin.desk.models import Watch, WatchEvent
from news2douyin.extraction.models import TextMention
from news2douyin.extraction.schema import Candidate, grounded, passages
from news2douyin.server.app import create_app
from news2douyin.storage.articles import latest_version, record_version
from news2douyin.storage.models import Article, ArticleEventLink, Event
from news2douyin.storage.utils import dumps
from test_postgres_integration import postgres_url


@pytest.mark.parametrize('database', ('sqlite', 'postgres'))
def test_focus_keeps_grounded_people_timeline_separate_from_official_graph(tmp_path, monkeypatch, database, request):
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', '')
    monkeypatch.setenv('NEWS2DOUYIN_TIMEZONE', 'Asia/Shanghai')
    database_url = (request.getfixturevalue('postgres_url') if database == 'postgres'
                    else f'sqlite:///{tmp_path / "people.db"}')
    app = create_app(db_url=database_url, storage_root=str(tmp_path))
    client = TestClient(app)
    try:
        focus_a, focus_b, excluded_focus = ('w_' + char * 32 for char in 'abc')
        quote = '2024年6月10日，陈澄代表海港研究所出席会议。'
        with Session(app.state.engine) as session:
            for key, name in zip((focus_a, focus_b, excluded_focus), ('机构跟踪', '人物跟踪', '排除材料')):
                session.add(Watch(key=key, name=name, kind='长期事件', scope='港口'))
            article = Article(article_key='article_people', title='港口会议', content=quote,
                              url='https://example.org/port', published_at='2024-06-11T00:00:00Z')
            event = Event(event_key='event_people', event_title='港口会议')
            session.add(article)
            session.add(event)
            session.flush()
            record_version(session, article)
            session.flush()
            version = latest_version(session, article.article_key)
            blocks = passages({'title': article.title, 'content': quote})
            mention = grounded(Candidate(block='P2', quote=quote, trigger='出席', kind='meeting',
                status='reported', time='2024年6月10日', arguments=[
                    dict(role='actor', text='陈澄', entity_type='person'),
                    dict(role='participant', text='海港研究所', entity_type='organization'),
                ]), blocks, article.article_key, version.revision, article.published_at)
            session.add(TextMention(id=mention['id'], version_id=version.id, article=article.article_key,
                                    revision=version.revision, action_date=mention['date'], data=dumps(mention)))
            session.add(ArticleEventLink(article_key=article.article_key, event_key=event.event_key))
            for key, choice in ((focus_a, 'include'), (focus_b, 'include'), (excluded_focus, 'exclude')):
                session.add(WatchEvent(watch=key, event=event.event_key, choice=choice))
            session.flush()
            session.add(desk.snapshot(session, event))
            session.commit()

        interval = {'days': 30, 'until': '2024-06-12'}
        first = client.get(f'/api/focus/{focus_a}/people', params=interval)
        assert first.status_code == 200, first.text
        data = first.json()
        identity = data['items'][0]
        assert identity['name'] == '陈澄'
        timeline = data['selected']['activities'][0]
        assert timeline['mention_id'] == mention['id']
        assert timeline['time'] == '2024-06-10' and timeline['time_kind'] == 'reported_occurrence'
        edge = data['selected']['organizations'][0]
        assert edge['relation'] == 'represents' and edge['relation_basis'] == 'literal_statement'
        assert edge['evidence_key'] == timeline['evidence_key']
        second = client.get(f'/api/focus/{focus_b}/people', params=interval).json()
        assert second['items'][0]['key'] == identity['key']
        assert second['selected']['activities'][0]['evidence_key'] == timeline['evidence_key']
        excluded = client.get(f'/api/focus/{excluded_focus}/people', params=interval).json()
        assert excluded['items'] == []
        graph = client.get('/graphs', params={'view': 'focus', 'focus': focus_a})
        assert f'/graphs?view=focus&amp;focus={focus_a}&amp;lens=people' in graph.text
        lens_params = {'view': 'focus', 'focus': focus_a, 'lens': 'people', **interval}
        canvas = client.get('/graphs', params=lens_params)
        assert canvas.status_code == 200
        assert 'data-lens="people"' in canvas.text and 'id="graph-canvas"' in canvas.text
        snapshot = client.get('/api/graphs', params=lens_params)
        assert snapshot.status_code == 200, snapshot.text
        snapshot = snapshot.json()
        assert snapshot['backend'] == 'official_focus'
        assert snapshot['nodes'] == [] and snapshot['state'] == 'unbuilt'
        assert '重建焦点图' in canvas.text
        page = client.get(f'/focus/{focus_a}', params={
            'view': 'people', 'person': identity['key'], 'people_days': 30, 'people_until': '2024-06-12',
        })
        assert page.status_code == 200, page.text
        for text in ('人物与组织', '陈澄', '海港研究所', '2024-06-10',
                     'article_people?revision=1#m-' + mention['id']):
            assert text in page.text
        source = client.get(timeline['source_path'].split('#', 1)[0])
        assert source.status_code == 200 and f'id="m-{mention["id"]}"' in source.text
        assert client.get('/api/focus/w_missing/people').status_code == 404
        assert client.get(f'/api/focus/{focus_a}/people', params={'until': '2024-02-30'}).status_code == 422
    finally:
        client.close()
        app.state.engine.dispose()
