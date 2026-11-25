from fastapi.testclient import TestClient
from sqlmodel import Session

from news2douyin.server.app import create_app
from news2douyin.server.product_pages import trade_view_public_url
from news2douyin.storage.models import Event, EventProductEvidence


def test_event_workspace_and_product_reverse_link(tmp_path, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_TRADE_VIEW_BASE_URL', 'http://backend-only.test')
    monkeypatch.setenv('NEWS2DOUYIN_TRADE_VIEW_API_KEY', 'private-test-value')
    monkeypatch.setenv('NEWS2DOUYIN_TRADE_VIEW_PUBLIC_URL', 'https://market.test/demo/')
    app = create_app(db_url=f'sqlite:///{tmp_path}/db', storage_root=str(tmp_path/'runs'))
    client = TestClient(app)
    with Session(app.state.engine) as session:
        session.add(Event(event_key='pilot', event_title='铜精矿出口通告'))
        session.add(EventProductEvidence(event_key='pilot', product_id='copper', article_key='report',
                    revision=2, content_hash='abc', source_url='https://news.test/report',
                    source_title='原通告', excerpt='铜精矿 <复核>'))
        session.commit()

    page = client.get('/events/pilot?products=1')
    assert page.status_code == 200
    assert '商品与原文证据' in page.text and 'product-evidence.js' in page.text
    assert 'data-market-url="https://market.test/demo" open' in page.text
    assert client.get('/research-assets/product-evidence.js').status_code == 200
    reverse = client.get('/commodities/copper/events')
    assert reverse.status_code == 200 and '铜精矿 &lt;复核&gt;' in reverse.text
    assert '/events/pilot?products=1' in reverse.text and '/articles/report?revision=2' in reverse.text
    assert 'href="https://market.test/demo/console?product_id=copper#commodities"' in reverse.text
    assert 'backend-only.test' not in page.text + reverse.text
    assert 'private-test-value' not in page.text + reverse.text
    assert '尚无此商品的已核对事件' in client.get('/commodities/silver/events').text
    assert client.get('/commodities/!!!/events').status_code == 422
    assert client.get('/commodities/copper/events?before_id=0').status_code == 422


def test_public_market_link_never_falls_back_to_backend_or_embedded_credentials(monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_TRADE_VIEW_BASE_URL', 'http://backend-only.test')
    monkeypatch.delenv('NEWS2DOUYIN_TRADE_VIEW_PUBLIC_URL', raising=False)
    assert not trade_view_public_url()
    for value in ('javascript:alert(1)', 'https://key@market.test', 'https://host/path?key=private',
                  'https://market.test/#token', 'https://market.test\\\\example'):
        monkeypatch.setenv('NEWS2DOUYIN_TRADE_VIEW_PUBLIC_URL', value)
        assert not trade_view_public_url()
