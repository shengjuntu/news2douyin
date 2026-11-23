"""Slow origins keep work resumable and leave already-published data queryable."""
from collections import Counter
from datetime import timedelta

import pytest
from requests.exceptions import ChunkedEncodingError, ConnectTimeout
from sqlmodel import Session

from news2douyin.history_archive import catalog, service
from news2douyin.history_archive.models import HistoryGroup, HistoryPart
from news2douyin.history_archive.search import find_leads
from test_history_archive_integration import BASE, FixtureTransport, NEW, archive_app
from test_postgres_integration import postgres_url


def _start(client):
    day = NEW.isoformat()
    assert client.post(BASE + '/start', json={'date_from': day, 'date_to': day}).status_code == 202


@pytest.mark.parametrize('failure', ('connection_reset', 'http_503'))
def test_transient_zip_failure_publishes_verified_prefix_and_retries(archive_app, monkeypatch, failure):
    app, client, storage = archive_app
    worker = app.state.history_archive
    fixture = FixtureTransport(app.state.engine)
    fixture.add_day(NEW, mentions=False, extra_event=True)
    clock = [1000.]
    waiting = NEW.strftime('%Y%m%d') + '000000.export.CSV.zip'
    completed = NEW.strftime('%Y%m%d') + '001500.export.CSV.zip'
    expected_size = len(fixture.archives[waiting])
    sent = []

    class SlowBody(FixtureTransport.Response):
        def iter_content(self, _size):
            yield self.body[:43]
            current = client.get(BASE).json()['current']
            assert current['filename'] == waiting and current['stamp'] == waiting[:14]
            assert current['received_bytes'] == 43 and current['expected_bytes'] == expected_size
            raise ChunkedEncodingError('download connection reset')

    def origin(url, **kwargs):
        name = url.rsplit('/', 1)[-1]
        if name == waiting:
            sent.append(kwargs['headers'])
            if len(sent) == 1:
                fixture.calls.append(name)
                if failure == 'http_503':
                    response = fixture.Response(b'', 503)
                    response.headers['Retry-After'] = '90'
                    return response
                return SlowBody(fixture.archives[name])
        return fixture.get(url, **kwargs)

    monkeypatch.setattr(catalog.requests, 'get', origin)
    monkeypatch.setattr(service, 'time', lambda: clock[0])
    _start(client)
    assert worker.step()  # plan
    assert not worker.step()  # download newest, then yield before the next request
    delayed = client.get(BASE).json()
    assert delayed['retry_scope'] == 'group'
    assert delayed['retry_at'] == (1090 if failure == 'http_503' else 1060)
    assert delayed['progress']['ready_files'] == 1 and delayed['progress']['errors'] == 0
    assert delayed['current']['filename'] == waiting
    if failure == 'connection_reset':
        assert delayed['current']['received_bytes'] == 43
    groups = service.query_groups(app.state.engine, NEW, NEW)
    assert len(groups) == 1 and groups[0]['state'] == 'pending'

    # A different dataset proceeds while this day's Events wait.
    assert worker.step()
    assert fixture.calls[-1].endswith('.gkg.csv.zip')
    assert not worker.step()
    with Session(app.state.engine) as session:
        assert session.get(HistoryPart, waiting).state == 'pending'
        assert session.get(HistoryPart, completed).state == 'ready'

    restarted = service.ArchiveWorker(app.state.engine, storage)
    assert not restarted.step()
    clock[0] = delayed['retry_at']
    assert restarted.step()
    assert Counter(fixture.calls)[completed] == 1
    assert Counter(fixture.calls)[waiting] == 2
    assert restarted.status()['progress']['ready_files'] == 3
    assert restarted.status()['retry_at'] is None
    if failure == 'connection_reset':
        assert sent[1]['Range'] == 'bytes=43-'
    matches = find_leads(app.state.engine, str(storage), NEW, NEW + timedelta(days=1), ['canada'])
    assert len(matches.rows) == 3


def test_catalog_timeout_preserves_plan_until_restart_retry(archive_app, monkeypatch):
    app, client, storage = archive_app
    worker = app.state.history_archive
    fixture = FixtureTransport(app.state.engine)
    fixture.add_day(NEW, mentions=False)
    clock = [1000.]
    calls = []

    def origin(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise ConnectTimeout('catalog connection slow')
        return fixture.get(url, **kwargs)

    monkeypatch.setattr(catalog.requests, 'get', origin)
    monkeypatch.setattr(service, 'time', lambda: clock[0])
    _start(client)
    assert not worker.step()
    state = worker.status()
    assert (state['state'], state['retry_scope'], state['retry_at']) == ('planning', 'catalog', 1060.)
    assert not state['error'] and state['progress']['errors'] == 0
    restarted = service.ArchiveWorker(app.state.engine, storage)
    app.state.history_archive = restarted
    assert not restarted.step() and len(calls) == 1
    assert client.post(BASE + '/retry').status_code == 202
    assert restarted.step() and len(calls) == 2
    assert restarted.status()['retry_at'] is None


def test_slow_successful_group_publishes_finished_files_at_checkpoint(archive_app, monkeypatch):
    app, client, _ = archive_app
    worker = app.state.history_archive
    fixture = FixtureTransport(app.state.engine)
    fixture.add_day(NEW, mentions=False, extra_event=True)
    monkeypatch.setattr(catalog.requests, 'get', fixture.get)
    monkeypatch.setattr(service, 'PUBLISH_INTERVAL', 0)
    _start(client)
    assert worker.step()
    # Reproduce verified ZIPs left by a worker that stopped before publication.
    (worker.root / 'staging').mkdir()
    for name, payload in fixture.archives.items():
        if name.endswith('.export.CSV.zip'):
            (worker.root / 'staging' / name).write_bytes(payload)
            worker._record_part(name, 'downloaded')
    assert worker.step()
    now = worker.status()
    assert now['retry_at'] is None and now['progress']['ready_files'] == 1
    with Session(app.state.engine) as session:
        day = session.get(HistoryGroup, '2/events/20220223')
        assert day.state == 'pending'
        assert day.failed_files == 0
    assert worker.step()
    assert len(service.query_groups(app.state.engine, NEW, NEW)) == 2
    assert fixture.calls == ['masterfilelist.txt']
