from datetime import date, datetime, timedelta
import json
from threading import Event
from types import SimpleNamespace

from sqlmodel import Session

from news2douyin.candidates import service as candidates
from news2douyin.digest import selection, service, video_service
from news2douyin.digest.candidate_pool import Candidate
from news2douyin.digest.models import DailyDigestIssue, VideoDigestIssue
from test_postgres_integration import postgres_url


DAY = date(2026, 4, 8)
NOW = datetime.fromisoformat('2026-04-08T17:20:10+08:00')


def _plan(name, keys, seen):
    def pick(stream, *, day, cutoff, prior_keys):
        rows = list(stream)
        seen.append((name, day, cutoff.isoformat(), [row.key for row in rows], set(prior_keys)))
        return dict(items=[dict(key=row.key, title=row.title) for row in rows if row.key in keys],
                    stats={'selection': {'strategy': name}})
    return SimpleNamespace(limit=100, needs_coarse=False, select=pick)


def _sources(monkeypatch):
    def feed(engine, storage_root, day, *, stats, cutoff, checkpoint, **_):
        checkpoint()
        stats['fixture_rows'] = 2
        for key, title in [('world', 'Regional summit statement'), ('story', 'A driver sets out again')]:
            yield Candidate(key, title, '2026-04-08T08:00:00Z', f'https://publisher.test/{key}', 'gkg')
    monkeypatch.setattr(candidates, 'iter_day', feed)


def test_same_closed_snapshot_feeds_two_independent_editions(engine, monkeypatch):
    _sources(monkeypatch)
    seen = []
    monkeypatch.setattr(selection, 'load_selection', lambda: _plan('configured-world', {'world'}, seen))
    monkeypatch.setattr(selection, 'create_selection', lambda strategy, *, limit: _plan(strategy, {'story'}, seen))
    built_at = NOW + timedelta(days=1)
    previous = (DAY - timedelta(days=1)).isoformat()
    with Session(engine) as session:
        session.add(DailyDigestIssue(day=previous, status='ready', keys_json='["prior-world"]'))
        session.add(VideoDigestIssue(day=previous, status='ready', keys_json='["prior-video"]'))
        session.commit()

    world = service.build_issue(engine, DAY, now=built_at)
    video = video_service.build_issue(engine, DAY, now=built_at)

    assert [item['key'] for item in world['items']] == ['world']
    assert [item['key'] for item in video['items']] == ['story']
    assert [entry[0] for entry in seen] == ['configured-world', 'video_story']
    assert seen[0][1:4] == seen[1][1:4] == (DAY, '2026-04-08T16:00:00+00:00', ['world', 'story'])
    assert seen[0][4] == {'prior-world'} and seen[1][4] == {'prior-video'}
    with Session(engine) as session:
        assert service.read_issue(session, DAY) == world
        assert video_service.read_issue(session, DAY) == video
        assert session.get(DailyDigestIssue, DAY.isoformat()).items_json != session.get(VideoDigestIssue, DAY.isoformat()).items_json


def test_video_refresh_failure_and_interruption_keep_published_cards(engine, monkeypatch):
    _sources(monkeypatch)
    monkeypatch.setattr(video_service, '_plan', lambda: _plan('video_story', {'story'}, []))
    day = service.last_closed_day()
    original = video_service.build_issue(engine, day)
    assert original['status'] == 'ready'
    video_service.request_rebuild(engine, day, require_worker=False)

    stopped = Event()
    stopped.set()
    interrupted = video_service.build_issue(engine, day, stop_event=stopped)
    assert interrupted['status'] == 'ready' and interrupted['build_status'] == 'pending'
    assert interrupted['items'] == original['items'] and interrupted['revision'] == original['revision']

    def bad_plan():
        raise RuntimeError('opaque backend exception')
    monkeypatch.setattr(video_service, '_plan', bad_plan)
    failed = video_service.build_issue(engine, day)
    assert failed['status'] == 'ready' and failed['build_status'] == 'failed'
    assert failed['items'] == original['items'] and failed['revision'] == original['revision']
    assert failed['error'] == 'RuntimeError'
    with Session(engine) as session:
        assert session.get(DailyDigestIssue, day.isoformat()) is None


def test_worker_on_demand_and_successive_beijing_day_close(engine, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_DIGEST_ENABLED', '1')
    monkeypatch.delenv('NEWS2DOUYIN_VIDEO_DIGEST_AUTOMATIC', raising=False)
    today = service.default_day()
    yesterday = service.last_closed_day()
    worker = video_service.VideoDigestWorker(engine, None)
    with Session(engine) as session:
        assert worker._due(session, datetime.now(service.BEIJING)) is None
        session.add(DailyDigestIssue(day=today.isoformat(), status='pending', requested=True))
        session.commit()
        assert worker._due(session, datetime.now(service.BEIJING)) is None
    assert video_service.request_issue(engine, today)['build_status'] == 'pending'
    with Session(engine) as session:
        assert worker._due(session, datetime.now(service.BEIJING)) == today
        row = session.get(VideoDigestIssue, today.isoformat())
        row.status = 'ready'
        session.add(VideoDigestIssue(day=yesterday.isoformat(), status='live'))
        session.commit()
        assert worker._due(session, datetime.now(service.BEIJING)) == yesterday


def test_video_story_uses_saved_cutoffs_even_for_retrospective_editions(engine, monkeypatch):
    from news2douyin.digest import video_story

    _sources(monkeypatch)
    calls = []
    monkeypatch.setattr(video_service, '_plan', lambda: _plan('video_story', {'story'}, []))
    def histories(items, editions, *, day, cutoff):
        calls.extend(editions)
        assert day == DAY and cutoff.isoformat() == '2026-04-08T09:20:11+00:00'
        items[0]['story_history'] = [{"day": calls[0]['day'], "key": "earlier"}]
    monkeypatch.setattr(video_story, 'attach_history', histories)

    def row(model, stamp, cutoff, state='ready'):
        return model(day=stamp, status=state, generated_at='2026-11-19T12:00:00Z',
                     stats_json=json.dumps({'publication': {'cutoff_at': cutoff}}),
                     items_json=json.dumps([{'key': 'earlier', 'title': 'Earlier port reopening'}]))
    with Session(engine) as session:
        session.add(row(VideoDigestIssue, '2026-04-07', '2026-04-07T16:00:00Z'))
        session.add(row(VideoDigestIssue, '2026-04-06', '2026-04-06T16:00:00Z', state='building'))
        session.add(row(VideoDigestIssue, '2026-04-09', '2026-04-09T16:00:00Z'))
        session.add(row(DailyDigestIssue, '2026-04-05', '2026-04-05T16:00:00Z'))
        session.commit()
    result = video_service.build_issue(engine, DAY, now=NOW)
    assert result['edition_kind'] == 'intraday' and result['cutoff_at'] == '2026-04-08T09:20:10Z'
    assert calls[0]['day'] == '2026-04-07' and len(calls) == 1
    assert calls[0]['cutoff_at'] == '2026-04-07T16:00:00Z'
    assert result['items'][0]['story_history'] == [{'day': '2026-04-07', 'key': 'earlier'}]


def test_postgres_video_snapshot_and_request_do_not_mutate_world(postgres_url, monkeypatch):
    from news2douyin.storage.db import init_db, make_engine

    _sources(monkeypatch)
    monkeypatch.setattr(video_service, '_plan', lambda: _plan('video_story', {'story'}, []))
    db = make_engine(postgres_url)
    try:
        init_db(db)
        with Session(db) as session:
            session.add(DailyDigestIssue(day=DAY.isoformat(), status='ready', items_json='[{"key":"world"}]'))
            session.commit()
        video_service.request_rebuild(db, DAY, require_worker=False)
        result = video_service.build_issue(db, DAY)
        assert result['status'] == 'ready' and result['requested']
        with Session(db) as session:
            assert video_service.read_issue(session, DAY)['items'][0]['key'] == 'story'
            assert service.read_issue(session, DAY)['items'] == [{'key': 'world'}]
    finally:
        db.dispose()
