from datetime import date, datetime, timedelta, timezone
from threading import Event, Thread

from sqlalchemy import event
from sqlmodel import Session

from news2douyin.digest import service
from news2douyin.digest.models import DailyDigestIssue
from news2douyin.digest.runtime import due_day
from news2douyin.intake.lake import Lake
from news2douyin.storage.models import ArticleEventLink
from test_digest_sources import radar, version
from test_postgres_integration import postgres_url


def _exercise_day(engine, root):
    """Use both real input streams and check that the cutoffs include the same second."""
    first = datetime(2025, 4, 9, 3, 0, tzinfo=timezone.utc)
    second = first + timedelta(hours=2)
    day = service.default_day(first)
    a, b = (t.strftime('%Y-%m-%dT%H:%M:%SZ') for t in (first, second))
    with Session(engine) as session:
        session.add_all([radar('s1'), radar('s2'), ArticleEventLink(article_key='a', event_key='s1'),
                         ArticleEventLink(article_key='b', event_key='s2'),
                         version('a', 1, a, 'Canada approves rail expansion', 'https://rail.example/a'),
                         version('b', 1, b, 'Argentina overhauls its tax code', 'https://tax.example/b')])
        session.commit()
    with Lake(root).connection(create=True) as lake:
        for timestamp, tick, title, url in (
                ('20250409030000', a, '德国议会公布新的汽车排放标准', 'https://motor.example/car'),
                ('20250409050000', b, '印度完成首次水下考古发掘', 'https://diving.example/archaeology')):
            lake.execute("INSERT INTO intake_archives (feed,stamp,rows,completed_at) VALUES ('gkg_english',?,1,?)",
                         [timestamp, tick])
            lake.execute("INSERT INTO intake_records_v2 (feed,stamp,row_index,title,url,valid) VALUES ('gkg_english',?,1,?,?,true)",
                         [timestamp, title, url])
    early = service.build_issue(engine, day, storage_root=root, now=first)
    assert early['edition_kind'] == 'intraday' and early['revision'] == 1 and early['cutoff_at'] == a
    assert {card['title'] for card in early['items']} == {'Canada approves rail expansion', '德国议会公布新的汽车排放标准'}
    assert early['stats']['sources']['gkg_seen'] == early['stats']['sources']['local_seen'] == 1
    with Session(engine) as session:
        # The calendar also offers a previous-day catch-up once today's job finishes;
        # it must never pick today early, and must pick it when the interval elapses.
        assert due_day(session, first + timedelta(minutes=14)) != day
        assert due_day(session, first + timedelta(minutes=15)) == day
    refreshed = service.build_issue(engine, day, storage_root=root, now=second)
    assert refreshed['build_status'] == 'idle' and refreshed['revision'] == 2 and refreshed['cutoff_at'] == b
    assert len(refreshed['items']) == 4
    assert refreshed['stats']['sources']['gkg_seen'] == refreshed['stats']['sources']['local_seen'] == 2
    # A delayed request with an earlier snapshot boundary cannot replace this edition.
    assert service.build_issue(engine, day, now=first) == refreshed
    # A stale scheduler result cannot claim today's just-finished refresh twice.
    assert service.build_issue(engine, day, now=second, only_due=True) == refreshed
    midnight = datetime(2025, 4, 9, 16, 0, tzinfo=timezone.utc)
    with Session(engine) as session:
        assert due_day(session, midnight) == day
    final = service.build_issue(engine, day, storage_root=root, now=midnight, only_due=True)
    assert final['edition_kind'] == 'closed' and final['revision'] == 3
    assert final['cutoff_at'] == '2025-04-09T16:00:00Z' and final['items'] == refreshed['items']
    with Session(engine) as session:
        assert due_day(session, midnight) == day + timedelta(days=1)
    assert service.build_issue(engine, day, now=midnight) == final
    assert service.build_issue(engine, day, now=second) == final
    assert service.request_issue(engine, day) == final


def test_intraday_actual_sql_duckdb_refresh_and_beijing_midnight(engine, tmp_path):
    _exercise_day(engine, tmp_path)


def test_intraday_actual_postgres_and_duckdb(postgres_url, tmp_path):
    from news2douyin.storage.db import init_db, make_engine
    engine = make_engine(postgres_url)
    try:
        init_db(engine)
        _exercise_day(engine, tmp_path)
    finally:
        engine.dispose()


def test_refresh_failure_keeps_legacy_snapshot_and_history_keys(engine, monkeypatch):
    import json
    from news2douyin.digest import geography
    from news2douyin.digest.ranking import card_progress_key

    day = date(2021, 10, 9)
    cards = [{'title':'已有选题标题', 'key':'prev', 'source_url':'https://archive.example/a'}]
    with Session(engine) as session:
        session.add(DailyDigestIssue(day=day.isoformat(), status='ready', items_json=json.dumps(cards),
                                    keys_json='["prev"]', stats_json='{"sources":{}}', generated_at='recorded'))
        session.commit()
        frozen = service.read_issue(session, day)
    assert frozen['revision'] == 1
    assert frozen['edition_kind'] == 'closed' and frozen['cutoff_at'] == ''
    assert service.request_issue(engine, day) == frozen
    queued = service.request_rebuild(engine, day)
    assert queued['status'] == 'ready' and queued['build_status'] == 'pending' and queued['items'] == cards

    entered, release = Event(), Event()
    def fail(_items, *, storage_root):
        entered.set()
        assert release.wait(5)
        raise OSError('sensitive backend message')

    monkeypatch.setattr(geography, 'enrich_selected', fail)
    thread = Thread(target=service.build_issue, args=(engine, day))
    thread.start()
    try:
        assert entered.wait(5)
        with Session(engine) as session:
            active = service.read_issue(session, day)
            assert active['status'] == 'ready' and active['build_status'] == 'building'
            assert active['items'] == cards and active['revision'] == 1
            assert {'prev', card_progress_key(cards[0])} <= service.prior_keys(session, day + timedelta(days=1))
        assert service.build_issue(engine, day) == active
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive()
    with Session(engine) as session:
        failed = service.read_issue(session, day)
    assert failed['status'] == 'ready' and failed['build_status'] == 'failed'
    assert failed['error'] == 'OSError' and 'sensitive' not in str(failed)
    for key in ('items', 'stats', 'revision', 'generated_at', 'cutoff_at'):
        assert failed[key] == frozen[key]
    assert service.request_rebuild(engine, day)['build_status'] == 'pending'
    monkeypatch.setattr(geography, 'enrich_selected', lambda *_args, **_kw: None)
    done = service.build_issue(engine, day)
    assert done['revision'] == 2 and done['build_status'] == 'idle' and done['error'] == ''


def test_live_requests_coalesce_and_explicit_repair_gets_one_followup(engine, monkeypatch):
    from news2douyin.digest import geography

    day = service.default_day()
    service.build_issue(engine, day)
    assert service.request_issue(engine, day)['build_status'] == 'pending'
    assert service.request_issue(engine, day)['revision'] == 1
    entered, resume = Event(), Event()
    def pause(_items, *, storage_root):
        entered.set()
        assert resume.wait(5)
    monkeypatch.setattr(geography, 'enrich_selected', pause)
    worker = Thread(target=service.build_issue, args=(engine, day))
    worker.start()
    try:
        assert entered.wait(5)
        for _ in range(2):
            assert service.request_issue(engine, day)['build_status'] == 'building'
        with Session(engine) as session:
            assert session.get(DailyDigestIssue, day.isoformat()).status == 'refreshing'
        for _ in range(2):
            assert service.request_rebuild(engine, day)['build_status'] == 'building'
        with Session(engine) as session:
            assert session.get(DailyDigestIssue, day.isoformat()).status == 'refreshing_again'
    finally:
        resume.set()
        worker.join(10)
    assert not worker.is_alive()
    with Session(engine) as session:
        first = service.read_issue(session, day)
    assert first['revision'] == 2 and first['build_status'] == 'pending'
    last = service.build_issue(engine, day, only_due=True)
    assert last['revision'] == 3 and last['build_status'] == 'idle'
    assert service.build_issue(engine, day, only_due=True) == last

    queries = []
    def record(_conn, _cursor, query, _params, _context, _multi):
        queries.append(query)
    event.listen(engine, 'before_cursor_execute', record)
    try:
        with Session(engine) as session:
            assert service.read_issue_state(session, day) == {k:v for k,v in last.items() if k not in ('items','stats')}
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    assert len(queries) == 1
    assert 'items_json' not in queries[0] and 'keys_json' not in queries[0]


def test_expired_refresh_cannot_overwrite_a_later_worker(engine, monkeypatch):
    from threading import current_thread
    from news2douyin.digest import geography

    day = service.default_day()
    original = service.build_issue(engine, day)
    assert original['revision'] == 1
    entered, resume = Event(), Event()
    def attach(items, *, storage_root):
        if current_thread() is paused:
            entered.set()
            assert resume.wait(5)
            key = 'expired'
        else:
            key = 'renewed'
        items.append(dict(key=key, title=key))
    monkeypatch.setattr(geography, 'enrich_selected', attach)
    paused = Thread(target=service.build_issue, args=(engine, day))
    paused.start()
    try:
        assert entered.wait(5)
        with Session(engine) as session:
            session.get(DailyDigestIssue, day.isoformat()).lease_until = 0
            session.commit()
        replacement = service.build_issue(engine, day, only_due=True)
        assert replacement['revision'] == 2 and replacement['items'][0]['key'] == 'renewed'
    finally:
        resume.set()
        paused.join(5)
    assert not paused.is_alive()
    with Session(engine) as session:
        assert service.read_issue(session, day) == replacement
