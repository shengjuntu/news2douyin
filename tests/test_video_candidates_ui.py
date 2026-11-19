"""The production video edition is a separately readable snapshot."""
from datetime import timedelta
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as db_event
from sqlmodel import Session

from news2douyin.digest import video_service
from news2douyin.digest.models import DailyDigestIssue, VideoDigestIssue
from news2douyin.digest.service import default_day
from news2douyin.server.app import create_app


@pytest.fixture
def suite(tmp_path, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', '')
    monkeypatch.setenv('NEWS2DOUYIN_DIGEST_ENABLED', '1')
    app = create_app(db_url='sqlite:///' + str(tmp_path / 'video.db'), storage_root=str(tmp_path / 'runs'))
    try:
        yield app, TestClient(app)
    finally:
        app.state.engine.dispose()


def _seed(app, day):
    earlier = (default_day() - timedelta(days=1)).isoformat()
    card = dict(
        key='progress_video', title='码头工人改走小路 <script>alert(1)</script>',
        excerpt='桥梁修好之前，通勤者步行过河后才能乘坐公交车。',
        source_url='https://press.example/ride', source_kind='collected', source_key='report:demo',
        source_ref={'provider': 'worldnewsapi'},
        sources=[dict(url='https://more.example/bus', title='查看交通公告'),
                 dict(url='javascript:alert(2)', title='恶意链接')],
        video=dict(person={'text': '通勤者', 'quote': '通勤者步行过河', 'field': 'excerpt'},
                   movement={'text': '路线调整', 'quote': '步行过河后才能乘坐公交车', 'field': 'excerpt'},
                   life={'text': '通勤', 'quote': '通勤者', 'field': 'excerpt'},
                   evidence={'status': 'excerpt', 'url': 'https://press.example/ride', 'source_key': 'report:demo'}),
        story_history=[dict(day=earlier, key='older', title='较早的通知',
                            url='https://press.example/bridge', reason='同一原文的较早标题')],
    )
    cards = [card] + [dict(card, key=f'video_{index}', title=f'第{index}条候选') for index in range(2, 101)]
    with Session(app.state.engine) as session:
        session.add(VideoDigestIssue(day=day, status='ready', items_json=json.dumps(cards)))
        session.add(DailyDigestIssue(day=day, status='ready', items_json=json.dumps([dict(key='ordinary', title='普通日报独有进展')])))
        session.commit()
    return earlier, card


def test_video_snapshot_sources_story_and_all_100_download(suite):
    app, client = suite
    day = default_day().isoformat()
    earlier, card = _seed(app, day)
    queries = []

    def capture(_conn, _cursor, statement, *_args):
        queries.append(statement)

    db_event.listen(app.state.engine, 'before_cursor_execute', capture)
    try:
        page = client.get('/radar/video', params={'day': day})
        report = client.get('/radar/video/export.md', params={'day': day, 'revision': 1})
    finally:
        db_event.remove(app.state.engine, 'before_cursor_execute', capture)
    assert page.status_code == report.status_code == 200
    assert all(query.lstrip().upper().startswith('SELECT') and 'videodigestissue' in query.lower() for query in queries)
    assert '普通日报独有进展' not in page.text + report.text
    main = client.get('/radar', params={'day': day})
    assert main.status_code == 200 and '普通日报独有进展' in main.text
    assert '码头工人改走小路' not in main.text
    assert f'href="/radar/video?day={day}"' in main.text
    assert '下载 Markdown（100 条）' in page.text
    assert '第100条候选' in page.text
    for label in ('人物', '情节', '生活关联', '故事回看', '较早的通知', '跟踪这个故事', '形成选题'):
        assert label in page.text
    assert f'/radar/video?day={earlier}#video-' in page.text
    assert f'video_day={day}&amp;video_key={card["key"]}' in page.text
    assert '/workbench/new?candidate=report%3Ademo' in page.text
    assert 'https://press.example/ride' in page.text + report.text
    assert 'javascript:alert(2)' not in page.text + report.text
    assert '<script>alert(1)' not in page.text + report.text
    assert '&lt;script&gt;' in page.text and '&lt;script&gt;' in report.text
    assert report.headers['content-disposition'] == f'attachment; filename="news2douyin-video-candidates-{day}-r1.md"'
    assert report.headers['content-type'].startswith('text/markdown')
    assert len([line for line in report.text.splitlines() if line.startswith('## ')]) == 100
    for label in ('人物', '情节', '生活关联', '通勤者步行过河', '较早的通知', '条目 ID　`progress_video`'):
        assert label in report.text
    assert client.get('/radar/video/export.md', params={'day': day, 'revision': 2}).status_code == 409


def test_generate_only_queues_own_day_with_origin_and_status(suite, monkeypatch):
    app, client = suite
    day = (default_day() - timedelta(days=3)).isoformat()

    def forbidden(*_args, **_kwargs):
        raise AssertionError('HTTP request must not build either edition')

    monkeypatch.setattr(video_service, 'build_issue', forbidden)
    response = client.get('/radar/video', params={'day': day})
    assert response.status_code == 200 and '生成视频候选' in response.text
    assert client.get('/radar/video/export.md', params={'day': day}).status_code == 404
    assert client.get('/api/radar/video', params={'day': day}).json()['requested'] is False
    assert client.post('/radar/video/generate', data={'day': day}, headers={'Origin': 'https://cross.example'}).status_code == 403
    for _ in range(2):
        posted = client.post('/radar/video/generate', data={'day': day}, follow_redirects=False)
        assert posted.status_code == 303 and posted.headers['location'] == '/radar/video?day=' + day
    state = client.get('/api/radar/video', params={'day': day}).json()
    assert state['requested'] is True and state['build_status'] == 'pending' and 'items' not in state
    assert '生成已排队' in client.get('/radar/video', params={'day': day}).text
    with Session(app.state.engine) as session:
        assert session.get(DailyDigestIssue, day) is None
    for raw in ('tomorrow', (default_day() + timedelta(days=1)).isoformat()):
        assert client.get('/radar/video', params={'day': raw}).status_code == 422
        assert client.post('/radar/video/generate', data={'day': raw}).status_code == 422
    monkeypatch.setenv('NEWS2DOUYIN_DIGEST_ENABLED', '0')
    paused_day = (default_day() - timedelta(days=4)).isoformat()
    paused = client.post('/radar/video/generate', data={'day': paused_day})
    assert paused.status_code == 503 and '视频候选生成已暂停' in paused.text
    assert not client.get('/api/radar/video', params={'day': paused_day}).json()['requested']
