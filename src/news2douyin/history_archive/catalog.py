"""Enumerate official GDELT archives and download from checksum manifests."""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import md5, sha256
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from threading import Lock
from time import time

import requests

from ..intake.service import response_error, timeout

HOST = 'https://data.gdeltproject.org'
START_DATE = date(1979, 1, 1)
GKG_START = date(2013, 4, 1)
V1_LAST_DAY = date(2015, 2, 18)
V2_FIRST_STAMP = '20150218230000'
KINDS = ('events', 'gkg', 'mentions')

_SUFFIXES = {
    'export.CSV.zip': 'events',
    'gkg.csv.zip': 'gkg',
    'mentions.CSV.zip': 'mentions',
}
_V2_URL = re.compile(
    rb'https?://data\.gdeltproject\.org/gdeltv2/(\d{14})\.(export\.CSV\.zip|gkg\.csv\.zip|mentions\.CSV\.zip)'
)
_V1_EVENTS = re.compile(r'(\d{4}|\d{6})\.zip|(\d{8})\.export\.CSV\.zip')
_V1_GKG = re.compile(r'(\d{8})\.gkg\.csv\.zip')
_MD5 = re.compile(r'[0-9a-fA-F]{32}')
_CATALOG_LOCK = Lock()
_MASTER = 'gdeltv2/masterfilelist.txt'
_MASTER_LIMIT = 256 * 1024 ** 2
_LEGACY_LIMIT = 4 * 1024 ** 2
_LEGACY_TTL = 24 * 60 * 60
_MASTER_TTL = 60 * 60
RELAY_CATALOGS = (_MASTER, 'events/filesizes', 'events/md5sums', 'gkg/filesizes', 'gkg/md5sums')


class RelayCatalogNeeded(Exception):
    """The local downloader has not handed off a current catalog yet."""



@dataclass(frozen=True)
class ArchivePackage:
    name: str
    kind: str
    version: int
    date_from: date
    date_to: date
    stamp: str
    url: str
    size: int
    md5: str


def _check_stop(stop):
    if stop is not None and stop.is_set():
        raise InterruptedError('historical archive task stopped')


def _sync_directory(path):
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


def _transfer(relative, destination, max_bytes, stop, *, package=None):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    received = 0
    digest = md5(usedforsecurity=False) if package else None
    fingerprint = sha256() if package else None
    try:
        with NamedTemporaryFile(dir=destination.parent, prefix=destination.name + '.', suffix='.part',
                                delete=False) as output:
            temporary = Path(output.name)
            _check_stop(stop)
            with requests.get(f'{HOST}/{relative}', stream=True, timeout=timeout(),
                              headers={'Accept-Encoding': 'identity'}, allow_redirects=False) as response:
                response_error(response)
                for block in response.iter_content(128 * 1024):
                    _check_stop(stop)
                    received += len(block)
                    if received > max_bytes:
                        raise ValueError('archive_download_size_limit')
                    output.write(block)
                    if digest is not None:
                        digest.update(block)
                        fingerprint.update(block)
            if not received:
                raise ValueError('archive_empty_response')
            if package and (received != package.size or digest.hexdigest() != package.md5.lower()):
                raise ValueError('archive_checksum_or_size_mismatch')
            output.flush()
            os.fsync(output.fileno())
        if package is None and not _looks_like_catalog(temporary, relative):
            raise ValueError('archive_invalid_catalog')
        os.replace(temporary, destination)
        _sync_directory(destination.parent)
        return {'compressed_bytes': received, 'md5': digest.hexdigest(), 'sha256': fingerprint.hexdigest()} if package else None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _looks_like_catalog(path, relative):
    with path.open('rb') as source:
        for _ in range(16):
            entry = source.readline(4096).split()
            if relative == _MASTER:
                if len(entry) == 3 and entry[0].isdigit() and _V2_URL.fullmatch(entry[2]):
                    return True
            elif len(entry) == 2:
                filename = entry[1].decode('ascii', errors='replace')
                if _legacy_period('events' if relative.startswith('events/') else 'gkg', filename):
                    token = entry[0].decode('ascii', errors='replace')
                    valid = token.isdigit() if relative.endswith('filesizes') else _MD5.fullmatch(token)
                    if valid:
                        return True
    return False


def _fresh(target, relative, archive_end):
    recent = archive_end is not None and archive_end >= datetime.now(timezone.utc).date() - timedelta(days=1)
    ttl = _MASTER_TTL if relative == _MASTER and recent else _LEGACY_TTL
    try:
        return time() - target.stat().st_mtime <= ttl
    except FileNotFoundError:
        return False


def needed_relay_catalogs(start, end, cache_dir):
    """Report exactly the catalogs used by Events/GKG planning for this date range."""
    required = []
    if end >= V1_LAST_DAY:
        required.append(_MASTER)
    if start <= V1_LAST_DAY:
        required.extend(RELAY_CATALOGS[1:3])
        if end >= GKG_START:
            required.extend(RELAY_CATALOGS[3:])
    return [relative for relative in required
            if not _fresh(Path(cache_dir) / relative.replace('/', '-'), relative, end)]


def _catalog(relative, cache_dir, stop, *, archive_end=None, local_only=False):
    target = Path(cache_dir) / relative.replace('/', '-')
    limit = _MASTER_LIMIT if relative == _MASTER else _LEGACY_LIMIT
    with _CATALOG_LOCK:
        _check_stop(stop)
        if not _fresh(target, relative, archive_end):
            if local_only:
                raise RelayCatalogNeeded(relative)
            _transfer(relative, target, limit, stop)
    return target


def _legacy_period(kind, name):
    matching = (_V1_EVENTS if kind == 'events' else _V1_GKG).fullmatch(name)
    if matching is None:
        return None
    stamp = next(group for group in matching.groups() if group is not None)
    if len(stamp) == 4:
        year = int(stamp)
        if not 1979 <= year <= 2005:
            return None
        return stamp, date(year, 1, 1), date(year, 12, 31)
    if len(stamp) == 6:
        year, month = int(stamp[:4]), int(stamp[4:])
        if not '200601' <= stamp <= '201303' or not 1 <= month <= 12:
            return None
        return stamp, date(year, month, 1), date(year, month, monthrange(year, month)[1])
    try:
        day = date.fromisoformat(f'{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}')
    except ValueError:
        return None
    if not GKG_START <= day <= V1_LAST_DAY:
        return None
    return stamp, day, day


def _legacy_values(path, periods, *, checksum=False):
    values = {}
    with path.open(encoding='ascii', errors='replace') as stream:
        for line in stream:
            parts = line.split()
            if len(parts) != 2:
                continue
            value, name = parts
            if name not in periods:
                continue
            if checksum:
                if not _MD5.fullmatch(value):
                    continue
                value = value.lower()
            else:
                if not value.isdigit() or int(value) <= 0:
                    continue
                value = int(value)
            if name in values and values[name] != value:
                raise ValueError(f'conflicting GDELT catalog entries for {name}')
            values[name] = value
    return values


def _legacy_packages(kind, lower, upper, cache_dir, stop, *, local_only=False):
    sizes_path = _catalog(f'{kind}/filesizes', cache_dir, stop, local_only=local_only)
    checksums_path = _catalog(f'{kind}/md5sums', cache_dir, stop, local_only=local_only)
    periods = {}
    with sizes_path.open(encoding='ascii', errors='replace') as stream:
        for line in stream:
            parts = line.split()
            if len(parts) != 2:
                continue
            name = parts[1]
            period = _legacy_period(kind, name)
            if period is not None and period[1] <= upper and period[2] >= lower:
                periods[name] = period
    sizes = _legacy_values(sizes_path, periods)
    checksums = _legacy_values(checksums_path, periods, checksum=True)
    for name, (stamp, start, end) in periods.items():
        _check_stop(stop)
        if name not in sizes or name not in checksums:
            raise ValueError(f'missing GDELT checksum or size for {name}')
        yield ArchivePackage(name, kind, 1, start, end, stamp, f'{HOST}/{kind}/{name}',
                             sizes[name], checksums[name])


def _modern_packages(path, kinds, lower, upper, stop):
    first_day = lower.strftime('%Y%m%d').encode('ascii')
    last_day = upper.strftime('%Y%m%d').encode('ascii')
    seen = {}
    with path.open('rb') as stream:
        for index, line in enumerate(stream):
            if index % 8192 == 0:
                _check_stop(stop)
            parts = line.split()
            if len(parts) != 3:
                continue
            size, checksum, url = parts
            match = _V2_URL.fullmatch(url)
            if match is None:
                continue
            batch, extension = match.groups()
            if not first_day <= batch[:8] <= last_day or batch < V2_FIRST_STAMP.encode('ascii'):
                continue
            kind = _SUFFIXES[extension.decode('ascii')]
            if kind not in kinds:
                continue
            digest = checksum.decode('ascii', errors='replace').lower()
            if not size.isdigit() or int(size) <= 0 or not _MD5.fullmatch(digest):
                raise ValueError(f'invalid GDELT checksum or size for {batch.decode()}')
            try:
                born = datetime.strptime(batch.decode('ascii'), '%Y%m%d%H%M%S')
            except ValueError:
                continue
            if born.minute % 15 or born.second:
                continue
            day = born.date()
            stamp = batch.decode('ascii')
            name = url.rsplit(b'/', 1)[-1].decode('ascii')
            key = (kind, stamp)
            metadata = (int(size), digest)
            if key in seen:
                if seen[key] != metadata:
                    raise ValueError(f'conflicting GDELT catalog entries for {name}')
                continue
            seen[key] = metadata
            yield ArchivePackage(name, kind, 2, day, day, stamp, f'{HOST}/gdeltv2/{name}', *metadata)


def list_packages(date_from, date_to, kinds, *, cache_dir, stop=None, local_only=False):
    """Return available archives intersecting the inclusive UTC date interval."""
    requested = frozenset(kinds)
    if not requested or not requested.issubset(KINDS):
        raise ValueError('invalid GDELT archive data type')
    if type(date_from) is not date or type(date_to) is not date or not START_DATE <= date_from <= date_to:
        raise ValueError('invalid GDELT history date range')
    packages = []
    if date_to >= V1_LAST_DAY:
        master = _catalog(_MASTER, cache_dir, stop, archive_end=date_to, local_only=local_only)
        packages.extend(_modern_packages(master, requested, date_from, date_to, stop))
    if date_from <= V1_LAST_DAY:
        for kind in KINDS[:2]:
            start = START_DATE if kind == 'events' else GKG_START
            if kind in requested and date_to >= start:
                packages.extend(_legacy_packages(kind, date_from, date_to, cache_dir, stop, local_only=local_only))
    order = {kind: len(KINDS) - index for index, kind in enumerate(KINDS)}
    return sorted(packages, key=lambda item: (item.date_to, item.version, item.stamp, order[item.kind]), reverse=True)


def download_package(package, destination, *, stop=None, max_bytes=None):
    """Download a catalog entry, checking its bytes and MD5 before publishing."""
    if package.kind not in KINDS or package.size <= 0 or not _MD5.fullmatch(package.md5):
        raise ValueError('invalid GDELT archive metadata')
    if package.version == 2:
        match = _V2_URL.fullmatch(package.url.encode('ascii'))
        if match is None or _SUFFIXES[match[2].decode('ascii')] != package.kind or match[1].decode('ascii') != package.stamp:
            raise ValueError('invalid GDELT archive URL')
        relative = f'gdeltv2/{package.name}'
    elif package.version == 1 and package.kind != 'mentions':
        period = _legacy_period(package.kind, package.name)
        if period is None or period != (package.stamp, package.date_from, package.date_to):
            raise ValueError('invalid GDELT archive name')
        relative = f'{package.kind}/{package.name}'
    else:
        raise ValueError('invalid GDELT archive version')
    if package.url != f'{HOST}/{relative}' or not re.fullmatch(r'[0-9.\-A-Za-z]+', package.name):
        raise ValueError('invalid GDELT archive URL')
    maximum = package.size if max_bytes is None else min(max_bytes, package.size)
    if package.size > maximum:
        raise ValueError('archive_download_size_limit')
    return _transfer(relative, Path(destination), maximum, stop, package=package)
