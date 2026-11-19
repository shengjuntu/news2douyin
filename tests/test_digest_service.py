from datetime import date, datetime, timedelta
from threading import Event, Thread
import time

import pytest
from sqlmodel import Session

from news2douyin.digest import service
from news2douyin.digest.models import DailyDigestIssue
from news2douyin.digest.runtime import due_day
from test_postgres_integration import postgres_url


@pytest.fixture
def simulated(monkeypatch):
    from types import SimpleNamespace
    from news2douyin.digest import focus_hints, selection, sources
    monkeypatch.setattr(focus_hints, 'annotate_focus', lambda stream, _matcher, **kwargs: stream)
    calls = []
    def read(session, day, *, stats, cutoff, checkpoint):
        calls.append(day)
        stats['local_seen'] = 2
        yield 'same-progress'
        yield day.isoformat()
    def rank(stream, *, prior_keys, day, cutoff):
        assert cutoff.tzinfo is not None
        keys = [key for key in stream if key not in prior_keys]
        return dict(items=[dict(key=key, title=key) for key in keys], stats=dict(unique=len(keys)))
    monkeypatch.setattr(sources, 'iter_candidates', read)
    monkeypatch.setattr(selection, 'load_selection', lambda: SimpleNamespace(limit=100, needs_coarse=False, select=rank))
    return calls


def test_publication_idempotency_and_history(engine, simulated):
    day = date(2020, 10, 5)
    with Session(engine) as session:
        assert service.read_issue(session, day)['status'] == 'pending'
        assert session.get(DailyDigestIssue, day.isoformat()) is None
    original = service.build_issue(engine, day)
    assert original['status'] == 'ready'
    assert original['stats']['sources'] == {'local_seen': 2, 'candidate_layer': 1, 'collected_emitted': 0}
    assert service.build_issue(engine, day) == original
    next_day = day + timedelta(days=1)
    followup = service.build_issue(engine, next_day)
    assert [item['key'] for item in followup['items']] == [next_day.isoformat()]
    assert simulated == [day, next_day]
    with Session(engine) as session:
        assert 'same-progress' not in service.prior_keys(session, day + timedelta(days=8))


def test_calendar_dates_and_scheduler_catchup(engine):
    day = date(2022, 8, 12)
    now = datetime.fromisoformat('2022-08-13T00:29:00+08:00')
    assert service.default_day(now) == day + timedelta(days=1)
    assert service.last_closed_day(now) == day
    with Session(engine) as session:
        assert due_day(session, now) == day + timedelta(days=1)
        session.add(DailyDigestIssue(day=service.default_day(now).isoformat(), status='live', next_retry=now.timestamp()+1000))
        session.commit()
        assert due_day(session, now) == day - timedelta(days=1)
        assert due_day(session, now + timedelta(minutes=1)) == day
        session.add(DailyDigestIssue(day='2022-05-01', status='ready'))
        session.commit()
        assert due_day(session, now + timedelta(minutes=1)) == day - timedelta(days=6)
    with pytest.raises(ValueError):
        service.build_issue(engine, service.default_day() + timedelta(days=1))


def test_concurrent_workers_and_atomic_failure(engine, simulated, monkeypatch):
    from news2douyin.digest import sources
    day = date(2020, 10, 5)
    entered, resume = Event(), Event()
    def broken(session, day, *, stats, cutoff, checkpoint):
        entered.set()
        assert resume.wait(5)
        yield 'partially-read'
        raise RuntimeError('private database query')
    original = sources.iter_candidates
    monkeypatch.setattr(sources, 'iter_candidates', broken)
    victim = Thread(target=service.build_issue, args=(engine, day))
    victim.start()
    try:
        assert entered.wait(5)
        second = service.build_issue(engine, day)
        assert second['status'] == 'building' and second['items'] == []
    finally:
        resume.set()
        victim.join(5)
    with Session(engine) as session:
        failed = service.read_issue(session, day)
        assert failed['status'] == 'failed' and failed['items'] == []
        assert failed['error'] == 'RuntimeError'
        assert 'private' not in str(failed)
    monkeypatch.setattr(sources, 'iter_candidates', original)
    assert service.build_issue(engine, day)['status'] == 'ready'


def test_expired_owner_and_cancelled_claim(engine, simulated):
    day = date(2021, 3, 2)
    with Session(engine) as session:
        session.add(DailyDigestIssue(day=day.isoformat(), status='building', lease_token='old', lease_until=0))
        session.commit()
    cancel = Event()
    cancel.set()
    assert service.build_issue(engine, day, stop_event=cancel)['status'] == 'pending'
    assert not service._change_owned(engine, day, 'old', status='ready')
    assert service.build_issue(engine, day)['status'] == 'ready'


def test_empty_latest_retry_preserves_nonempty_edition(engine, simulated, monkeypatch):
    from news2douyin.digest import sources
    day = service.last_closed_day()
    original = sources.iter_candidates
    monkeypatch.setattr(sources, 'iter_candidates', lambda *args, **kwargs: iter(()))
    assert service.build_issue(engine, day)['items'] == []
    monkeypatch.setattr(sources, 'iter_candidates', original)
    assert service.build_issue(engine, day)['items'] == []
    with Session(engine) as session:
        row = session.get(DailyDigestIssue, day.isoformat())
        assert row.next_retry > time.time()
        row.next_retry = time.time() - 1
        session.commit()
    assert service.build_issue(engine, day)['items']
    assert simulated == [day]


def test_postgres_publishes_shared_sql_and_archive_snapshot(postgres_url, tmp_path):
    from news2douyin.storage.db import init_db, make_engine
    engine = make_engine(postgres_url)
    try:
        init_db(engine)
        day = date(2022, 3, 12)
        built = service.build_issue(engine, day, storage_root=tmp_path)
        assert built['status'] == 'ready' and built['items'] == []
        assert built['stats']['sources']['gkg_archive_present'] is False
        assert built['stats']['sources']['local_seen'] == 0
        assert service.build_issue(engine, day, storage_root=tmp_path) == built
    finally:
        engine.dispose()


def test_manual_queue_is_cheap_idempotent_and_respects_current_issue(engine, simulated, monkeypatch):
    today = datetime.now(service.BEIJING)
    tick = today
    latest = service.default_day(tick)
    old = latest - timedelta(days=365)
    assert service.request_issue(engine, old) == service.request_issue(engine, old)
    assert not simulated
    with Session(engine) as session:
        assert service.read_issue(session, old)['requested']
        assert due_day(session, tick) == old
    ready = service.build_issue(engine, old)
    assert ready['status'] == 'ready' and ready['requested']
    with Session(engine) as session:
        assert due_day(session, tick) == latest
    monkeypatch.setenv('NEWS2DOUYIN_DIGEST_ENABLED', '0')
    assert service.request_issue(engine, old) == ready
    with pytest.raises(RuntimeError):
        service.request_issue(engine, old - timedelta(days=1))
    with pytest.raises(ValueError):
        service.request_issue(engine, latest + timedelta(days=1))


def test_old_requested_day_failure_retries_after_backoff(engine):
    tick = datetime.now(service.BEIJING).replace(hour=12, minute=0)
    latest = service.default_day(tick)
    old = latest - timedelta(days=365)
    with Session(engine) as session:
        session.add(DailyDigestIssue(day=latest.isoformat(), status='live',next_retry=tick.timestamp()+900))
        session.add(DailyDigestIssue(day=service.last_closed_day(tick).isoformat(), status='ready'))
        session.add(DailyDigestIssue(day=old.isoformat(), status='failed', requested=True,
                                     next_retry=tick.timestamp() + 60))
        session.commit()
        assert due_day(session, tick) is None
        assert due_day(session, tick + timedelta(minutes=2)) == old


def test_publishes_configured_external_strategy_and_keeps_snapshot_if_rebuild_fails(engine, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from news2douyin.digest import archive, selection
    from news2douyin.digest.brief_export import markdown_for_shortlist
    from news2douyin.digest.ranking import Candidate

    day = date(2020, 10, 5)
    config = tmp_path/'selection.toml'
    config.write_text('[selection]\nstrategy="raw_archive_order"\nlimit=512\n[selection.options]\nskip=2\n')
    monkeypatch.setenv(selection.CONFIG_ENV, str(config))
    loaded = []

    class ReverseArchive:
        api_version = 1
        version = '2.1'
        needs_coarse = True

        def __init__(self, options):
            loaded.append(options)
            self.skip = options['skip']

        def select(self, candidates, context):
            assert context.day == day and context.cutoff.isoformat() == '2020-10-05T16:00:00+00:00'
            assert context.limit == 512 and not context.prior_keys
            rows = list(reversed(list(candidates)))[self.skip:]
            for row in rows[:context.limit]:
                yield selection.SelectionPick(row)

    entry = SimpleNamespace(name='raw_archive_order', load=lambda: ReverseArchive)
    monkeypatch.setattr(selection, '_plugins', lambda: [entry])

    def archive_rows(storage_root, day, *, stats, cutoff, checkpoint, coarse):
        assert coarse is True
        for n in range(516):
            yield Candidate(f'row-{n}', 'Regional investment announcement', '2020-10-05T10:00:00Z',
                            f'https://regional.example/report/{n}', 'gkg')
        stats['gkg_emitted'] = 516

    monkeypatch.setattr(archive, 'iter_gkg', archive_rows)
    published = service.build_issue(engine, day, storage_root=tmp_path)
    assert published['status'] == 'ready' and published['revision'] == 1
    assert loaded == [{'skip': 2}]
    assert published['stats']['scanned'] == published['stats']['sources']['gkg_emitted'] == 516
    assert published['stats']['selection'] == dict(strategy='raw_archive_order', version='2.1', limit=512,
                                                   api_version=1, options={'skip': 2})
    assert [c['key'] for c in published['items']] == [f'row-{n}' for n in range(513, 1, -1)]
    with Session(engine) as session:
        markdown = markdown_for_shortlist(session, service.read_issue(session, day))
    assert '完整导出本期 512 条' in markdown and '## 512.' in markdown

    config.write_text('[selection]\nstrategy="unknown_strategy"\n')
    service.request_rebuild(engine, day, require_worker=False)
    failed = service.build_issue(engine, day, storage_root=tmp_path)
    assert failed['status'] == 'ready' and failed['build_status'] == 'failed'
    assert failed['revision'] == 1 and failed['items'] == published['items'] and failed['stats'] == published['stats']
