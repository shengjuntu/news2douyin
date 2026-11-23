"""Offline archive journey through real HTTP routes, ledger, worker and Parquet.

Only the external HTTP transport is a fixture. Catalog parsing, checksum checks,
conversion, process recovery, SQL publication and historical queries are real.
Run with run_postgres_test_server.py to repeat both journeys on PostgreSQL.
"""
from collections import Counter
from contextlib import closing
from datetime import date, datetime, timezone
from hashlib import md5
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import duckdb
from fastapi.testclient import TestClient
import pytest
from sqlmodel import Session, select

from news2douyin.history_archive import catalog, convert, service
from news2douyin.history_archive.models import HistoryGroup, HistoryPart, HistoryPublication
from news2douyin.history_archive.search import find_leads
from news2douyin.server.app import create_app
from test_postgres_integration import postgres_url


BASE = '/api/history-archive'
OLD, NEW = date(2022, 2, 21), date(2022, 2, 23)


@pytest.fixture(params=('sqlite', 'postgres'))
def archive_app(request, tmp_path):
    url = (request.getfixturevalue('postgres_url') if request.param == 'postgres'
           else f'sqlite:///{tmp_path / "history.db"}')
    app = create_app(db_url=url, storage_root=str(tmp_path))
    # Do not enter TestClient's lifespan: all background ticks are explicit.
    with closing(TestClient(app)) as client:
        yield app, client, tmp_path
    app.state.engine.dispose()


def _zip(name, body):
    output = BytesIO()
    with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
        archive.writestr(name[:-4], body)
    return output.getvalue()


def _line(kind, values):
    return ('\t'.join(values.get(column, '') for column in convert.source_columns(kind, 2)) + '\n').encode()


class FixtureTransport:
    """Small official-shaped manifest and ZIPs; no external requests allowed."""

    class Response:
        def __init__(self, body, status=200):
            self.body, self.status_code, self.headers = body, status, {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def iter_content(self, _size):
            # Exercise the streaming path across multiple writes.
            for offset in range(0, len(self.body), 43):
                yield self.body[offset:offset + 43]

    def __init__(self, engine):
        self.engine = engine
        self.archives, self.calls, self.fail_once = {}, [], set()

    def add_day(self, day, *, mentions=True, extra_event=False, invalid=False):
        prefix = day.strftime('%Y%m%d')
        suffixes = {'events': '.export.CSV.zip', 'gkg': '.gkg.csv.zip', 'mentions': '.mentions.CSV.zip'}
        event_id = prefix + '1'
        kinds = ('events', 'gkg', 'mentions') if mentions else ('events', 'gkg')
        for kind in kinds:
            values = {
                'events': dict(global_event_id=event_id, event_date=prefix,
                               actor1_name='Canada', actor2_name='France',
                               actor1_country_code='CAN', actor2_country_code='FRA',
                               event_code='042', date_added=prefix + '000000',
                               source_url=f'https://event.test/{prefix}'),
                'mentions': dict(global_event_id=event_id, event_time_date=prefix + '000000',
                                 mention_time_date=prefix + '001500', mention_type='1',
                                 mention_identifier=f'https://mention.test/{prefix}', confidence='95'),
                'gkg': dict(gkg_record_id=prefix + '000000-1', document_date=prefix + '000000',
                            document_identifier=f'https://gkg.test/{prefix}', persons='Canada'),
            }[kind]
            body = _line(kind, values)
            if invalid and kind == 'gkg':
                body += b'broken\trow\n'
            name = prefix + '000000' + suffixes[kind]
            self.archives[name] = _zip(name, body)
            if extra_event and kind == 'events':
                name = prefix + '001500' + suffixes[kind]
                self.archives[name] = _zip(name, _line(kind, {
                    **values, 'global_event_id': prefix + '2', 'date_added': prefix + '001500',
                    'source_url': f'https://event-second.test/{prefix}',
                }))

    def get(self, url, **kwargs):
        assert self.engine.pool.checkedout() == 0, 'network must not hold a SQL connection'
        assert kwargs['allow_redirects'] is False
        assert url.startswith(catalog.HOST + '/gdeltv2/'), 'unexpected network request'
        name = url.rsplit('/', 1)[-1]
        self.calls.append(name)
        if name == 'masterfilelist.txt':
            # Deliberately oldest first, with a repeated row. The real catalog
            # must sort/deduplicate independently of the upstream list order.
            names = sorted(self.archives)
            lines = [f'{len(self.archives[n])} {md5(self.archives[n]).hexdigest()} '
                     f'http://data.gdeltproject.org/gdeltv2/{n}\n' for n in names]
            return self.Response((''.join(lines + lines[:1])).encode())
        assert name in self.archives
        if name in self.fail_once:
            self.fail_once.remove(name)
            return self.Response(b'not found', 404)
        return self.Response(self.archives[name])


def _one_boot_tick(worker):
    """Run actual ownership/recovery startup without a background thread/sleep."""
    worker.stop_event.clear()
    original = worker.step

    def once():
        try:
            return original()
        finally:
            worker.stop_event.set()

    worker.step = once
    try:
        worker._loop()
    finally:
        worker.step = original


def _drain(worker, maximum=20):
    for _ in range(maximum):
        if not worker.step():
            return
    pytest.fail('fixture archive work did not finish within its bounded tick budget')


def test_archive_newest_first_crash_resume_and_committed_historical_query(archive_app, monkeypatch):
    app, client, root = archive_app
    engine, worker = app.state.engine, app.state.history_archive
    transport = FixtureTransport(engine)
    transport.add_day(OLD)
    transport.add_day(NEW, extra_event=True, invalid=True)
    monkeypatch.setattr(catalog.requests, 'get', transport.get)
    real_convert, conversions = convert.convert_group, []

    class ProcessCrash(BaseException):
        pass

    def crash_after_files(*args, **kwargs):
        assert engine.pool.checkedout() == 0, 'conversion must not hold a SQL connection'
        result = real_convert(*args, **kwargs)
        conversions.append(result)
        raise ProcessCrash()

    payload = dict(date_from=OLD.isoformat(), date_to=NEW.isoformat(), mentions=True)
    assert client.post(BASE + '/start', json=payload).status_code == 202
    assert worker.step(), 'planning must parse the fixture manifest'
    status = client.get(BASE + '?limit=2&offset=2').json()
    assert status['coverage_total'] == 9 and len(status['coverage']) == 2
    assert status['progress']['total_files'] == 7, 'duplicate manifest entry must not create another part'
    with Session(engine) as session:
        gaps = session.exec(select(HistoryGroup).where(HistoryGroup.period == '20220222')).all()
        assert len(gaps) == 3
        assert all(g.state == 'missing' and g.archive_missing == 96 and not g.total_files for g in gaps)

    monkeypatch.setattr(convert, 'convert_group', crash_after_files)
    with pytest.raises(ProcessCrash):
        worker.step()
    assert transport.calls[1:] == ['20220223001500.export.CSV.zip', '20220223000000.export.CSV.zip']
    assert conversions[0].parquet.is_file()
    assert service.query_groups(engine, OLD, NEW) == [], 'orphan files must not be query-visible before SQL commit'
    assert find_leads(engine, root, OLD, date(2022, 2, 24), ['canada']).rows == []

    # Persisted pause must survive a new process owner recovering a running
    # group; validated ZIPs and unpublished Parquet remain available for replay.
    assert client.post(BASE + '/pause').status_code == 202
    restarted = service.ArchiveWorker(engine, root)
    app.state.history_archive = restarted
    monkeypatch.setattr(convert, 'convert_group', real_convert)
    _one_boot_tick(restarted)
    assert client.get(BASE).json()['state'] == 'paused'
    assert service.query_groups(engine, OLD, NEW) == []
    before = list(transport.calls)
    assert client.post(BASE + '/resume').status_code == 202
    _one_boot_tick(restarted)
    assert transport.calls == before, 'verified staged ZIPs must be reused after restart'
    assert len(service.query_groups(engine, OLD, NEW)) == 1
    assert not list((restarted.root / 'staging').glob('*.zip'))
    restarted.stop_event.clear()
    _drain(restarted)

    downloads = [name for name in transport.calls if name.endswith('.zip')]
    assert [name[:8] for name in downloads] == ['20220223'] * 4 + ['20220221'] * 3
    status = client.get(BASE + '?limit=120').json()
    assert status['state'] == 'partial' and status['through'] is None
    assert status['progress']['ready_files'] == 7 and status['progress']['errors'] == 0
    assert sum(item['invalid_rows'] for item in status['coverage']) == 1
    with Session(engine) as session:
        publications = session.exec(select(HistoryPublication)).all()
        parts = session.exec(select(HistoryPart)).all()
        assert len(publications) == 6 and len(parts) == 7
        assert all(part.state == 'ready' for part in parts)
        rows = 0
        with duckdb.connect(':memory:') as connection:
            for publication in publications:
                path = restarted.root / publication.path
                rows += connection.execute('SELECT count(*) FROM read_parquet(?)', [str(path)]).fetchone()[0]
        assert rows == 7

    result = find_leads(engine, root, NEW, date(2022, 2, 24), ['canada'],
                        cutoff=datetime(2022, 2, 23, 1, tzinfo=timezone.utc))
    assert {row['url'] for row in result.rows} == {
        'https://event.test/20220223', 'https://event-second.test/20220223',
        'https://mention.test/20220223', 'https://gkg.test/20220223',
    }
    assert all(item['state'] == 'partial' and item['expected_days'] == 1 for item in result.coverage.values())

    # UI/status only inspect persisted bookkeeping, never convert/query files
    # or fetch from an upstream provider during a read request.
    def forbidden(*_args, **_kwargs):
        raise AssertionError('history status GET attempted external/archive work')

    monkeypatch.setattr(catalog.requests, 'get', forbidden)
    monkeypatch.setattr(duckdb, 'connect', forbidden)
    assert client.get(BASE).status_code == 200
    assert client.get('/admin/history-archive').status_code == 200
    assert client.get(BASE + '?limit=121').status_code == 422
    assert client.get(BASE + '?offset=-1').status_code == 422


def test_archive_failed_source_retry_preserves_published_parts(archive_app, monkeypatch):
    app, client, root = archive_app
    engine, worker = app.state.engine, app.state.history_archive
    transport = FixtureTransport(engine)
    transport.add_day(OLD, mentions=False)
    transport.add_day(NEW, mentions=False)
    failed = '20220223000000.gkg.csv.zip'
    transport.fail_once.add(failed)
    monkeypatch.setattr(catalog.requests, 'get', transport.get)
    assert client.post(BASE + '/start', json=dict(date_from=OLD.isoformat(), date_to=NEW.isoformat())).status_code == 202
    _drain(worker)
    first = client.get(BASE).json()
    assert first['state'] == 'partial'
    assert first['progress']['ready_files'] == 3 and first['progress']['errors'] == 1
    assert any(item['kind'] == 'gkg' and item['date_to'] == NEW.isoformat() for item in first['failures'])
    committed = {item['path'] for item in service.query_groups(engine, OLD, NEW)}
    assert len(committed) == 3

    # Reopen from the persisted ledger, retry the failing source, and ensure
    # already-ready groups are not downloaded or credited a second time.
    restarted = service.ArchiveWorker(engine, root)
    app.state.history_archive = restarted
    assert client.post(BASE + '/retry').status_code == 202
    _drain(restarted)
    final = client.get(BASE).json()
    assert final['state'] == 'partial', 'fixture manifest still intentionally lacks the middle day/remaining slots'
    assert final['progress']['ready_files'] == 4 and final['progress']['errors'] == 0
    assert final['progress']['downloaded_files'] == 4
    calls = Counter(name for name in transport.calls if name.endswith('.zip'))
    assert calls[failed] == 2 and all(count == 1 for name, count in calls.items() if name != failed)
    assert len(service.query_groups(engine, OLD, NEW)) == 4
    assert committed.issubset({item['path'] for item in service.query_groups(engine, OLD, NEW)})
    assert final['progress']['ready_bytes'] == sum(map(len, transport.archives.values()))

