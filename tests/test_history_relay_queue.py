"""Publish trusted local transfers through the existing history ledger and search."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from hashlib import md5
import os
from zipfile import ZipFile
from threading import Barrier
from time import time

import pytest
from news2douyin.history_archive import catalog, service
from news2douyin.history_archive.models import HistoryProviderCooldown
from news2douyin.history_archive.search import find_leads
from test_history_archive_integration import FixtureTransport, NEW, OLD, archive_app
from test_postgres_integration import postgres_url


@pytest.fixture
def worker(archive_app, monkeypatch):
    app, _, storage = archive_app
    monkeypatch.setenv('NEWS2DOUYIN_HISTORY_ARCHIVE_SOURCE', 'relay')
    monkeypatch.setenv('NEWS2DOUYIN_HISTORY_RELAY_TOKEN', 'relay-fixture-only')
    monkeypatch.setattr(catalog.requests, 'get', lambda *a, **k: pytest.fail('unexpected server download'))
    return service.ArchiveWorker(app.state.engine, storage)


def prepare(worker, start=NEW, end=NEW, *, extra_event=False):
    fixture = FixtureTransport(worker.engine)
    for day in sorted({start, end}):
        fixture.add_day(day, mentions=False, extra_event=extra_event)
    worker.start(start, end)
    assert not worker.step()
    assert worker.relay_work()['catalogs'] == ['gdeltv2/masterfilelist.txt']
    listing = fixture.get(catalog.HOST + '/gdeltv2/masterfilelist.txt', allow_redirects=False)
    worker.catalog_dir.mkdir(parents=True)
    (worker.catalog_dir/'gdeltv2-masterfilelist.txt').write_bytes(listing.body)
    assert worker.step(), 'plan using uploaded catalog'
    return fixture.archives


def upload(worker, archives, entry, path):
    data = archives[entry['name']]
    path.write_bytes(data)
    return worker.relay_receive(entry['name'], path, len(data), md5(data).hexdigest())


def test_waiting_worker_accepts_then_publishes_in_date_order(worker, tmp_path):
    assert worker.status()['relay_last_poll_at'] is None
    archives = prepare(worker, OLD, NEW, extra_event=True)
    with worker._write() as session:
        session.add(HistoryProviderCooldown(provider='gdelt', resume_at=time()+300))
    status = worker.status()
    assert status['source'] == 'relay' and status['relay_token_configured'] and status['relay_last_poll_at']
    assert status['retry_at'] is None
    observed = []
    for _ in range(12):
        task = worker.relay_work(limit=2)
        assert not task['catalogs']
        if not task['packages']:
            if task['waiting'] == 'idle':
                break
            worker.step()
            continue
        for entry in task['packages']:
            observed.append(entry['name'][:8])
            expected = worker.relay_part(entry['name'])
            assert expected == {**entry, 'state': 'pending'}
            assert upload(worker, archives, entry, tmp_path/'send.zip')['state'] == 'staged'
        assert worker.step()
    assert observed == ['20220223']*3 + ['20220221']*3
    status = worker.status()
    assert status['progress']['ready_files'] == status['progress']['downloaded_files'] == 6
    assert not list((worker.root/'staging').glob('*.zip'))
    leads = find_leads(worker.engine, worker.root.parent, NEW, date(2022, 2, 24), ['canada'])
    assert {r['url'] for r in leads.rows} == {
        'https://event.test/20220223', 'https://event-second.test/20220223', 'https://gkg.test/20220223'}


def test_pause_duplicate_and_recovery_of_missing_staging(worker, tmp_path):
    archives = prepare(worker)
    [entry] = worker.relay_work()['packages']
    worker.pause()
    assert worker.relay_work()['paused']
    with pytest.raises(RuntimeError):
        upload(worker, archives, entry, tmp_path/'first')
    worker.resume()
    barrier = Barrier(2)
    def handoff(index):
        barrier.wait(5)
        return upload(worker, archives, entry, tmp_path/f'copy-{index}')
    with ThreadPoolExecutor(2) as pool:
        assert all(r['state']=='staged' for r in pool.map(handoff, range(2)))
    assert worker.status()['progress']['downloaded_files'] == 1
    (worker.root/'staging'/entry['name']).unlink()
    assert worker.step() and worker.relay_part(entry['name'])['state'] == 'pending'
    assert worker.status()['progress']['downloaded_files'] == 0
    upload(worker, archives, entry, tmp_path/'again')
    assert worker.relay_fail(entry['name'], 404)['state']=='staged'
    assert worker.step()
    assert upload(worker, archives, entry, tmp_path/'lost-ack')['state'] == 'ready'
    assert worker.relay_fail(entry['name'], 410)['state']=='ready'
    assert not (worker.root/'staging'/entry['name']).exists()


def test_failure_is_idempotent_and_other_files_can_publish(worker, tmp_path):
    archives = prepare(worker, extra_event=True)
    a, b = worker.relay_work()['packages']
    with pytest.raises(ValueError): worker.relay_fail(a['name'], 429)
    for _ in range(2): assert worker.relay_fail(a['name'], 404)['state'] == 'failed'
    with pytest.raises(RuntimeError): upload(worker, archives, a, tmp_path/'failed')
    assert worker.status()['progress']['errors'] == 1
    upload(worker, archives, b, tmp_path/'valid')
    assert worker.step()
    [gkg] = worker.relay_work()['packages']
    assert worker.relay_fail(gkg['name'], 410)['state']=='failed'
    assert worker.step()
    assert worker.status()['progress']['ready_files'] == 1
    worker.retry()
    assert worker.relay_part(a['name'])['state']=='pending'
    assert worker.relay_part(b['name'])['state']=='ready'
    with pytest.raises(KeyError): worker.relay_part('../fake.zip')
    with pytest.raises(ValueError): worker.relay_receive(a['name'], tmp_path/'unused', 0, 'not expected')


def test_legacy_and_expired_catalogs_are_requested_by_exact_range(tmp_path):
    old = date(1989, 1, 1)
    assert set(catalog.needed_relay_catalogs(old, old, tmp_path)) == {'events/md5sums', 'events/filesizes'}
    assert set(catalog.needed_relay_catalogs(old, NEW, tmp_path)) == set(catalog.RELAY_CATALOGS)
    recent = service._today()
    manifest = tmp_path/'gdeltv2-masterfilelist.txt'
    manifest.touch()
    assert catalog.needed_relay_catalogs(recent, recent, tmp_path) == []
    os.utime(manifest, (time()-7200, time()-7200))
    assert catalog.needed_relay_catalogs(recent, recent, tmp_path) == ['gdeltv2/masterfilelist.txt']


def test_partial_year_request_hands_off_one_original_legacy_package(worker, tmp_path):
    start = date(1979, 7, 2)
    worker.start(start, start, mentions=True)
    assert worker.relay_work()['catalogs'] == ['events/filesizes', 'events/md5sums']
    name = '1979.zip'
    path = tmp_path / name
    with ZipFile(path, 'w') as zf:
        zf.writestr('1979.csv', b'\t'.join([b'1979'] * 57) + b'\n')
    data = path.read_bytes()
    digest = md5(data).hexdigest()
    worker.catalog_dir.mkdir(parents=True)
    (worker.catalog_dir/'events-filesizes').write_text(f'{len(data)} {name}\n')
    (worker.catalog_dir/'events-md5sums').write_text(f'{digest} {name}\n')
    assert worker.step()
    [entry] = worker.relay_work()['packages']
    assert entry['name'] == name and entry['url'] == catalog.HOST + '/events/1979.zip'
    assert worker.relay_receive(name, path, len(data), digest)['state'] == 'staged'
    assert worker.step()
    worker.step()
    assert worker.relay_work()['waiting'] == 'idle'
    [group] = service.query_groups(worker.engine, start, start)
    assert group['state'] == 'ready' and group['kind'] == 'events' and group['version'] == 1
    assert (worker.root / group['path']).is_file()
