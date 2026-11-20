"""Radar hotspot browsing and unsaved focus hand-off."""
import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlmodel import Session, func, select

from news2douyin.desk.models import Watch
from news2douyin.server.app import create_app
from news2douyin.server import hotspots as web


def test_radar_hotspots_filters_provenance_and_local_focus_prefill(tmp_path, monkeypatch):
    app = create_app(db_url=f'sqlite:///{tmp_path / "news.db"}', storage_root=str(tmp_path))
    client = TestClient(app)
    as_of = datetime.now(timezone.utc).isoformat()
    title = '港湾地区地震 <通报>'
    topic = dict(id='hot_demo', title=title, category='disaster', status='emerging', status_label='新近出现',
                 summary='当地发布震情通报', recent_reports=2, last_seen=as_of, domains=2,
                 focus_description=f'持续跟踪「{title}」的后续进展。', focus_terms=['港湾地区地震', 'Harbor earthquake'],
                 reasons=['两个来源域名'], sources=[
                     dict(title='当地网站通报震情', url='https://news.example/quake', source_kind='gkg',
                          source_key='gkg:gkg_english:20261119000000:2'),
                     dict(title='危险地址不展示', url='javascript:alert(1)'),
                 ])
    reads = []

    def read_window(_session, **filters):
        reads.append(filters)
        return {'items': [], 'sources': {'daily': {'count': 2}}}

    monkeypatch.setattr(web, 'read_window', read_window)
    monkeypatch.setattr(web, 'detect_hotspots', lambda items, **options: [topic])
    try:
        assert 'href="/radar/hotspots"' in client.get('/radar').text
        page = client.get('/radar/hotspots')
        assert page.status_code == 200 and '港湾地区地震 &lt;通报&gt;' in page.text
        assert 'https://news.example/quake' in page.text and 'javascript:alert(1)' not in page.text
        assert '/workbench/new?candidate=gkg%3Agkg_english%3A20261119000000%3A2' in page.text
        assert '/focus?new=1&amp;hotspot=hot_demo' in page.text
        assert title not in client.get('/radar/hotspots?category=conflict').text
        assert '暂无可辨认的热点' in client.get('/radar/hotspots?category=conflict').text
        assert client.get('/radar/hotspots?category=invalid').status_code == 422

        draft = client.get('/focus?new=1&hotspot=hot_demo')
        assert draft.status_code == 200 and '持续跟踪「港湾地区地震 &lt;通报&gt;」' in draft.text
        payload = draft.text.split('id="focus-config" type="application/json">', 1)[1].split('</script>', 1)[0]
        assert json.loads(payload)['seed_terms'] == topic['focus_terms']
        assert client.get('/focus?new=1&hotspot=missing').status_code == 404
        assert client.get('/focus?new=1&hotspot=hot_demo&seed=other').status_code == 422
        assert len(reads) == 1  # The filters and focus hand-off reuse one short-lived snapshot.

        with Session(app.state.engine) as session:
            assert session.exec(select(func.count()).select_from(Watch)).one() == 0
            session.add(Watch(key='w_already', name=title, kind='长期事件', scope='港湾地区地震'))
            session.commit()
        again = client.get('/radar/hotspots')
        assert 'href="/focus/w_already">打开焦点' in again.text
    finally:
        client.close()
        app.state.engine.dispose()
