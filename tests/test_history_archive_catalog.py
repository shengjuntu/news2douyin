from dataclasses import replace
from datetime import date
from hashlib import md5
from threading import Event

import pytest

from news2douyin.history_archive import catalog
from news2douyin.intake.service import IntakeHTTPError


_DIGEST = md5(b'ZIP fixture').hexdigest()


def _old_catalog(root, kind, *names):
    (root / f'{kind}-filesizes').write_text(''.join(f'11 {name}\n' for name in names))
    (root / f'{kind}-md5sums').write_text(''.join(f'{_DIGEST}  {name}\n' for name in names))


def _modern_catalog(root, *names):
    (root / 'gdeltv2-masterfilelist.txt').write_text(''.join(
        f'11 {_DIGEST} http://data.gdeltproject.org/gdeltv2/{name}\n' for name in names
    ))


def test_catalog_legacy_boundaries_and_full_gkg(tmp_path):
    _old_catalog(tmp_path, 'events', '1979.zip', '2005.zip', '200601.zip', '200602.zip',
                 '201303.zip', '20130401.export.CSV.zip', '20150218.export.CSV.zip',
                 '20150219.export.CSV.zip', 'GDELT.MASTERREDUCEDV2.1979-2013.zip')
    _old_catalog(tmp_path, 'gkg', '20130401.gkg.csv.zip', '20130401.gkgcounts.csv.zip',
                 '20150218.gkg.csv.zip', '20150218.gkg.csv.zip')
    _modern_catalog(tmp_path, '20150218230000.export.CSV.zip', '20150218230000.mentions.CSV.zip',
                    '20150218230000.gkg.csv.zip', '20150218231500.export.CSV.zip',
                    '20150218231500.export.CSV.zip', '20150219000000.translation.gkg.csv.zip',
                    '20150218224500.export.CSV.zip', '20150218230000.other.zip')
    rows = catalog.list_packages(date(1979, 1, 1), date(2015, 2, 19), catalog.KINDS, cache_dir=tmp_path)
    assert [row.name for row in rows] == [
        '20150218231500.export.CSV.zip', '20150218230000.export.CSV.zip',
        '20150218230000.gkg.csv.zip', '20150218230000.mentions.CSV.zip',
        '20150218.export.CSV.zip', '20150218.gkg.csv.zip',
        '20130401.export.CSV.zip', '20130401.gkg.csv.zip', '201303.zip',
        '200602.zip', '200601.zip', '2005.zip', '1979.zip',
    ]
    assert rows[0].url == catalog.HOST + '/gdeltv2/20150218231500.export.CSV.zip'
    assert (rows[0].version, rows[0].date_from, rows[0].date_to) == (2, date(2015, 2, 18), date(2015, 2, 18))
    day = date(2006, 1, 8)
    [row] = catalog.list_packages(day, day, ('events',), cache_dir=tmp_path)
    assert (row.version, row.stamp, row.date_from, row.date_to) == (1, '200601', date(2006, 1, 1), date(2006, 1, 31))
    assert catalog.list_packages(day, day, ('mentions',), cache_dir=tmp_path) == []


def test_manifest_refresh_uses_fixed_url_and_refuses_html(monkeypatch, tmp_path):
    urls = []
    responses = iter((
        _Response(b'<html>Not a catalog</html>'),
        _Response(f'11 {_DIGEST} http://data.gdeltproject.org/gdeltv2/20220222120000.export.CSV.zip\n'.encode()),
    ))

    def get(url, **kwargs):
        urls.append(url)
        assert not kwargs['allow_redirects']
        return next(responses)

    monkeypatch.setattr(catalog.requests, 'get', get)
    day = date(2022, 2, 22)
    with pytest.raises(ValueError, match='invalid_catalog'):
        catalog.list_packages(day, day, ('events',), cache_dir=tmp_path)
    assert not list(tmp_path.iterdir())
    [row] = catalog.list_packages(day, day, ('events',), cache_dir=tmp_path)
    assert row.stamp == '20220222120000'
    assert catalog.list_packages(day, day, ('events',), cache_dir=tmp_path) == [row]
    assert urls == [catalog.HOST + '/gdeltv2/masterfilelist.txt'] * 2


class _Response:
    def __init__(self, body=b'', status=200, headers=None):
        self.body = body
        self.status_code = status
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def iter_content(self, _):
        midpoint = len(self.body) // 2
        yield self.body[:midpoint]
        yield self.body[midpoint:]


@pytest.mark.parametrize('filename,kind', [('20220222120000.export.CSV.zip', 'events'),
                                           ('20140201.gkg.csv.zip', 'gkg')])
def test_download_checks_size_and_digest_before_publish(monkeypatch, tmp_path, filename, kind):
    if filename.startswith('2022'):
        _modern_catalog(tmp_path, filename)
        day = date(2022, 2, 22)
    else:
        _old_catalog(tmp_path, kind, filename)
        day = date(2014, 2, 1)
    [row] = catalog.list_packages(day, day, (kind,), cache_dir=tmp_path)
    target = tmp_path / 'stage' / filename
    monkeypatch.setattr(catalog.requests, 'get', lambda *a, **k: _Response(b'ZIP fixture'))
    assert catalog.download_package(row, target)['compressed_bytes'] == 11
    assert target.read_bytes() == b'ZIP fixture'
    assert list(target.parent.iterdir()) == [target]
    with pytest.raises(ValueError, match='size_limit'):
        catalog.download_package(row, target, max_bytes=10)
    with pytest.raises(ValueError, match='invalid GDELT archive URL'):
        catalog.download_package(replace(row, url='http://localhost/' + filename), target)
    monkeypatch.setattr(catalog.requests, 'get', lambda *a, **k: _Response(b'BAD fixture'))
    with pytest.raises(ValueError, match='checksum_or_size_mismatch'):
        catalog.download_package(row, target)
    assert target.read_bytes() == b'ZIP fixture'


def test_download_interrupt_and_http_status(monkeypatch, tmp_path):
    _modern_catalog(tmp_path, '20220222120000.export.CSV.zip')
    day = date(2022, 2, 22)
    [row] = catalog.list_packages(day, day, ('events',), cache_dir=tmp_path)
    target = tmp_path / 'stage' / row.name
    stop = Event()
    stop.set()
    with pytest.raises(InterruptedError):
        catalog.download_package(row, target, stop=stop)
    for status in (404, 429):
        monkeypatch.setattr(catalog.requests, 'get', lambda *a, **k: _Response(status=status, headers={'Retry-After': '37'}))
        with pytest.raises(IntakeHTTPError) as exc:
            catalog.download_package(row, target)
        assert (exc.value.status, exc.value.retry_after) == (status, 37 if status == 429 else 0)
    assert not list(target.parent.iterdir())
