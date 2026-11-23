"""Bounded streaming and safe HTTP resume for the historical archive."""
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from hashlib import md5, sha256
import json
from math import isfinite
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from time import time

import requests

from ..intake.service import IntakeHTTPError, response_error


_BLOCK = 64 * 1024
_RANGE = re.compile(r'bytes (\d+)-(\d+)/(\d+)', re.ASCII)
_ETAG = re.compile(r'"[!#-~]{0,128}"', re.ASCII)


class ArchiveTransferError(RuntimeError):
    """A temporary connection failure or short response body."""


def _timeout(field, minimum):
    def value(prefix, fallback):
        try:
            seconds = float(os.getenv(prefix + field + '_TIMEOUT', fallback))
        except ValueError:
            return fallback
        return seconds if 1 <= seconds <= 600 else fallback

    inherited = value('NEWS2DOUYIN_GDELT_', minimum)
    return value('NEWS2DOUYIN_HISTORY_ARCHIVE_', max(minimum, inherited))


def _retry_after(headers):
    raw = headers.get('Retry-After', '')
    try:
        seconds = float(raw)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(raw).timestamp() - time()
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, seconds) if isfinite(seconds) else 0


def check_stop(stop):
    if stop is not None and stop.is_set():
        raise InterruptedError('historical archive task stopped')


def sync_directory(path):
    if hasattr(os, 'O_DIRECTORY'):
        try:
            fd = os.open(path, os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            # Some supported filesystems cannot fsync directory descriptors.
            pass


@contextmanager
def _response(get, url, headers, stop):
    check_stop(stop)
    try:
        with get(url, stream=True, allow_redirects=False,
                 timeout=(_timeout('CONNECT', 30), _timeout('READ', 120)),
                 headers={'Accept-Encoding': 'identity', **headers}) as response:
            if response.status_code in (408, 429) or 500 <= response.status_code < 600:
                raise IntakeHTTPError(response.status_code, _retry_after(response.headers))
            if response.status_code not in (206, 304, 416):
                response_error(response)
            yield response
    except requests.exceptions.ContentDecodingError as error:
        raise ValueError('archive_invalid_content_encoding') from error
    except requests.exceptions.RequestException as error:
        raise ArchiveTransferError('历史归档连接中断，等待重试。') from error


def _length(response):
    value = response.headers.get('Content-Length')
    if value is None:
        return None
    if not re.fullmatch(r'[0-9]{1,18}', value):
        raise ValueError('archive_invalid_content_length')
    return int(value)


def _range_offset(response, offset, total):
    if response.headers.get('Content-Encoding', 'identity').lower().strip() != 'identity':
        raise ValueError('archive_invalid_content_encoding')
    content_range = response.headers.get('Content-Range')
    if response.status_code == 200:
        if content_range is not None:
            raise ValueError('archive_unexpected_content_range')
        return 0
    if response.status_code != 206 or not offset:
        raise ValueError('archive_unexpected_partial_response')
    match = _RANGE.fullmatch(content_range or '')
    if not match or match.groups() != (str(offset), str(total - 1), str(total)):
        raise ValueError('archive_invalid_content_range')
    return offset


def _copy(response, path, *, offset, maximum, length, stop, progress, update=None):
    check_stop(stop)
    written = offset
    if progress:
        progress(written, length)
    with path.open('ab' if offset else 'wb') as output:
        for block in response.iter_content(_BLOCK):
            check_stop(stop)
            if written + len(block) > maximum:
                raise ValueError('archive_download_size_limit')
            output.write(block)
            written += len(block)
            if update:
                update(block)
            if progress and block:
                progress(written, length)
        if not written or (length is not None and written != length):
            raise ArchiveTransferError('历史归档响应未传完，等待重试。')
        output.flush()
        os.fsync(output.fileno())
    return written


def _proof(path, stop):
    digest, fingerprint = md5(usedforsecurity=False), sha256()

    def extend(block):
        digest.update(block)
        fingerprint.update(block)

    if path is not None:
        with path.open('rb') as source:
            for block in iter(lambda: source.read(_BLOCK), b''):
                check_stop(stop)
                extend(block)
    return extend, digest, fingerprint


def transfer_zip(get, url, destination, size, expected_md5, stop, progress):
    """Reuse a prefix across retries and commit only a matching official ZIP."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + '.part')
    for _ in range(2):
        check_stop(stop)
        offset = partial.stat().st_size if partial.is_file() else 0
        if offset > size:
            partial.unlink()
            offset = 0
        update, digest, fingerprint = _proof(partial if offset else None, stop)
        if offset < size:
            headers = {'Range': f'bytes={offset}-'} if offset else {}
            try:
                with _response(get, url, headers, stop) as response:
                    if response.status_code == 416 and offset:
                        partial.unlink()
                        continue
                    start = _range_offset(response, offset, size)
                    header_length = _length(response)
                    if header_length is not None and header_length != size - start:
                        raise ValueError('archive_checksum_or_size_mismatch')
                    if start == 0 and offset:
                        update, digest, fingerprint = _proof(None, stop)
                    offset = start
                    _copy(response, partial, offset=offset, maximum=size, length=size,
                          stop=stop, progress=progress, update=update)
            except ValueError:
                # Malformed range data never gets appended. Discard previous
                # data and give the full response one chance to succeed.
                partial.unlink(missing_ok=True)
                if offset:
                    continue
                raise
        if digest.hexdigest() != expected_md5:
            partial.unlink(missing_ok=True)
            if offset:
                continue
            raise ValueError('archive_checksum_or_size_mismatch')
        if offset == size:
            with partial.open('r+b') as source:
                os.fsync(source.fileno())
        if progress:
            progress(size, size)
        os.replace(partial, destination)
        sync_directory(destination.parent)
        return {'compressed_bytes': size, 'md5': digest.hexdigest(), 'sha256': fingerprint.hexdigest()}
    raise ValueError('archive_checksum_or_size_mismatch')


def _metadata(path, data):
    stat = data.stat()
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict) or value.get('mtime') != stat.st_mtime_ns or value.get('size') != stat.st_size:
            return {}
        return value
    except (OSError, ValueError):
        return {}


def _save_metadata(path, data, properties):
    stat = data.stat()
    with NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, prefix=path.name + '.',
                            delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump({**properties, 'mtime': stat.st_mtime_ns, 'size': stat.st_size}, output)
            output.close()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _catalog_identity(response, total):
    value = response.headers.get('ETag', '')
    return {'etag': value if _ETAG.fullmatch(value) else '', 'total': total}


def transfer_catalog(get, url, destination, maximum, stop, progress, valid):
    """Keep the last valid catalog and resume a known immutable representation."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + '.part')
    partial_meta = partial.with_name(partial.name + '.json')
    published_meta = destination.with_name(destination.name + '.json')
    cached = _metadata(published_meta, destination) if destination.is_file() else {}
    info = _metadata(partial_meta, partial) if partial.is_file() else {}
    offset = info.get('size', 0)
    total = info.get('total')
    if not (info.get('etag') and type(total) is int and 0 < offset < total <= maximum):
        offset = 0
        partial.unlink(missing_ok=True)
        partial_meta.unlink(missing_ok=True)
    for _ in range(2):
        headers = {'If-None-Match': cached['etag']} if cached.get('etag') else {}
        ranged = bool(offset)
        if ranged:
            headers = {'Range': f'bytes={offset}-', 'If-Range': info['etag']}
        try:
            with _response(get, url, headers, stop) as response:
                if response.status_code == 304:
                    if 'If-None-Match' not in headers:
                        raise ValueError('archive_unexpected_not_modified')
                    os.utime(destination, None)
                    _save_metadata(published_meta, destination, cached)
                    return
                start = _range_offset(response, offset, total)
                length = _length(response)
                expected = total if start else length
                if expected is not None and not 0 < expected <= maximum:
                    raise ValueError('archive_download_size_limit')
                if expected is None and response.headers.get('Transfer-Encoding', '').lower() != 'chunked':
                    raise ValueError('archive_unframed_catalog')
                identity = _catalog_identity(response, expected)
                if start and (identity['etag'] != info['etag'] or (length is not None and length != total - start)):
                    raise ValueError('archive_invalid_content_range')
                # The checkpoints are bound to the local file metadata and to
                # a strong server ETag. Without both, never stitch two downloads.
                if not start:
                    partial.unlink(missing_ok=True)
                    partial_meta.unlink(missing_ok=True)
                try:
                    _copy(response, partial, offset=start, maximum=expected or maximum,
                          length=expected, stop=stop, progress=progress)
                except (InterruptedError, ArchiveTransferError, requests.exceptions.RequestException):
                    if identity['etag'] and expected and partial.is_file() and 0 < partial.stat().st_size < expected:
                        _save_metadata(partial_meta, partial, identity)
                    else:
                        partial.unlink(missing_ok=True)
                        partial_meta.unlink(missing_ok=True)
                    raise
                if not valid(partial):
                    raise ValueError('archive_invalid_catalog')
                os.replace(partial, destination)
                if identity['etag']:
                    _save_metadata(published_meta, destination, identity)
                else:
                    published_meta.unlink(missing_ok=True)
                partial_meta.unlink(missing_ok=True)
                sync_directory(destination.parent)
                return
        except ValueError:
            partial.unlink(missing_ok=True)
            partial_meta.unlink(missing_ok=True)
            if not ranged:
                raise
            # A changed representation or a 416 requires a fresh response.
            offset = 0
