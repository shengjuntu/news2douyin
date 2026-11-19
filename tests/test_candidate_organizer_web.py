from datetime import datetime, timezone
from urllib.parse import quote

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.candidates.service import record_reports
from news2douyin.server.app import create_app
from news2douyin.storage.models import Event


def token_from(response):
    assert response.status_code == 200, response.text
    return response.text.split('data-token="', 1)[1].split('"', 1)[0]


def test_organize_collected_report_from_preview_and_join_event(tmp_path, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', '')
    app = create_app(db_url=f'sqlite:///{tmp_path / "organize.db"}', storage_root=str(tmp_path / 'runs'))
    moment = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    title = 'تحديث بشأن افتتاح الميناء'
    first_url = 'https://publisher.example/port-open'
    with Session(app.state.engine) as session:
        first, second = record_reports(session, [
            {'url': first_url, 'title': title, 'language': 'ar', 'content': 'تفاصيل افتتاح الميناء',
             'fetched_at': moment, 'published_at': moment},
            {'url': 'https://publisher.example/port-arrivals', 'title': 'New arrivals at the port',
             'language': 'en', 'content': 'Ships arrived after the opening.',
             'fetched_at': moment, 'published_at': moment},
        ], provider='worldnewsapi', batch='organize-web')
        session.commit()
        assert session.exec(select(Event)).first() is None

    with TestClient(app) as client:
        listing = client.get('/candidates', params={'provider': 'worldnewsapi', 'language': 'ar'})
        assert listing.status_code == 200, listing.text
        assert title in listing.text and 'New arrivals at the port' not in listing.text
        assert f'/organize?candidate={quote(first, safe="")}' in listing.text
        assert f'/workbench/new?candidate={quote(first, safe="")}' in listing.text

        preview = client.get('/organize', params={'candidate': first})
        payload = dict(candidate_key=first, expected_token=token_from(preview), request_id='organize-web-create',
                       title='Harbor opening', note='Read the publisher report')
        assert first_url in preview.text
        assert '阿拉伯语' in preview.text
        assert client.post('/api/candidates/organize', json=payload,
                           headers={'Origin': 'https://other.example'}).status_code == 403

        created = client.post('/api/candidates/organize', json=payload)
        assert created.status_code == 200, created.text
        event = created.json()
        assert client.post('/api/candidates/organize', json=payload).json() == event
        assert client.get(event['url']).status_code == 200
        targets = client.get('/api/candidates/organize/targets', params={'q': 'Harbor opening'}).json()
        assert [(item['event_key'], item['version']) for item in targets] == [(event['event_key'], event['version'])]

        attach = dict(candidate_key=second, expected_token=token_from(
            client.get('/organize', params={'candidate': second})), request_id='organize-web-attach',
            event_key=event['event_key'], expected_version=event['version'])
        attached = client.post('/api/candidates/organize', json=attach)
        assert attached.status_code == 200, attached.text
        assert attached.json()['event_key'] == event['event_key']
        persisted = client.get('/api/events/' + event['event_key'] + '/workspace').json()
        assert {source['url'] for source in persisted['sources']} == {
            first_url, 'https://publisher.example/port-arrivals'
        }
