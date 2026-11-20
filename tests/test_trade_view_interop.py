import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from news2douyin.server import interop
from news2douyin.storage.models import Article, ArticleEventLink, Event, EventMoment, EventState


def client_for(engine):
    app = FastAPI()
    interop.register(app, engine)
    return TestClient(app)


def test_news_feed_requires_key_and_reads_beijing_dates_without_claiming_occurrence(engine, monkeypatch):
    client = client_for(engine)
    params = {'start': '2026-11-09', 'end': '2026-11-09', 'q': '港口'}
    path = '/api/v1/interop/news-events'
    monkeypatch.delenv('NEWS2DOUYIN_INTEROP_API_KEY', raising=False)
    assert client.get(path, params=params).status_code == 503
    monkeypatch.setenv('NEWS2DOUYIN_INTEROP_API_KEY', 'event-test')
    assert client.get(path, params=params).status_code == 401
    headers = {'X-API-Key': 'event-test'}

    with Session(engine) as session:
        session.add_all([
            Event(event_key='first', event_title='港口报告', first_seen_at='2026-11-08T16:00:00+00:00'),
            Event(event_key='second', event_title='Raw title', first_seen_at='2026-11-09T15:59:59Z'),
            Event(event_key='before', event_title='港口更早', first_seen_at='2026-11-08T15:59:59Z'),
            Event(event_key='after', event_title='港口更晚', first_seen_at='2026-11-09T16:00:00Z'),
            Event(event_key='merged', event_title='港口已合并', first_seen_at='2026-11-09T05:00:00Z'),
            EventState(event_key='second', title_override='港口补充通报'),
            EventState(event_key='merged', merged_into='second'),
            Article(article_key='article', url='https://example.test/report'),
            ArticleEventLink(article_key='article', event_key='second'),
            EventMoment(event_key='second', moment_key='moment', title='发生节点',
                        time_kind='occurred', date_start='2026-11-05', certainty='exact',
                        reviewed=True, sources_json='[{"article_key":"article"}]'),
        ])
        session.commit()

    assert client.get(path, params={**params, 'end': '2027-11-10'}, headers=headers).status_code == 422
    data = client.get(path, params={**params, 'limit': 1}, headers=headers).json()['data']
    assert data['timezone'] == 'Asia/Shanghai' and data['time_basis'] == 'first_seen_at'
    assert data['truncated']
    assert data['items'] == [{
        'event_key': 'second', 'title': '港口补充通报', 'topic': '',
        'first_seen_at': '2026-11-09T15:59:59Z', 'source_url': 'https://example.test/report',
        'event_path': '/events/second', 'reviewed_moment_date': '2026-11-05',
    }]
    data = client.get(path, params=params, headers=headers).json()['data']
    assert [item['event_key'] for item in data['items']] == ['second', 'first']
    assert 'reviewed_moment_date' not in data['items'][1]
    assert not data['truncated']


def test_market_proxy_uses_server_credentials_and_preserves_price_basis(engine, monkeypatch):
    client = client_for(engine)
    path = '/api/v1/interop/market-series'
    params = {'symbol': '000001', 'start': '2026-11-09', 'end': '2026-11-09'}
    monkeypatch.delenv('NEWS2DOUYIN_TRADE_VIEW_BASE_URL', raising=False)
    assert client.get(path, params=params).status_code == 503
    monkeypatch.setenv('NEWS2DOUYIN_TRADE_VIEW_BASE_URL', 'http://trade-view.test')
    monkeypatch.setenv('NEWS2DOUYIN_TRADE_VIEW_API_KEY', 'market-test')
    assert client.get(path, params=params, headers={'Origin': 'http://other.test'}).status_code == 403

    payload = {'ok': True, 'data': {
        'symbol': '000001.SZ', 'frequency': '1d', 'timezone': 'Asia/Shanghai',
        'start': '2026-11-09', 'end': '2026-11-09', 'availability': 'available',
        'source': 'collection.daily', 'items': [
            {'date': '2026-11-09', 'close': 12.37, 'price_unit': 'CNY', 'adjustment': 'unadjusted'},
        ],
    }}
    seen = []

    class FakeResponse:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def iter_content(self, chunk_size):
            yield json.dumps(payload).encode()

    class FakeClient(FakeResponse):
        def get(self, url, **kwargs):
            seen.append((url, kwargs, self.trust_env))
            return FakeResponse()

    monkeypatch.setattr(interop.requests, 'Session', FakeClient)
    response = client.get(path, params=params, headers={'X-API-Key': 'untrusted-client-input'})
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'no-store'
    assert 'market-test' not in response.text
    data = response.json()['data']
    assert data['symbol'] == '000001.SZ' and data['source'] == 'collection.daily'
    assert data['items'][0]['price_unit'] == 'CNY' and data['items'][0]['adjustment'] == 'unadjusted'
    url, request, trust_env = seen[0]
    assert url == 'http://trade-view.test' + path and request['params'] == params
    assert request['headers']['X-API-Key'] == 'market-test'
    assert not request['allow_redirects'] and not trust_env
    assert client.get(path, params={**params, 'symbol': '000001.SH'}).status_code == 502
