from datetime import date
from hashlib import md5, sha256
import os
from threading import Event
from time import time

import pytest
import requests

from news2douyin.history_archive import catalog
from news2douyin.intake.service import IntakeHTTPError


DATA = b'partial ZIP bytes'
DAY = date(2022, 2, 22)
NAME = '20220222120000.export.CSV.zip'
PACKAGE = catalog.ArchivePackage(NAME, 'events', 2, DAY, DAY, '20220222120000',
                                 f'{catalog.HOST}/gdeltv2/{NAME}', len(DATA), md5(DATA).hexdigest())
CATALOG = f'{len(DATA)} {PACKAGE.md5} {PACKAGE.url}\n'.encode()


class Response:
    def __init__(self, body, *, status=200, headers=None, broken=False):
        self.body, self.status_code, self.broken = body, status, broken
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def iter_content(self, _):
        yield self.body
        if self.broken:
            raise requests.exceptions.ChunkedEncodingError('interrupted fixture')


def install(monkeypatch, *responses):
    calls = []
    pending = iter(responses)

    def get(url, **kwargs):
        assert not kwargs['allow_redirects']
        calls.append((url, kwargs['headers'], kwargs['timeout']))
        return next(pending)

    monkeypatch.setattr(catalog.requests, 'get', get)
    return calls


def exact_range(body, size, offset, **headers):
    return Response(body[offset:], status=206, headers={
        'Content-Length': str(size-offset),
        'Content-Range': f'bytes {offset}-{size-1}/{size}',
        **headers,
    })


def test_broken_connection_resumes_zip_and_reports_progress(monkeypatch, tmp_path):
    offset = 8
    calls = install(monkeypatch, Response(DATA[:offset], broken=True), exact_range(DATA, len(DATA), offset))
    destination = tmp_path / NAME
    progress = []
    report = lambda received, total: progress.append((received, total))
    with pytest.raises(catalog.ArchiveTransferError):
        catalog.download_package(PACKAGE, destination, progress=report)
    assert not destination.exists()
    assert destination.with_suffix('.zip.part').read_bytes() == DATA[:offset]
    result = catalog.download_package(PACKAGE, destination, progress=report)
    assert calls[1][1]['Range'] == 'bytes=8-'
    assert (8, len(DATA)) in progress and progress[-1] == (len(DATA), len(DATA))
    assert result['sha256'] == sha256(DATA).hexdigest()
    assert destination.read_bytes() == DATA and list(tmp_path.iterdir()) == [destination]


@pytest.mark.parametrize('offset_response', [
    Response(DATA),
    Response(b'', status=416),
    Response(b'bad', status=206, headers={'Content-Range': 'bytes 0-1/17'}),
])
def test_missing_or_bad_range_restarts_from_zero(monkeypatch, tmp_path, offset_response):
    destination = tmp_path / NAME
    destination.with_suffix('.zip.part').write_bytes(DATA[:3])
    calls = install(monkeypatch, offset_response, *([Response(DATA)] if offset_response.status_code != 200 else []))
    catalog.download_package(PACKAGE, destination)
    assert destination.read_bytes() == DATA
    assert calls[0][1]['Range'] == 'bytes=3-'
    if offset_response.status_code != 200:
        assert 'Range' not in calls[1][1]


def test_full_replacement_after_corrupt_partial_fails_checksum(monkeypatch, tmp_path):
    destination = tmp_path / NAME
    partial = destination.with_suffix('.zip.part')
    partial.write_bytes(b'bad!')
    calls = install(monkeypatch, exact_range(DATA, len(DATA), 4), Response(b'x' * PACKAGE.size))
    with pytest.raises(ValueError, match='checksum_or_size_mismatch'):
        catalog.download_package(PACKAGE, destination)
    assert len(calls) == 2 and 'Range' not in calls[1][1]
    assert not destination.exists() and not partial.exists()


def test_pause_and_http_backoff_preserve_zip_prefix(monkeypatch, tmp_path):
    destination = tmp_path / NAME
    partial = destination.with_suffix('.zip.part')
    stop = Event()

    def observe(received, _total):
        if received:
            stop.set()

    calls = install(monkeypatch, Response(DATA[:3]), Response(b'', status=503, headers={'Retry-After': '41'}),
                    exact_range(DATA, len(DATA), 3))
    # The final callback can pause after a chunk. The partial survives and
    # either the stop or the short response leaves nothing published.
    with pytest.raises((catalog.ArchiveTransferError, InterruptedError)):
        catalog.download_package(PACKAGE, destination, stop=stop, progress=observe)
    assert not destination.exists() and partial.read_bytes() == DATA[:3]
    stop.clear()
    with pytest.raises(IntakeHTTPError) as error:
        catalog.download_package(PACKAGE, destination)
    assert (error.value.status, error.value.retry_after) == (503, 41)
    assert partial.read_bytes() == DATA[:3]
    catalog.download_package(PACKAGE, destination)
    assert calls[1][1]['Range'] == calls[2][1]['Range'] == 'bytes=3-'
    assert destination.read_bytes() == DATA


def test_catalog_resume_conditional_and_separate_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv('NEWS2DOUYIN_GDELT_CONNECT_TIMEOUT', '60')
    monkeypatch.setenv('NEWS2DOUYIN_GDELT_READ_TIMEOUT', '30')
    monkeypatch.delenv('NEWS2DOUYIN_HISTORY_ARCHIVE_CONNECT_TIMEOUT', raising=False)
    monkeypatch.delenv('NEWS2DOUYIN_HISTORY_ARCHIVE_READ_TIMEOUT', raising=False)
    offset, headers = 12, {'ETag': '"file-1"', 'Content-Length': str(len(CATALOG))}
    calls = install(monkeypatch, Response(CATALOG[:offset], headers=headers, broken=True),
                    exact_range(CATALOG, len(CATALOG), offset, ETag='"file-1"'), Response(b'', status=304))
    progress = []

    def load():
        return catalog.list_packages(DAY, DAY, ('events',), cache_dir=tmp_path,
                                     progress=lambda name, received, total: progress.append((name, received, total)))

    with pytest.raises(catalog.ArchiveTransferError):
        load()
    monkeypatch.setenv('NEWS2DOUYIN_HISTORY_ARCHIVE_READ_TIMEOUT', '200')
    assert load() == [PACKAGE]
    assert calls[0][2] == (60, 120) and calls[1][2] == (60, 200)
    assert calls[1][1]['Range'] == 'bytes=12-' and calls[1][1]['If-Range'] == '"file-1"'
    assert ('gdeltv2/masterfilelist.txt', offset, len(CATALOG)) in progress
    assert progress[-1][1:] == (len(CATALOG), len(CATALOG))
    published = tmp_path / 'gdeltv2-masterfilelist.txt'
    now = time()
    stale = now - 3 * 86400
    # Adjust the cache clock through the planner so the stored ETag remains
    # bound to the actual downloaded file's stat.
    monkeypatch.setattr(catalog, 'time', lambda: now + 3 * 86400)
    assert load() == [PACKAGE]
    assert calls[2][1]['If-None-Match'] == '"file-1"'
    assert published.stat().st_mtime > stale


def test_catalog_invalid_resume_falls_back_and_keeps_last_valid_file(monkeypatch, tmp_path):
    offset, headers = 6, {'ETag': '"file-1"', 'Content-Length': str(len(CATALOG))}
    destination = tmp_path / 'gdeltv2-masterfilelist.txt'
    destination.write_bytes(CATALOG)
    os.utime(destination, (1, 1))
    calls = install(monkeypatch, Response(CATALOG[:offset], headers=headers, broken=True),
                    exact_range(CATALOG, len(CATALOG), offset, ETag='"changed"'),
                    Response(CATALOG))  # EOF-only, valid-looking prefix

    def load():
        return catalog.list_packages(DAY, DAY, ('events',), cache_dir=tmp_path)

    with pytest.raises(catalog.ArchiveTransferError):
        load()
    with pytest.raises(ValueError, match='unframed_catalog'):
        load()
    assert calls[1][1]['If-Range'] == '"file-1"' and 'Range' not in calls[2][1]
    assert destination.read_bytes() == CATALOG and list(tmp_path.iterdir()) == [destination]
