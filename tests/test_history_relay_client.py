import io
import json
from email.message import Message
from hashlib import md5
from urllib.error import HTTPError

import pytest
from news2douyin.history_archive import relay_client as client


ZIP = b'test archive body'
NAME = '20220222003000.export.CSV.zip'


def row(name=NAME, content=ZIP):
    return dict(name=name, md5=md5(content, usedforsecurity=False).hexdigest(), size=len(content),
                url=client.OFFICIAL + 'gdeltv2/' + name)


class Response(io.BytesIO):
    def __init__(self, body, *, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers if headers is not None else {'Content-Length': str(len(body))}


def setup_relay(tmp_path, work=None):
    relay = client.Relay('http://localhost:3180', 'private-key', tmp_path)
    work = work if work is not None else {'packages': [row()], 'catalogs': [], 'paused': False}
    uploads, requests = [], []

    def upstream(req, timeout):
        assert req.get_header('Authorization') is None
        assert req.full_url.startswith(client.OFFICIAL)
        requests.append(req.full_url)
        return Response(ZIP)

    def downstream(req, timeout):
        assert req.get_header('Authorization') == 'Bearer private-key'
        assert req.full_url.startswith(relay.server + '/')
        if req.method == 'GET':
            return Response(json.dumps(work).encode())
        if req.method == 'POST':
            ack = {'state': 'failed', 'name': req.full_url.split('/')[-2]}
            uploads.append((req.method, req.full_url, req.data))
        else:
            data = req.data.read()
            assert int(req.get_header('Content-length')) == len(data)
            uploads.append((req.method, req.full_url, data))
            if '/catalog/' in req.full_url:
                ack = {'state': 'accepted', 'catalog': req.full_url.split('/catalog/')[-1]}
            else:
                ack = {'state': 'staged', 'name': req.full_url.split('/')[-1]}
        return Response(json.dumps(ack).encode())

    relay.upstream.open, relay.downstream.open = upstream, downstream
    return relay, work, uploads, requests


def test_upload_stream_includes_content_length_and_isolates_token(tmp_path):
    relay, work, uploads, requests = setup_relay(tmp_path)
    work['catalogs'] = ['gdeltv2/masterfilelist.txt']
    assert relay.run_once() == 2
    assert [x[1] for x in uploads] == [relay.server + '/catalog/gdeltv2/masterfilelist.txt',
                                       relay.server + '/packages/' + NAME]
    assert [x[2] for x in uploads] == [ZIP, ZIP]
    assert requests == [client.OFFICIAL + 'gdeltv2/masterfilelist.txt', row()['url']]
    assert not list(relay.cache.iterdir())


def test_ack_loss_resends_verified_cache_even_when_ticket_no_longer_listed(tmp_path):
    relay, work, uploads, requests = setup_relay(tmp_path)
    old_open = relay.downstream.open

    def dropped(req, timeout):
        response = old_open(req, timeout)
        if req.method == 'PUT':
            raise ConnectionError('connection lost after server commit')
        return response

    relay.downstream.open = dropped
    with pytest.raises(ConnectionError):
        relay.run_once()
    assert (relay.cache / 'payload').read_bytes() == ZIP
    work['packages'] = []
    restarted = client.Relay('http://localhost:3180', 'private-key', tmp_path)
    restarted.downstream.open = old_open
    assert restarted.run_once() == 1
    assert len(requests) == 1 and uploads[0] == uploads[1]
    assert not list(restarted.cache.iterdir())


def test_corrupt_input_and_permanent_missing_do_not_block_other_packages(tmp_path):
    relay, work, uploads, requests = setup_relay(tmp_path)
    work['packages'] = [row(content=b'X' * len(ZIP)), row('20220222001500.export.CSV.zip'),
                        row('20220222000000.export.CSV.zip')]
    old_open = relay.upstream.open

    def missing(req, timeout):
        if '001500' in req.full_url:
            raise HTTPError(req.full_url, 404, 'not found', Message(), io.BytesIO())
        return old_open(req, timeout)

    relay.upstream.open = missing
    with pytest.raises(client.TransferError, match='1 个文件'):
        relay.run_once()
    assert [(x[0], x[1].rsplit('/', 1)[-1]) for x in uploads] == [('POST', 'failure'), ('PUT', work['packages'][2]['name'])]
    assert json.loads(uploads[0][-1])['status'] == 404
    assert not list(relay.cache.iterdir())


def test_429_delays_whole_batch_without_uploading_or_reporting_missing(tmp_path):
    relay, work, uploads, _ = setup_relay(tmp_path)
    work['packages'].append(row('20220222000000.export.CSV.zip'))
    calls = []

    def limited(relative):
        calls.append(relative)
        headers = Message()
        headers['Retry-After'] = '357'
        raise HTTPError(client.OFFICIAL + relative, 429, 'slow down', headers, io.BytesIO())

    relay.open_official = limited
    with pytest.raises(client.TransferError) as exc:
        relay.run_once()
    assert exc.value.delay == 357 and len(calls) == 1 and not uploads
    assert not list(relay.cache.iterdir())


@pytest.mark.parametrize('status, headers', [
    (200, {'Content-Length': '500'}), (206, {'Content-Length': '17'}),
    (200, {'Content-Length': '17', 'Content-Range': 'bytes 0-16/400'}),
    (200, {}), (200, {'Content-Length': '0'}),
])
def test_reject_incomplete_catalog(tmp_path, status, headers):
    relay, work, uploads, _ = setup_relay(tmp_path, {'catalogs': ['gdeltv2/masterfilelist.txt'], 'packages': []})
    relay.open_official = lambda relative: Response(ZIP, status=status, headers=headers)
    with pytest.raises(client.TransferError):
        relay.run_once()
    assert not uploads and not list(relay.cache.iterdir())


def test_catalog_rejected_by_server_is_fetched_again(tmp_path):
    relay, work, uploads, requests = setup_relay(tmp_path, {'catalogs': ['events/filesizes'], 'packages': []})
    old_open = relay.downstream.open

    def reject(req, timeout):
        if req.method == 'PUT':
            raise HTTPError(req.full_url, 422, 'invalid catalog', Message(), io.BytesIO())
        return old_open(req, timeout)

    relay.downstream.open = reject
    with pytest.raises(client.TransferError):
        relay.run_once()
    assert not list(relay.cache.iterdir())
    relay.downstream.open = old_open
    assert relay.run_once() == 1 and len(requests) == 2


@pytest.mark.parametrize('name,folder', [
    ('1979.zip', 'events'), ('201303.zip', 'events'), ('20140101.export.CSV.zip', 'events'),
    ('20140101.gkg.csv.zip', 'gkg'), (NAME, 'gdeltv2'),
    ('20150218230000.gkg.csv.zip', 'gdeltv2'), ('20220222001500.mentions.CSV.zip', 'gdeltv2'),
])
def test_accepted_archives(name, folder):
    item = row(name)
    item['url'] = client.OFFICIAL + folder + '/' + name
    assert client.package_ticket(item).relative == folder + '/' + name


@pytest.mark.parametrize('name', ['20130230.export.CSV.zip', '201302.gkg.csv.zip',
                       '20220222000300.mentions.CSV.zip', '20220222000000.translation.gkg.csv.zip',
                       '200513.zip', '../1980.zip', 'test.csv', '20220222000000.export.CSV.zip?x=y'])
def test_rejected_archives(name):
    with pytest.raises(ValueError):
        client.package_ticket(row(name))


def test_scope_urls_and_redirects(tmp_path):
    bad = {**row(), 'url': 'https://other.example/' + NAME}
    with pytest.raises(ValueError):
        client.package_ticket(bad)
    assert client.NoRedirect().redirect_request(None, None, 307, '', {}, 'https://elsewhere') is None
    with pytest.raises(ValueError):
        client.Relay('https://login:pass@example.test', 'key', tmp_path)
    assert client.Relay('https://one.example.test', 'key', tmp_path).cache != client.Relay('https://two.example.test', 'key', tmp_path).cache
