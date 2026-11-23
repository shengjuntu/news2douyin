"""The relay accepts only authenticated, complete official files from its queue."""
from hashlib import md5
from threading import Event
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect
import pytest

from news2douyin.server import history_relay as api

BASE = '/api/history-archive/relay'
AUTH = {'Authorization': 'Bearer local-test-token'}
NAME = '20220222120000.export.CSV.zip'
BODY = b'zip-fixture'
DIGEST = md5(BODY, usedforsecurity=False).hexdigest()
LINE = f'{len(BODY)} {DIGEST} http://data.gdeltproject.org/gdeltv2/{NAME}\n'.encode()


class Worker:
    archive_source = 'relay'

    def __init__(self, root):
        self.root = root
        self.wake_event = Event()
        self.state = 'pending'
        self.received = []

    def relay_work(self, *, limit):
        return {'catalogs': [], 'packages': [], 'paused': False, 'waiting': '', 'requested': limit}

    def relay_part(self, name):
        if name != NAME:
            raise KeyError
        return dict(state=self.state, size=len(BODY), md5=DIGEST)

    def relay_receive(self, name, path, size, digest):
        self.received.append((name, path.read_bytes(), size, digest))
        self.state = 'downloaded'
        return dict(name=name, state='staged')

    def relay_fail(self, name, status):
        if name != NAME:
            raise KeyError
        if self.state == 'pending':
            self.state = 'failed'
        return dict(name=name, state=self.state)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    app = FastAPI()
    api.register_relay(app)
    monkeypatch.setenv('NEWS2DOUYIN_HISTORY_RELAY_TOKEN', 'local-test-token')
    worker = Worker(tmp_path)
    app.state.history_archive = worker
    with TestClient(app) as client:
        yield client, worker


def test_gate_and_work(setup, monkeypatch):
    client, worker = setup
    for path, body in [('/work', None), ('/catalog/events/filesizes', b'file'), (f'/packages/{NAME}', BODY)]:
        r = client.get(BASE + path) if body is None else client.put(BASE + path, content=body)
        assert r.status_code == 401 and r.headers['www-authenticate'] == 'Bearer'
    assert not list(worker.root.iterdir())
    assert client.get(BASE + '/work?limit=3', headers=AUTH).json()['requested'] == 3
    assert client.get(BASE + '/work?limit=97', headers=AUTH).status_code == 422
    assert client.get(BASE + '/work', headers={'Authorization': 'Bearer wrong'}).status_code == 401
    monkeypatch.delenv('NEWS2DOUYIN_HISTORY_RELAY_TOKEN')
    assert client.get(BASE + '/work', headers=AUTH).status_code == 503
    worker.archive_source = 'direct'
    assert client.put(BASE + f'/packages/{NAME}', content=BODY, headers=AUTH).status_code == 503


def test_atomic_catalog_and_bad_replacement(setup, monkeypatch):
    client, worker = setup
    endpoint = BASE + '/catalog/gdeltv2/masterfilelist.txt'
    assert client.put(endpoint, content=LINE, headers=AUTH).json() == {
        'catalog': 'gdeltv2/masterfilelist.txt', 'state': 'accepted'}
    catalog = worker.root / 'relay-catalog' / 'gdeltv2-masterfilelist.txt'
    assert catalog.read_bytes() == LINE and worker.wake_event.is_set()
    before = catalog.stat().st_mtime_ns
    worker.wake_event.clear()
    for body, extra, expected in [(b'<!doctype html>', {}, 422), (LINE + b'12345 hash', {}, 422),
                                  (LINE, {'Content-Length': str(len(LINE) + 1)}, 422),
                                  (LINE, {'Content-Length': '9999999999'}, 413),
                                  (LINE, {'Content-Length': 'x'}, 400)]:
        assert client.put(endpoint, content=body, headers={**AUTH, **extra}).status_code == expected
    assert client.put(endpoint, content=iter([LINE]), headers=AUTH).status_code == 411
    assert client.put(BASE + '/catalog/tmp/filesizes', content=LINE, headers=AUTH).status_code == 404
    assert catalog.read_bytes() == LINE and catalog.stat().st_mtime_ns == before
    assert not worker.wake_event.is_set() and not list((worker.root / '.relay-incoming').iterdir())
    monkeypatch.setattr(api, 'disk_usage', lambda path: SimpleNamespace(free=0))
    assert client.put(endpoint, content=LINE, headers=AUTH).status_code == 507
    assert catalog.stat().st_mtime_ns == before


@pytest.mark.parametrize('relative,data', [
    ('events/filesizes', b'100 1979.zip\n'), ('events/md5sums', f'{DIGEST} 1979.zip'.encode()),
    ('gkg/filesizes', b'100 20130401.gkg.csv.zip\n'),
    ('gkg/md5sums', f'{DIGEST} 20130401.gkg.csv.zip'.encode()),
])
def test_legacy_catalogs(setup, relative, data):
    client, worker = setup
    assert client.put(BASE + '/catalog/' + relative, content=data, headers=AUTH).status_code == 200
    assert (worker.root / 'relay-catalog' / relative.replace('/', '-')).read_bytes() == data


def test_package_checked_against_planned_part(setup):
    client, worker = setup
    url = BASE + '/packages/' + NAME
    assert client.put(BASE + '/packages/unknown.zip', content=BODY, headers=AUTH).status_code == 404
    for body, status in [(BODY[:-1], 422), (BODY + b'!', 413), (b'x' * len(BODY), 422)]:
        assert client.put(url, content=body, headers=AUTH).status_code == status
    assert not worker.received and worker.state == 'pending'
    assert client.put(url, content=BODY, headers=AUTH).json() == {'state': 'staged', 'name': NAME}
    assert worker.received == [(NAME, BODY, len(BODY), DIGEST)]
    assert not list((worker.root / '.relay-incoming').iterdir())
    worker.state = 'ready'
    assert client.put(url, content=BODY, headers=AUTH).json() == {'state': 'ready', 'name': NAME}
    assert len(worker.received) == 1


def test_failure_only_permanent_codes(setup):
    client, worker = setup
    url = BASE + '/packages/' + NAME + '/failure'
    for value in ({}, {'status': 429}, {'status': 404, 'url': 'x'}, {'status': 404.0}):
        assert client.post(url, json=value, headers=AUTH).status_code == 422
    assert client.post(url, content=b'x'*257, headers=AUTH).status_code == 413
    assert client.post(url, json={'status': 410}).status_code == 401
    assert worker.state == 'pending'
    assert client.post(url, json={'status': 404}, headers=AUTH).json() == {'state': 'failed', 'name': NAME}

def test_disconnected_upload_removes_temp(setup):
    client, worker = setup

    def interrupted():
        yield BODY[:3]
        raise ClientDisconnect()

    r = client.put(BASE + '/packages/' + NAME, headers={**AUTH, 'Content-Length': str(len(BODY))},
                   content=interrupted())
    assert r.status_code == 400 and not worker.received
    assert not list((worker.root / '.relay-incoming').iterdir())


def test_download_client_script(setup):
    client, _ = setup
    r = client.get('/admin/history-archive/relay-client.py')
    assert r.status_code == 200 and r.content.startswith((b'#!', b'"""'))
    assert 'gdelt_history_relay.py' in r.headers['content-disposition']
    compile(r.content, 'gdelt_history_relay.py', 'exec')
    assert AUTH['Authorization'].encode() not in r.content
