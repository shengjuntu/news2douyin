"""Targeted Web News NGrams 3.0 reconstruction using optional upstream gdeltnews.

Minute archives are shared by all requested URLs, scanned once, and cached with
bounded retention. Only the code-owned GDELT archive URL is fetched. The text
is a reconstruction of one URL/language/observation, never a publisher snapshot.
"""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import gzip
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import zlib

import requests

from ..collect.providers.gdelt import request_timeouts
from ..selection.source_content import source_url

BASE = 'https://data.gdeltproject.org/gdeltv3/webngrams/'
METHOD = 'gdeltnews/1.1.0'
MAX_URLS = 100
MAX_COMPRESSED = 64 * 1024**2
MAX_EXPANDED = 512 * 1024**2
MAX_LINE = 64 * 1024
MAX_FRAGMENTS = 4096
MAX_MATCH_BYTES = 16 * 1024**2
CACHE_BYTES = 512 * 1024**2
CACHE_FILES = 512
NEGATIVE_SECONDS = 15 * 60
_MINUTE_LOCKS = tuple(threading.Lock() for _ in range(32))
_CACHE_LOCK = threading.Lock()


@dataclass(frozen=True)
class NgramResult:
    status: str
    stamp: str
    text: str = ''
    language: str = ''
    archive_sha256: str = ''
    fragments: int = 0  # Number of observations supplied, not a coverage estimate.
    detail: str = ''
    method: str = METHOD
    observation_at: str = ''


class _MinuteProblem(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def batch_url(stamp: str) -> str:
    if not isinstance(stamp, str) or not re.fullmatch(r'[0-9]{12}00', stamp):
        raise ValueError('Web NGrams 批次须为 14 位 UTC 整分钟')
    datetime.strptime(stamp, '%Y%m%d%H%M%S')
    return BASE + stamp + '.webngrams.json.gz'


def cache_path(storage_root, stamp):
    batch_url(stamp)
    return Path(storage_root) / 'intake' / 'webngrams' / (stamp + '.webngrams.json.gz')


def _load_assembler():
    try:
        if metadata.version('gdeltnews') != '1.1.0':
            raise _MinuteProblem('unavailable', '请安装本版本的 ngrams 可选依赖')
        from gdeltnews.wordmatch import process_article, transform_dict
    except (ImportError, metadata.PackageNotFoundError) as exc:
        raise _MinuteProblem('unavailable', '请安装 ngrams 可选依赖（gdeltnews==1.1.0）') from exc

    def assemble(url, rows):
        transformed = transform_dict({url: rows})
        assembled = process_article((url, transformed[url]), keep_unmerged=False)
        if assembled.get('error'):
            raise ValueError('gdeltnews 无法重建此报道')
        return assembled['text']

    return assemble


def _targets(urls):
    result = {}
    for url in urls:
        if not source_url(url):
            raise ValueError('补证需要原始报道的 HTTP(S) URL')
        result[url] = None
        if len(result) > MAX_URLS:
            raise ValueError(f'单批最多 {MAX_URLS} 个报道 URL')
    return result


def _download(stamp, output):
    timeout = request_timeouts()
    deadline = time.monotonic() + sum(timeout)
    with requests.get(batch_url(stamp), stream=True, allow_redirects=False, timeout=timeout,
                      headers={'Accept-Encoding': 'identity'}) as response:
        if response.status_code == 404:
            raise _MinuteProblem('missing_batch', 'GDELT 当前没有此分钟文件（HTTP 404）')
        if response.status_code != 200:
            raise _MinuteProblem('failed', f'GDELT HTTP {response.status_code}')
        declared = response.headers.get('Content-Length', '')
        if declared.isdigit() and int(declared) > MAX_COMPRESSED:
            raise _MinuteProblem('limit', '压缩分钟文件超过大小上限')
        size = 0
        for chunk in response.iter_content(128 * 1024):
            if time.monotonic() > deadline:
                raise _MinuteProblem('failed', '获取分钟文件超过总时限')
            size += len(chunk)
            if size > MAX_COMPRESSED:
                raise _MinuteProblem('limit', '压缩分钟文件超过大小上限')
            output.write(chunk)
        if not size:
            raise _MinuteProblem('failed', 'GDELT 返回了空文件')


def _prune(path):
    # Only cache files created by this adapter are eligible. Never touch a
    # publisher snapshot, the main lake, or a download still being written.
    with _CACHE_LOCK:
        entries = []
        for candidate in path.parent.iterdir():
            if re.fullmatch(r'[0-9]{14}\.[a-z0-9_]+\.part', candidate.name):
                try:
                    if time.time() - candidate.stat().st_mtime > 86_400:
                        candidate.unlink()
                except OSError:
                    pass
                continue
            if not re.fullmatch(r'[0-9]{14}\.webngrams\.json\.gz(?:\.missing)?', candidate.name):
                continue
            try:
                info = candidate.stat()
            except FileNotFoundError:
                continue
            entries.append((info.st_mtime_ns, candidate, info.st_size))
        total = sum(size for _, _, size in entries)
        count = len(entries)
        for _, candidate, size in sorted(entries):
            if total <= CACHE_BYTES and count <= CACHE_FILES:
                break
            if candidate == path:
                continue
            try:
                candidate.unlink()
            except OSError:
                continue
            total -= size
            count -= 1


def _scan(path, targets):
    groups = defaultdict(lambda: defaultdict(list))
    seen = defaultdict(int)
    invalid = set()
    skipped_languages = {}
    digest = hashlib.sha256()
    with path.open('rb') as archive:
        if os.fstat(archive.fileno()).st_size > MAX_COMPRESSED:
            raise _MinuteProblem('limit', '压缩分钟文件超过大小上限')
        if archive.read(2) != b'\x1f\x8b':
            raise gzip.BadGzipFile('missing gzip header')
        archive.seek(0)
        for chunk in iter(lambda: archive.read(256 * 1024), b''):
            digest.update(chunk)
        archive.seek(0)
        with gzip.GzipFile(fileobj=archive) as data:
            total = matched = 0
            while line := data.readline(MAX_LINE + 1):
                total += len(line)
                if len(line) > MAX_LINE or total > MAX_EXPANDED:
                    raise _MinuteProblem('limit', '分钟文件解压超过扫描上限')
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict) or not isinstance(row.get('url'), str):
                    raise ValueError('无效的 GDELT NGrams 记录')
                url = row['url']
                if url not in targets:
                    continue
                lang = row.get('lang')
                if str(row.get('type')) == '2':
                    skipped_languages[url] = lang if isinstance(lang, str) else ''
                    continue
                if (str(row.get('type')) != '1' or not isinstance(lang, str) or not lang
                        or not isinstance(row.get('date'), str) or not row['date']
                        or not all(isinstance(row.get(key, ''), str) for key in ('pre', 'post'))
                        or not isinstance(row.get('ngram'), str) or not row['ngram'].strip()
                        or str(row.get('pos')) not in _POSITIONS):
                    invalid.add(url)
                    continue
                seen[url] += 1
                if seen[url] > MAX_FRAGMENTS:
                    groups.pop(url, None)
                    continue
                matched += len(line)
                if matched > MAX_MATCH_BYTES:
                    raise _MinuteProblem('limit', '候选报道分片总量超过单批上限')
                groups[url][(row['date'], lang)].append(row)
    return digest.hexdigest(), groups, seen, invalid, skipped_languages


_POSITIONS = frozenset(str(i) for i in range(0, 100, 10))


def _from_scan(stamp, targets, scanned, assembler):
    digest, groups, seen, invalid, skipped_languages = scanned
    results = {}
    for url in targets:
        values = dict(stamp=stamp, archive_sha256=digest)
        if seen[url] > MAX_FRAGMENTS:
            result = NgramResult('limit', **values, detail='报道分片超过上限')
        elif url in invalid:
            result = NgramResult('failed', **values, detail='报道分片字段无效')
        elif groups[url]:
            # Never join observations across languages, editions, or minutes.
            key, rows = max(groups[url].items(), key=lambda pair: (len(pair[1]), pair[0]))
            values.update(language=key[1], fragments=len(rows), observation_at=key[0])
            try:
                text = assembler(url, rows).strip()
            except (ValueError, TypeError, KeyError):
                text = ''
            if text:
                result = NgramResult('ready', **values, text=text)
            else:
                result = NgramResult('failed', **values, detail='匹配的分片未能重建文本')
        elif url in skipped_languages:
            result = NgramResult('unsupported_language', **values, language=skipped_languages[url],
                                 detail='gdeltnews 当前不支持无空格分词的 Type 2')
        else:
            result = NgramResult('not_found', **values, detail='此分钟文件未匹配报道的原始 URL')
        results[url] = result
    return results


def _read_minute(path, stamp, targets, allow_download):
    negative = path.with_name(path.name + '.missing')
    try:
        return _scan(path, targets)
    except FileNotFoundError:
        pass
    except (gzip.BadGzipFile, EOFError, zlib.error, ValueError, UnicodeError):
        path.unlink(missing_ok=True)  # A damaged cache must not permanently poison retries.
        if not allow_download:
            raise _MinuteProblem('failed', '缓存的分钟文件损坏')
    if not allow_download:
        raise _MinuteProblem('missing_batch', '本机没有缓存此分钟文件')
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if time.time() - negative.stat().st_mtime < NEGATIVE_SECONDS:
            raise _MinuteProblem('missing_batch', '最近一次请求得到 GDELT HTTP 404')
    except FileNotFoundError:
        pass
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False, prefix=stamp+'.', suffix='.part') as output:
            temp = Path(output.name)
            _download(stamp, output)
        scanned = _scan(temp, targets)
        os.replace(temp, path)
        negative.unlink(missing_ok=True)
        _prune(path)
        return scanned
    except _MinuteProblem as exc:
        if exc.status == 'missing_batch':
            negative.touch()
            _prune(negative)
        raise
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def reconstruct_minute(storage_root, stamp: str, urls, *, allow_download=True) -> dict[str, NgramResult]:
    """Match exact URLs in one UTC minute and return a result for every candidate.

    A 404 is a missing time slot; a URL miss only describes the searched file.
    Failures/oversize inputs never publish partial results from a truncated scan.
    """
    path = cache_path(storage_root, stamp)
    targets = _targets(urls)
    if not targets:
        return {}
    try:
        if stamp < '20200101000100':
            raise _MinuteProblem('missing_batch', '时间早于 Web News NGrams 3.0 首个批次')
        assembler = _load_assembler()
        with _MINUTE_LOCKS[hash(stamp) % len(_MINUTE_LOCKS)]:
            scanned = _read_minute(path, stamp, targets, allow_download)
        return _from_scan(stamp, targets, scanned, assembler)
    except _MinuteProblem as exc:
        status, detail = exc.status, exc.detail
    except (OSError, EOFError, ValueError, zlib.error, requests.RequestException) as exc:
        status, detail = 'failed', '分钟文件读取或解析失败 · ' + type(exc).__name__
    return {url: NgramResult(status, stamp, detail=detail) for url in targets}


def reconstruct_local_archive(path, stamp, urls):
    """Read a supplied archive once without updating the cache or contacting GDELT.

    File-level failures stop the audit rather than manufacturing URL misses.
    """
    batch_url(stamp)
    targets = _targets(urls)
    if not targets:
        return {}
    try:
        assembler = _load_assembler()
        return _from_scan(stamp, targets, _scan(Path(path), targets), assembler)
    except _MinuteProblem as exc:
        raise ValueError(exc.detail) from exc
