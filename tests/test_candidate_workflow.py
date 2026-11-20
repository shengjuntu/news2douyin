"""One acquired report can enter Focus even when absent from both the article list and Top N."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlmodel import Session, select

from news2douyin.candidates import service as candidates
from news2douyin.candidates.models import SourceReport
from news2douyin.candidates.service import candidate_token, resolve_candidate
from news2douyin.collect.providers import search_news
from news2douyin.collect.service import run_collection, create_or_update_profile
from news2douyin.desk.focus_leads import CURSOR, scan_step
from news2douyin.desk.focus_models import FocusLead
from news2douyin.desk.models import DeskMeta
from news2douyin.digest.calendar import day_bounds_utc
from news2douyin.digest.selection import SelectionPick
from news2douyin.digest.service import build_issue, default_day
from news2douyin.server.app import create_app
from news2douyin.storage.models import Article, ArticleEventLink, Event, EventActivity

TITLE = 'Mombasa yazindua reli mpya ya mizigo'
OTHER = 'Taarifa ya bei ya nishati'


class OneEditorialPick:
    api_version = 1
    version = '1'

    def __init__(self, options):
        self.title = options['title']

    def select(self, candidates, context):
        for candidate in candidates:
            if candidate.title == self.title:
                return [SelectionPick(candidate, score=1.0, reasons=('test choice',))]
        return []


class EntryPoint:
    name = 'test-editorial'

    def load(self):
        return OneEditorialPick


def test_new_language_report_can_be_recovered_by_focus_and_organized(tmp_path, monkeypatch):
    now = datetime(2025, 4, 18, 7, 10, tzinfo=timezone.utc)
    observed = now.isoformat(timespec='seconds').replace('+00:00', 'Z')
    cutoff = now + timedelta(minutes=1)
    language = tmp_path / 'languages.toml'
    language.write_text(
        '[labels]\n"sw-ke" = "斯瓦希里语（肯尼亚）"\n'
        '[providers.worldnewsapi.aliases]\n"SWH-KE" = "sw-ke"\n'
        '[providers.worldnewsapi.query]\n"sw-ke" = "sw"\n', encoding='utf-8',
    )
    monkeypatch.setenv('NEWS2DOUYIN_LANGUAGE_CONFIG', str(language))
    config = tmp_path / 'selection.toml'
    config.write_text('[selection]\nstrategy="test-editorial"\nlimit=1\n[selection.options]\n'
                      f'title="{OTHER}"\n', encoding='utf-8')
    monkeypatch.setenv('NEWS2DOUYIN_SELECTION_CONFIG', str(config))
    monkeypatch.setenv('API_KEY', 'test-key')
    from news2douyin.digest import selection
    monkeypatch.setattr(selection.metadata, 'entry_points', lambda **_: (EntryPoint(),))

    class Response:
        status_code = 200

        def json(self):
            return {'news': [
                {'id': 71, 'title': TITLE, 'url': 'https://rail.example/mombasa', 'language': 'SWH-KE',
                 'text': 'Bandari ya Mombasa imeanza kutumia reli mpya ya mizigo.', 'publish_date': observed},
                {'id': 72, 'title': OTHER, 'url': 'https://energy.example/prices', 'language': 'SWH-KE',
                 'text': 'Serikali imetoa taarifa ya bei ya nishati.', 'publish_date': observed},
            ]}

    def get(_url, *, params, **_kwargs):
        assert params['language'] == 'sw'
        assert params['text'] == 'taarifa'
        return Response()

    monkeypatch.setattr(search_news.requests, 'get', get)
    monkeypatch.setattr(search_news, 'utc_now_iso', lambda: observed)
    monkeypatch.setattr(candidates, 'utc_now_iso', lambda: observed)

    app = create_app(db_url=f'sqlite:///{tmp_path / "app.db"}', storage_root=str(tmp_path))
    client = TestClient(app)
    try:
        focus = client.post('/api/focus', json={'name': '蒙巴萨港', 'kind': '长期事件', 'scope': 'Mombasa'})
        assert focus.status_code == 201, focus.text
        watch = focus.json()['key']
        with Session(app.state.engine) as session:
            create_or_update_profile(session, dict(name='ke', provider='worldnewsapi', country='ke', language='sw-ke',
                collection_mode='search', search_query='taarifa', filter_mode='rules',
                date_str='2025-04-18', keywords_include=['nishati']))
            run = run_collection(session, 'ke', storage_root=tmp_path)
            assert json.loads(run.stats_json)['after_filter'] == 1
            assert TITLE in (Path(run.storage_path) / 'raw.jsonl').read_text(encoding='utf-8')
            candidate = session.exec(select(SourceReport).where(SourceReport.title == TITLE)).one()
            key = candidate.key
            assert candidate.language == 'sw-ke'
            assert json.loads(candidate.provenance_json)['source_language'] == 'SWH-KE'
            assert session.exec(select(Article.title)).all() == [OTHER]
            assert session.exec(select(func.count()).select_from(Event)).one() == 0

        edition = build_issue(app.state.engine, default_day(cutoff), storage_root=tmp_path, now=cutoff)
        assert [item['title'] for item in edition['items']] == [OTHER]
        assert edition['stats']['selection']['strategy'] == 'test-editorial'
        assert edition['stats']['sources']['collected_emitted'] == 2

        day_start, _ = day_bounds_utc(default_day(cutoff))
        begin = max(day_start, now - timedelta(minutes=5)).isoformat(timespec='seconds').replace('+00:00', 'Z')
        with Session(app.state.engine) as session:
            session.merge(DeskMeta(key=CURSOR, value=json.dumps({'through': begin, 'versions': [[watch, 1]]})))
            session.commit()
        assert scan_step(app.state.engine, tmp_path, now=cutoff)
        with Session(app.state.engine) as session:
            lead = session.exec(select(FocusLead).where(FocusLead.watch == watch)).one()
            assert lead.candidate == key and lead.language == 'sw-ke'
            token = candidate_token(resolve_candidate(session, tmp_path, key))
            assert session.exec(select(func.count()).select_from(Event)).one() == 0

        assert TITLE in client.get('/focus/' + watch, params={'view': 'leads'}).text
        page = client.get('/workbench/new', params={'candidate': key})
        assert page.status_code == 200 and TITLE in page.text
        draft = client.post('/api/workbench', json=dict(candidate=key, candidate_token=token, request_id='fixture-draft'))
        assert draft.status_code == 201, draft.text
        assert draft.json()['sources'][0]['language'] == 'sw-ke'
        with Session(app.state.engine) as session:
            assert session.exec(select(func.count()).select_from(Event)).one() == 0
        response = client.post('/api/candidates/organize', json=dict(
            candidate_key=key, expected_token=token, request_id='fixture-organize'))
        assert response.status_code in (200, 201), response.text
        event_key = response.json()['event_key']
        with Session(app.state.engine) as session:
            assert session.exec(select(Event.event_key)).one() == event_key
            assert session.exec(select(ArticleEventLink.event_key)).one() == event_key
            assert key in session.exec(select(EventActivity.payload_json).where(
                EventActivity.event_key == event_key, EventActivity.action == 'promote_candidate')).one()
    finally:
        client.close()
        app.state.engine.dispose()
