"""Provider contract using the official minute schema, isolated from live HTTP."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import gzip
import hashlib
from importlib import metadata
import json

import pytest
from news2douyin.intake import ngrams


STAMP = '20250316000100'
URL = 'https://publisher.example/city'


def tokens(word, pre, post, pos=0, *, url=URL, lang='en', kind=1, date='2025-03-16T00:01:00.000Z'):
    return dict(url=url, date=date, ngram=word, pos=pos, pre=pre, post=post, lang=lang, type=kind)


def compressed(rows):
    data = b''.join(json.dumps(row, ensure_ascii=False).encode() + b'\n' for row in rows)
    return gzip.compress(data)


@pytest.fixture
def archive(tmp_path):
    def put(rows):
        path = ngrams.cache_path(tmp_path, STAMP)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = compressed(rows)
        path.write_bytes(data)
        return hashlib.sha256(data).hexdigest()
    return put


@pytest.fixture
def assembler(monkeypatch):
    def fake(url, rows):
        return ' '.join(row['ngram'] for row in rows)
    monkeypatch.setattr(ngrams, '_load_assembler', lambda: fake)


def test_real_pinned_gdeltnews_reconstruction(tmp_path, archive):
    pytest.importorskip('gdeltnews')
    assert metadata.version('gdeltnews') == '1.1.0'
    digest = archive([tokens('city', 'On Monday the', 'council met'),
        tokens('met', 'the city council', 'and the council'),
        tokens('council', 'met and the', 'approved the budget', pos=10),
        tokens('approved', 'the council', 'the budget', pos=10)])
    result = ngrams.reconstruct_minute(tmp_path, STAMP, [URL], allow_download=False)[URL]
    assert result.status == 'ready'
    assert result.text == 'On Monday the city council met and the council approved the budget'
    assert result.language == 'en' and result.fragments == 4
    assert result.method == 'gdeltnews/1.1.0' and result.archive_sha256 == digest


def test_one_scan_exact_url_independent_observations(tmp_path, archive, assembler, monkeypatch):
    unsupported = URL + '/jp'
    other = URL + '?different=1'
    archive([tokens('first', '', 'news'), tokens('development', '', 'news'),
             tokens('different-day', '', '', date='2025-03-15T23:59:00.000Z'),
             tokens('autre', '', '', lang='fr'), tokens('foreign', '', '', url=URL + '-suffix'),
             tokens('市', '', '', kind=2, lang='ja', url=unsupported)])
    real_scan = ngrams._scan
    scans = []
    def count(*args):
        scans.append(1)
        return real_scan(*args)
    monkeypatch.setattr(ngrams, '_scan', count)
    found = ngrams.reconstruct_minute(tmp_path, STAMP, [URL, other, unsupported], allow_download=False)
    assert len(scans) == 1
    assert found[URL].text == 'first development' and found[URL].fragments == 2
    assert found[URL].observation_at == '2025-03-16T00:01:00.000Z'
    assert found[other].status == 'not_found'
    assert found[unsupported].status == 'unsupported_language' and found[unsupported].language == 'ja'


def test_same_minute_concurrent_cache_and_missing_slot(tmp_path, monkeypatch, assembler):
    data = compressed([tokens('agreement', 'municipal', 'signed')])
    requests = []
    class Response:
        headers = {}
        def __init__(self, status):
            self.status_code = status
        def iter_content(self, chunk_size):
            yield data
    @contextmanager
    def fetch(url, **kwargs):
        assert kwargs['allow_redirects'] is False and kwargs['stream']
        requests.append(url)
        yield Response(200 if STAMP in url else 404)
    monkeypatch.setattr(ngrams.requests, 'get', fetch)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: ngrams.reconstruct_minute(tmp_path, STAMP, [URL]), range(2)))
    assert [r[URL].text for r in results] == ['agreement'] * 2
    assert requests == [ngrams.batch_url(STAMP)]
    second = '20250316000200'
    for _ in range(2):
        assert ngrams.reconstruct_minute(tmp_path, second, [URL])[URL].status == 'missing_batch'
    assert requests == [ngrams.batch_url(STAMP), ngrams.batch_url(second)]


def test_capped_and_corrupt_batches_never_return_partial_success(tmp_path, monkeypatch, assembler, archive):
    archive([tokens('first', 'x', 'y'), tokens('second', 'x', 'y')])
    with monkeypatch.context() as m:
        m.setattr(ngrams, 'MAX_FRAGMENTS', 1)
        result = ngrams.reconstruct_minute(tmp_path, STAMP, [URL], allow_download=False)[URL]
        assert result.status == 'limit' and not result.text
    with monkeypatch.context() as m:
        m.setattr(ngrams, 'MAX_EXPANDED', 100)
        result = ngrams.reconstruct_minute(tmp_path, STAMP, [URL], allow_download=False)[URL]
        assert result.status == 'limit' and not result.text
    path = ngrams.cache_path(tmp_path, STAMP)
    path.write_bytes(path.read_bytes()[:-7])
    result = ngrams.reconstruct_minute(tmp_path, STAMP, [URL], allow_download=False)[URL]
    assert result.status == 'failed' and not result.text


def test_failure_does_not_poison_cache(tmp_path, monkeypatch, assembler):
    responses = [(429, b''), (200, b'truncated'), (200, compressed([tokens('success', '', '')]))]
    @contextmanager
    def get(*_, **kwargs):
        status, data = responses.pop(0)
        class Response:
            status_code = status
            headers = {}
            def iter_content(self, chunk_size):
                yield data
        yield Response()
    monkeypatch.setattr(ngrams.requests, 'get', get)
    for status in ('failed', 'failed', 'ready'):
        assert ngrams.reconstruct_minute(tmp_path, STAMP, [URL])[URL].status == status
        assert ngrams.cache_path(tmp_path, STAMP).exists() == (status == 'ready')
    assert not list(ngrams.cache_path(tmp_path, STAMP).parent.glob('*.part'))


def test_unavailable_and_missing_are_distinct(tmp_path, monkeypatch):
    def absent():
        raise ngrams._MinuteProblem('unavailable', 'gdeltnews missing')
    monkeypatch.setattr(ngrams, '_load_assembler', absent)
    assert ngrams.reconstruct_minute(tmp_path, STAMP, [URL], allow_download=False)[URL].status == 'unavailable'
    monkeypatch.setattr(ngrams, '_load_assembler', lambda: None)
    assert ngrams.reconstruct_minute(tmp_path, STAMP, [URL], allow_download=False)[URL].status == 'missing_batch'
    assert not (tmp_path / 'intake').exists()
    with pytest.raises(ValueError):
        ngrams.reconstruct_minute(tmp_path, '../20250316000100', [URL])
    with pytest.raises(ValueError):
        ngrams.reconstruct_minute(tmp_path, STAMP, [f'{URL}/{i}' for i in range(101)])


def test_cache_retains_newest_and_leaves_unrelated_files(tmp_path, monkeypatch, assembler):
    def download(stamp, output):
        output.write(compressed([tokens('news', '', '')]))
    monkeypatch.setattr(ngrams, '_download', download)
    monkeypatch.setattr(ngrams, 'CACHE_FILES', 1)
    first = ngrams.cache_path(tmp_path, STAMP)
    first.parent.mkdir(parents=True)
    unrelated = first.parent / 'retained-note'
    unrelated.write_bytes(b'unrelated')
    assert ngrams.reconstruct_minute(tmp_path, STAMP, [URL])[URL].status == 'ready'
    second = '20250316000200'
    assert ngrams.reconstruct_minute(tmp_path, second, [URL])[URL].status == 'ready'
    assert not first.exists() and unrelated.read_bytes() == b'unrelated'
    assert ngrams.cache_path(tmp_path, second).is_file()


def test_http_redirect_and_oversize_do_not_cache(tmp_path, monkeypatch, assembler):
    class Response:
        def __init__(self, status, length):
            self.status_code = status
            self.headers = {'Content-Length': str(length)}
        def iter_content(self, chunk_size):
            pytest.fail('Rejected response body was consumed')
    replies = iter((Response(302, 1), Response(200, ngrams.MAX_COMPRESSED + 1)))
    @contextmanager
    def get(_, **kwargs):
        assert kwargs['allow_redirects'] is False
        yield next(replies)
    monkeypatch.setattr(ngrams.requests, 'get', get)
    for status in ('failed', 'limit'):
        assert ngrams.reconstruct_minute(tmp_path, STAMP, [URL])[URL].status == status
        assert not ngrams.cache_path(tmp_path, STAMP).exists()
