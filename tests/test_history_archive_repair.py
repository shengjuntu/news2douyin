"""A completed job is a readable publication, including after replacement of storage."""
from collections import Counter
from datetime import timedelta

import pytest
from sqlmodel import Session, select

from news2douyin.history_archive import catalog, service
from news2douyin.history_archive.models import HistoryJob, HistoryPart
from news2douyin.history_archive.search import find_leads
from test_history_archive_integration import FixtureTransport, NEW, _drain, archive_app
from test_postgres_integration import postgres_url


def populate(app, monkeypatch):
    worker = app.state.history_archive
    transport = FixtureTransport(worker.engine)
    transport.add_day(NEW, mentions=False, extra_event=True, invalid=True)
    monkeypatch.setattr(catalog.requests, 'get', transport.get)
    monkeypatch.setattr(service, '_today', lambda: NEW)
    monkeypatch.setattr(service, 'PUBLISH_INTERVAL', 0)
    worker.start(NEW, NEW)
    _drain(worker)
    assert worker.status()['state'] == 'ready'
    return worker, transport


@pytest.mark.parametrize('damage', ('remove', 'overwrite', 'invalid_sidecar'))
def test_only_the_damaged_shard_is_rebuilt(archive_app, monkeypatch, damage):
    app, _, storage = archive_app
    worker, transport = populate(app, monkeypatch)
    before = worker.status()['progress']
    kind = 'gkg' if damage == 'invalid_sidecar' else 'events'
    pub = next(item['path'] for item in service.query_groups(worker.engine, NEW, NEW) if item['kind'] == kind)
    data = worker.root / pub
    target = data.with_name('invalid.jsonl.gz') if damage == 'invalid_sidecar' else data
    expected = data.read_bytes()
    if damage == 'overwrite':
        # Same file length and a valid Parquet footer. Cached integrity receipts
        # must still notice in-place data-page changes, before SQL runs.
        with data.open('r+b') as stream:
            stream.seek(10)
            old = stream.read(1)
            stream.seek(10)
            stream.write(bytes([old[0] ^ 255]))
    else:
        target.unlink()
    result = find_leads(worker.engine, storage, NEW, NEW + timedelta(days=1), ['canada'])
    assert result.coverage[kind]['state'] == ('gap' if kind == 'gkg' else 'partial')
    with Session(worker.engine) as session:
        part = session.exec(select(HistoryPart.key).where(HistoryPart.publication_path == pub)).one()
    worker.request_window(NEW, NEW)
    worker.pause()
    assert not worker.step() and len([n for n in transport.calls if n.endswith('.zip')]) == 3
    worker.resume()
    restarted = service.ArchiveWorker(worker.engine, storage)
    _drain(restarted)
    with Session(worker.engine) as session:
        assert all(job.state == 'ready' for job in session.exec(select(HistoryJob)))
    assert data.read_bytes() == expected and target.is_file()
    assert restarted.status()['progress'] == before
    before_repeat = Counter(transport.calls)
    assert before_repeat[part] == 2 and all(before_repeat[n] == 1 for n in transport.archives if n != part)
    restarted.request_window(NEW, NEW)
    _drain(restarted)
    assert Counter(transport.calls) == before_repeat
    assert find_leads(worker.engine, storage, NEW, NEW + timedelta(days=1), ['canada']).coverage[kind]['state'] == 'covered'


def test_missing_volume_waits_without_resetting_the_ledger(archive_app, monkeypatch):
    app, _, storage = archive_app
    worker, transport = populate(app, monkeypatch)
    before = worker.status()['progress']
    original = worker.root.with_name('temporarily-unmounted')
    worker.root.rename(original)
    worker.root.mkdir()
    calls = len(transport.calls)
    worker.request_window(NEW, NEW)
    restarted = service.ArchiveWorker(worker.engine, storage)
    assert not restarted.step()
    assert '挂载' in restarted.status()['error']
    assert restarted.status()['progress'] == before and len(transport.calls) == calls
    assert list(worker.root.iterdir()) == []
    worker.root.rmdir()
    original.rename(worker.root)
    _drain(restarted)
    assert not restarted.status()['error'] and len(transport.calls) == calls
    assert restarted.status()['progress'] == before


def test_first_upgrade_audit_verifies_legacy_outputs_and_persists_receipts(archive_app, monkeypatch):
    from sqlalchemy import delete
    from news2douyin.history_archive import convert
    from news2douyin.history_archive.models import HistoryPublicationCheck, HistoryVolume

    app, _, storage = archive_app
    worker, transport = populate(app, monkeypatch)
    # A real 0.55 ledger and its immutable outputs predate the volume marker
    # and receipt tables. init_db creates the two empty tables on upgrade.
    with Session(worker.engine) as session:
        session.execute(delete(HistoryVolume))
        session.execute(delete(HistoryPublicationCheck))
        session.commit()
    (worker.root / '.news2douyin-volume').unlink()
    hashed = []
    original = convert._source_hash
    def tracked(path, stop=None):
        hashed.append(path.name)
        return original(path, stop)
    monkeypatch.setattr(convert, '_source_hash', tracked)
    calls = list(transport.calls)
    for _ in range(2):
        restarted = service.ArchiveWorker(worker.engine, storage)
        restarted.request_window(NEW, NEW)
        _drain(restarted)
        assert Counter(hashed) == {'data.parquet': 3, 'invalid.jsonl.gz': 1}
        assert list(transport.calls) == calls
