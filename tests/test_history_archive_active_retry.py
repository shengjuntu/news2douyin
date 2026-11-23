"""Retry requests are durable even while the worker owns a group's ZIPs."""
from collections import Counter

from sqlmodel import Session

from news2douyin.history_archive import catalog
from news2douyin.history_archive.models import HistoryGroup, HistoryPart
from test_history_archive_integration import BASE, NEW, FixtureTransport, _drain, archive_app
from test_postgres_integration import postgres_url


def test_retry_during_a_group_repairs_the_first_failed_zip(archive_app, monkeypatch):
    app, client, _ = archive_app
    engine, worker = app.state.engine, app.state.history_archive
    transport = FixtureTransport(engine)
    transport.add_day(NEW, mentions=False, extra_event=True)
    first = '20220223001500.export.CSV.zip'
    second = '20220223000000.export.CSV.zip'
    transport.fail_once.add(first)
    sent_retry = False

    def get(url, **kwargs):
        nonlocal sent_retry
        if url.endswith(second) and not sent_retry:
            # First archive already failed. The second request is still in the
            # same worker-owned group while the status/retry routes execute.
            assert client.get(BASE).json()['progress']['errors'] == 1
            assert client.post(BASE + '/retry').status_code == 202
            with Session(engine) as session:
                group = session.get(HistoryGroup, '2/events/20220223')
                assert (group.state, group.phase) == ('running', 'downloading')
            sent_retry = True
        return transport.get(url, **kwargs)

    monkeypatch.setattr(catalog.requests, 'get', get)
    assert client.post(BASE + '/start', json={
        'date_from': NEW.isoformat(), 'date_to': NEW.isoformat(),
    }).status_code == 202
    assert worker.step(), 'plan'
    assert worker.step(), 'finish the original in-flight group'
    with Session(engine) as session:
        assert session.get(HistoryGroup, '2/events/20220223').state == 'pending'
        assert session.get(HistoryPart, first).state == 'pending'
        assert session.get(HistoryPart, second).state == 'ready'
    _drain(worker)
    status = client.get(BASE).json()
    assert sent_retry and status['progress']['ready_files'] == status['progress']['total_files'] == 3
    assert status['progress']['errors'] == 0
    assert Counter(transport.calls)[first] == 2
    assert Counter(transport.calls)[second] == 1


def test_retry_recovers_a_stranded_pending_zip_from_an_older_release(archive_app, monkeypatch):
    app, client, _ = archive_app
    engine, worker = app.state.engine, app.state.history_archive
    transport = FixtureTransport(engine)
    transport.add_day(NEW, mentions=False, extra_event=True)
    staged = '20220223001500.export.CSV.zip'
    transport.fail_once.add(staged)
    monkeypatch.setattr(catalog.requests, 'get', transport.get)
    assert client.post(BASE + '/start', json={
        'date_from': NEW.isoformat(), 'date_to': NEW.isoformat(),
    }).status_code == 202
    _drain(worker)
    with Session(engine) as session:
        group = session.get(HistoryGroup, '2/events/20220223')
        part = session.get(HistoryPart, staged)
        assert group.state == 'partial' and part.state == 'failed'
        # v0.51 could store this combination upon an in-flight retry.
        group.failed_files = 0
        part.state, part.error = 'pending', ''
        session.add_all((group, part))
        session.commit()
    assert client.post(BASE + '/retry').status_code == 202
    _drain(worker)
    status = client.get(BASE).json()
    assert status['progress']['ready_files'] == status['progress']['total_files'] == 3
    assert Counter(transport.calls)[staged] == 2
