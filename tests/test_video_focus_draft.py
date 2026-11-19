"""A saved video candidate can start a focus draft without changing either edition."""
import json

from fastapi.testclient import TestClient
from sqlmodel import Session, func, select

from news2douyin.desk.models import Watch
from news2douyin.digest.models import DailyDigestIssue, VideoDigestIssue
from news2douyin.server.app import create_app


def test_saved_video_story_prefills_a_read_only_focus_draft(tmp_path):
    app = create_app(db_url=f'sqlite:///{tmp_path / "desk.db"}', storage_root=str(tmp_path))
    client = TestClient(app)
    query = {'new': 1, 'video_day': '2024-11-01', 'video_key': 'daily:story-1'}
    name = '巴士司机换班后，把乘客送回了家 <记事>'
    try:
        with Session(app.state.engine) as session:
            session.add(DailyDigestIssue(day=query['video_day'], status='ready',
                items_json=json.dumps([{'key': query['video_key'], 'title': '每日精选标题'}])))
            session.add(VideoDigestIssue(day=query['video_day'], status='ready',
                items_json=json.dumps([{'key': query['video_key'], 'title': name}])))
            session.commit()

        response = client.get('/focus', params=query)
        assert response.status_code == 200
        assert '持续跟踪「巴士司机换班后，把乘客送回了家 &lt;记事&gt;」的后续进展' in response.text
        assert '每日精选标题' not in response.text
        assert client.get('/focus', params={**query, 'video_key': 'missing'}).status_code == 404
        with Session(app.state.engine) as session:
            assert session.exec(select(func.count()).select_from(Watch)).one() == 0
    finally:
        client.close()
        app.state.engine.dispose()
