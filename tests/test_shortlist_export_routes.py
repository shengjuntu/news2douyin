import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from news2douyin.digest.cli import main
from news2douyin.digest.models import DailyDigestIssue
from news2douyin.server.daily_brief import register
from news2douyin.storage.db import create_schema, make_engine


def test_revision_bound_complete_export_and_cli(tmp_path, capsys):
    day = '2025-11-15'
    url = f'sqlite:///{tmp_path}/snapshot.db'
    engine = make_engine(url)
    create_schema(engine)
    app = FastAPI()
    register(app, engine)
    endpoint = f'/api/radar/daily/{day}/shortlist.md'
    path = tmp_path / 'issue.md'
    names = [f'Community dispatch number {i:02d}' for i in range(13)]
    cards = [dict(key=f'key-{i}', source_kind='gkg', title=title,
                  source_url=f'https://paper.example/{i}') for i, title in enumerate(names)]
    payload = dict(publication=dict(kind='intraday', revision=3, cutoff_at=f'{day}T02:00:00Z'))
    try:
        with TestClient(app) as client:
            assert client.get(endpoint).status_code == 404
            with Session(engine) as session:
                session.add(DailyDigestIssue(day=day, status='refreshing', stats_json=json.dumps(payload),
                                             items_json=json.dumps(cards)))
                session.commit()
            pinned = client.get(endpoint, params={'revision': 3})
            assert pinned.status_code == 200 and pinned.headers['cache-control'] == 'no-store'
            assert pinned.headers['content-type'] == 'text/markdown; charset=utf-8'
            assert pinned.headers['content-disposition'] == f'attachment; filename="news2douyin-shortlist-{day}-r3.md"'
            assert pinned.content == client.get(endpoint).content
            assert [pinned.text.index(title) for title in names] == sorted(pinned.text.index(title) for title in names)
            stale = client.get(endpoint, params={'revision': 2})
            assert stale.status_code == 409 and f'/radar?day={day}' in stale.text
            assert client.get(endpoint, params={'revision': 0}).status_code == 422
            assert client.get('/api/radar/daily/2099-11-15/shortlist.md').status_code == 422
            assert client.get('/api/radar/daily/invalid/shortlist.md').status_code == 422
            args = ['export', '--day', day, '--db-url', url]
            assert main(args) == 0 and capsys.readouterr().out == pinned.text
            assert main([*args, '--out', str(path)]) == 0 and path.read_bytes() == pinned.content
            with pytest.raises(SystemExit) as absent:
                main(['export', '--day', '2025-11-14', '--db-url', url, '--out', str(path)])
            assert absent.value.code == 2 and path.read_bytes() == pinned.content

            with Session(engine) as session:
                issue = session.get(DailyDigestIssue, day)
                issue.status = 'ready'
                issue.items_json = '[]'
                payload['publication']['revision'] = 4
                issue.stats_json = json.dumps(payload)
                session.commit()
            empty = client.get(endpoint, params={'revision': 4})
            assert empty.status_code == 200 and names[0] not in empty.text and day in empty.text
            assert main([*args, '--out', str(path)]) == 0 and path.read_bytes() == empty.content
            assert client.get(endpoint, params={'revision': 3}).status_code == 409
    finally:
        engine.dispose()
