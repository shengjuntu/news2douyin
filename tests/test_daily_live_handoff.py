import json
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.digest.handoff import card_token
from news2douyin.digest.models import DailyDigestIssue
from news2douyin.digest.service import default_day
from news2douyin.server.app import create_app
from news2douyin.topics.models import TopicSource


def test_old_tab_keeps_selected_materials_or_requests_a_refresh(tmp_path):
    app = create_app(db_url=f'sqlite:///{tmp_path}/daily.db', storage_root=str(tmp_path))
    today = default_day().isoformat()
    first = dict(key='progress-a', title='港口公布新的通行时段', excerpt='原始公告摘录。',
                 source_kind='local', source_key='local:1', source_url='https://paper.example/a')
    second = dict(first, excerpt='更新版说明增加了一条规定。')

    def publish(cards):
        with Session(app.state.engine) as session:
            issue = session.get(DailyDigestIssue, today) or DailyDigestIssue(day=today)
            issue.status, issue.items_json = 'live', json.dumps(cards)
            session.add(issue)
            session.commit()

    client = TestClient(app)
    try:
        publish([first])
        fields = dict(digest_day=today, progress=first['key'], digest_token=card_token(first))
        payload = dict(fields, request_id='daily-first-materials')
        assert client.get('/workbench/new', params=fields).status_code == 200
        created = client.post('/api/workbench', json=payload)
        assert created.status_code == 201, created.text
        key = created.json()['key']
        publish([second])
        stale = client.get('/workbench/new', params=fields)
        assert stale.status_code == 409 and ('/radar?day=' + today) in stale.text
        assert client.post('/api/workbench', json=dict(payload, request_id='daily-new-reader')).status_code == 409
        assert client.post('/api/workbench', json=payload).json()['key'] == key
        with Session(app.state.engine) as session:
            material = session.exec(select(TopicSource).where(TopicSource.workspace_id == key)).one()
            assert json.loads(material.document_json)['content'] == first['excerpt']
        assert client.get('/workbench/new', params=dict(fields, digest_token=card_token(second))).status_code == 200
        publish([])
        assert client.get('/workbench/new', params=fields).status_code == 409
    finally:
        client.close()
        app.state.engine.dispose()


def test_review_of_today_uses_the_published_revision_cutoff(tmp_path, monkeypatch):
    from datetime import timedelta
    from news2douyin.digest import review_cli
    from news2douyin.storage.db import make_engine, init_db

    url = f'sqlite:///{tmp_path}/review.db'
    engine = make_engine(url)
    init_db(engine)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    day = default_day(now).isoformat()
    publication = dict(kind='intraday', revision=2, cutoff_at=now.isoformat())
    with Session(engine) as session:
        session.add(DailyDigestIssue(day=day, status='live', stats_json=json.dumps(dict(publication=publication))))
        session.commit()
    engine.dispose()
    observed = []
    def candidates(engine, root, day, counts, **kwargs):
        observed.append(kwargs['cutoff'])
        return ()
    monkeypatch.setattr(review_cli, 'observed_candidates', candidates)
    out = tmp_path/'review'
    review_cli.main(['export-daily', '--start', day, '--end', day, '--db-url', url,
                     '--out', str(out), '--outside', '0'])
    from news2douyin.digest.calendar import day_bounds_utc
    assert observed == [min(now + timedelta(seconds=1), day_bounds_utc(default_day(now))[1])]
    info = json.loads((out/'daily-key.json').read_text())['days'][day]
    assert info['edition_kind'] == 'intraday' and info['revision'] == 2
