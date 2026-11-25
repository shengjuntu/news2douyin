import re

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.candidates.service import record_reports
from news2douyin.research.models import ResearchCase, ResearchRun
from news2douyin.server import interop
from news2douyin.server.app import create_app
from news2douyin.storage.articles import record_version
from news2douyin.storage.models import Article, ArticleEventLink, Event, EventMoment
from news2douyin.storage.utils import dumps
from test_research import FakeRunDesk


def receipt(response):
    assert response.status_code == 200, response.text
    return re.search(r'name="receipt" value="([0-9a-f]+)"', response.text)[1]


def test_market_impact_candidate_is_frozen_and_click_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', '')
    app = create_app(db_url=f'sqlite:///{tmp_path / "impact.db"}', storage_root=str(tmp_path / 'runs'))
    settings = app.state.research_settings
    settings.write({**settings.get(), 'runtime_mode': 'local', 'search_provider': 'rundesk'})
    fake = FakeRunDesk()
    app.state.research.client_factory = lambda config: fake
    with Session(app.state.engine) as session:
        key, = record_reports(session, [
            dict(title='Chile announces export tariff on copper concentrate',
                 url='https://publisher.example/copper', content='Proposed levy details', content_kind='excerpt')
        ], provider='worldnewsapi', batch='case-test')
        session.commit()
    client = TestClient(app)
    assert client.post('/api/research/setup').status_code == 200
    url = '/research/market-impact'
    page = client.get(url, params={'candidate_key': key})
    assert 'https://publisher.example/copper' in page.text and '铜' in page.text
    form = {'candidate_key': key, 'receipt': receipt(page)}
    with Session(app.state.engine) as session:
        assert session.exec(select(ResearchCase)).first() is None
    assert client.post(url, data=form, headers={'Origin': 'https://outside.example'}).status_code == 403
    for attempt in range(2):
        response = client.post(url, data=form, follow_redirects=False)
        assert response.status_code == 303, response.text
    case_id = response.headers['location'].rsplit('/', 1)[-1]
    case = client.get('/api/research/cases/' + case_id).json()
    assert case['strategy']['market_impact']['hypothesis']['product_id'] == 'copper'
    assert case['strategy']['market_impact']['candidate_key'] == key
    assert case['sources'][0]['url'] == 'https://publisher.example/copper'
    assert case['sources'][0]['kind'] == 'search_snippet'
    assert len(case['runs']) == 1
    run_id = case['runs'][0]['run_id']
    app.state.research.dispatch(run_id)
    assert len(fake.sessions) == 1

    monkeypatch.setenv('NEWS2DOUYIN_TRADE_VIEW_BASE_URL', 'http://tradeview.example')
    monkeypatch.setenv('NEWS2DOUYIN_TRADE_VIEW_API_KEY', 'local-test-key')
    monkeypatch.setenv('NEWS2DOUYIN_TRADE_VIEW_PUBLIC_URL', 'https://tradeview.example')
    upstream = []

    def fake_service(base, key, path, params):
        upstream.append((base, key, path, params))
        meta = dict(series_id='ak.fut:CU.close', source='ak.fut', name='铜期货收盘', freq='D', unit='元/吨')
        if path.endswith('/series'):
            return dict(vintage='latest', product_id='copper', mapping_state='mapped', truncated=False, items=[meta])
        return dict(vintage='latest', metadata=meta, **params,
                    items=[{'date': params['start'], 'value': 0.0}, {'date': params['end'], 'value': None}])

    monkeypatch.setattr(interop, 'trade_view_get', fake_service)
    headers = {'Authorization': 'Bearer ' + settings.get()['mcp_token']}

    def mcp(name, arguments):
        return client.post('/mcp/research', headers=headers, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': name, 'arguments': dict(run_id=run_id, **arguments)},
        }).json()['result']

    assert not mcp('research_commodity_series', {'product_id': 'copper'})['isError']
    args = dict(series_id='ak.fut:CU.close', start='2026-01-01', end='2026-01-02')
    result = mcp('research_commodity_observations', args)
    assert not result['isError'], result
    assert mcp('research_commodity_observations', args) == result
    assert len(upstream) == 2
    case = client.get('/api/research/cases/' + case_id).json()
    market = next(row for row in case['sources'] if row['kind'] == 'market_observation')
    detail = client.get(f'/api/research/cases/{case_id}/sources/{market["source_id"]}').json()
    assert detail['provenance']['time_basis'] == 'latest_revised'
    assert [p['text'] for p in detail['paragraphs']][-2:] == ['2026-01-01 | 0.0', '2026-01-02 | 缺值']
    assert not mcp('research_save_claim', {'text': '首日该序列收录值为零', 'kind': 'fact',
        'evidence': [{'source_id': market['source_id'], 'paragraph_id': detail['paragraphs'][-2]['id'],
                      'quote': '2026-01-01 | 0.0'}]})['isError']
    assert mcp('research_commodity_observations', dict(args, end='2026-08-01'))['isError']
    assert len(upstream) == 2


def test_market_impact_uses_the_moment_version_and_keeps_draft_on_unconfigured_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', '')
    app = create_app(db_url=f'sqlite:///{tmp_path / "impact.db"}', storage_root=str(tmp_path / 'runs'))
    with Session(app.state.engine) as session:
        event = Event(event_key='mine', event_title='铜矿停产')
        article = Article(article_key='publisher', title='铜矿暂停生产', url='https://publisher.example/mine',
                          content='铜矿停产十天。', provider='worldnewsapi',
                          raw_json='{"content_kind":"text"}')
        session.add_all([event, article, ArticleEventLink(article_key='publisher', event_key='mine')])
        session.flush()
        record_version(session, article)
        from news2douyin.storage.articles import latest_version
        version = latest_version(session, article.article_key)
        session.add(EventMoment(moment_key='paused', event_key='mine', title='铜矿暂停生产', reviewed=True,
            date_start='2026-01-02', sources_json=dumps([dict(article_key='publisher', revision=version.revision,
                                                               content_hash=version.content_hash)])))
        article.content = '铜矿已经复产。'
        record_version(session, article)
        session.commit()
    client = TestClient(app)
    url = '/research/market-impact'
    params = dict(event_key='mine', moment_key='paused')
    page = client.get(url, params=params)
    assert '版本 r1' in page.text
    response = client.post(url, data=dict(params, receipt=receipt(page)))
    assert '连接 RunDesk' in response.text
    assert client.get('/events/mine').status_code == 200
    assert '市场研究记录' in client.get('/events/mine').text
    with Session(app.state.engine) as session:
        case, = session.exec(select(ResearchCase)).all()
        assert session.exec(select(ResearchRun)).first() is None
    snapshot = client.get('/api/research/cases/' + case.case_id).json()['sources'][0]
    assert snapshot['provenance']['article_version'] == 1
    assert snapshot['provenance']['market_impact']['moment_key'] == 'paused'
    assert snapshot['kind'] == 'news_snapshot'
