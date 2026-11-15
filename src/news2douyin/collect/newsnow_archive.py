"""Immutable NewsNow observations with content-addressed response bodies.

This helper performs filesystem work only; call it outside an SQL transaction.
An upstream-reported snapshot time is not a publication or verified fetch time.
Repeated cached response bodies share one file, while each collection run keeps
its own small observation manifest. Existing archives are never replaced.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any


MAX_SNAPSHOTS = 128
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_BATCH_BYTES = 32 * 1024 * 1024
MAX_MANIFEST_BYTES = 512 * 1024
MAX_STAGE_BYTES = 32 * 1024 * 1024
_SOURCE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}')
_HASH = re.compile(r'[0-9a-fA-F]{64}')


def _json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError('NewsNow archive metadata must be finite JSON data') from exc


def _contained(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise ValueError('NewsNow archive path must stay inside storage_root')
    return resolved


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _verify_existing(path: Path, body: bytes) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError('NewsNow archive target is not a regular file')
    if path.stat().st_size != len(body):
        raise ValueError('NewsNow existing archive checksum mismatch')
    if _digest(path.read_bytes()) != _digest(body):
        raise ValueError('NewsNow existing archive checksum mismatch')


def _create_only(path: Path, body: bytes, root: Path) -> bool:
    """Publish complete bytes with a create-only hard link, including on NTFS."""
    _contained(path, root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _contained(path, root)
    if path.exists() or path.is_symlink():
        _verify_existing(path, body)
        return False
    name = None
    try:
        with tempfile.NamedTemporaryFile(prefix='.newsnow-', dir=path.parent, delete=False) as output:
            name = Path(output.name)
            output.write(body)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(name, path)
        except FileExistsError:
            _verify_existing(path, body)
            return False
        return True
    finally:
        if name is not None:
            name.unlink(missing_ok=True)


def _strict_json(body: bytes):
    def invalid_constant(_value):
        raise ValueError('non-finite JSON constant')
    try:
        return True, json.loads(body, parse_constant=invalid_constant)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False, None


def _body(snapshot: dict) -> tuple[bytes | None, str, bool]:
    if snapshot.get('raw_body_b64') is not None:
        encoded = snapshot['raw_body_b64']
        if not isinstance(encoded, str) or len(encoded) > ((MAX_RESPONSE_BYTES + 2)//3)*4:
            raise ValueError('NewsNow response exceeds archive byte limit')
        try:
            body = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError('NewsNow raw response is not valid base64') from exc
        claimed = snapshot.get('raw_sha256')
        if claimed is not None and (not isinstance(claimed, str) or not _HASH.fullmatch(claimed)
                                    or claimed.lower() != _digest(body)):
            raise ValueError('NewsNow raw response checksum mismatch')
        valid_json, parsed = _strict_json(body)
        if 'payload' in snapshot and snapshot['payload'] is not None:
            if not valid_json or _json_bytes(parsed) != _json_bytes(snapshot['payload']):
                raise ValueError('NewsNow response payload does not match raw bytes')
        representation = 'http_response_bytes'
    elif 'payload' in snapshot and snapshot['payload'] is not None:
        body = _json_bytes(snapshot['payload'])
        claimed = snapshot.get('payload_sha256')
        if claimed is not None and (not isinstance(claimed, str) or not _HASH.fullmatch(claimed)
                                    or claimed.lower() != _digest(body)):
            raise ValueError('NewsNow canonical payload checksum mismatch')
        valid_json, representation = True, 'canonical_json'
    else:
        return None, 'no_response', False
    if len(body) > MAX_RESPONSE_BYTES:
        raise ValueError('NewsNow response exceeds archive byte limit')
    return body, representation, valid_json


def persist_batch(storage_root: str | Path, run_dir: str | Path, batch: dict) -> dict:
    """Save full source bodies and an immutable per-run manifest.

    Returns a compact JSON report suitable for diagnostics and checkpoints.
    ``snapshots`` in the result contains references, never raw bodies or items.
    A network failure can have no response body; its diagnostic still survives.
    A second call with the identical batch is safe, including concurrent calls.
    """
    root = Path(storage_root).resolve()
    run = _contained(Path(run_dir), root)
    manifest = run/'newsnow-snapshots.json'
    snapshots = batch.get('snapshots', [])
    if not isinstance(snapshots, list) or len(snapshots) > MAX_SNAPSHOTS:
        raise ValueError('NewsNow snapshot count exceeds archive limit')
    if not isinstance(batch.get('summary', {}), dict) or not isinstance(batch.get('sources', []), list):
        raise ValueError('NewsNow batch diagnostics must be JSON objects')
    prepared = []
    total = 0
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            raise ValueError('NewsNow snapshot must be an object')
        source = snapshot.get('source_id')
        if not isinstance(source, str) or not _SOURCE.fullmatch(source):
            raise ValueError('NewsNow snapshot source identifier is invalid')
        body, representation, valid_json = _body(snapshot)
        total += len(body) if body is not None else 0
        if total > MAX_BATCH_BYTES:
            raise ValueError('NewsNow batch exceeds archive byte limit')
        digest = _digest(body) if body is not None else ''
        suffix = '.json' if valid_json else '.body'
        target = root/'newsnow'/'snapshots'/(digest+suffix) if digest else None
        reference = target.relative_to(root).as_posix() if target else ''
        row = dict(source_id=source,
            observed_at=snapshot.get('observed_at', ''),
            response_status=snapshot.get('response_status', ''),
            snapshot_reported_at=snapshot.get('snapshot_reported_at', ''),
            snapshot_time_basis=snapshot.get('snapshot_time_basis', 'upstream_reported'),
            representation=representation, archive_ref=reference, archive_sha256=digest,
            archive_bytes=len(body) if body is not None else 0,
            content_type=snapshot.get('content_type', ''), json_valid=valid_json)
        prepared.append((target, body, row))
    report = dict(schema_version=1, summary=batch.get('summary', {}), sources=batch.get('sources', []),
        snapshots=[row for _path, _body, row in prepared],
        manifest_path=manifest.relative_to(root).as_posix(),
        note='每轮观察保留；快照报告时间不是发布时间，也不保证是上游实际抓取时间。')
    manifest_body = _json_bytes(report)+b'\n'
    if len(manifest_body) > MAX_MANIFEST_BYTES:
        raise ValueError('NewsNow manifest exceeds archive byte limit')
    # Validate everything before publishing any new file. A later I/O failure
    # may leave a verified, unreferenced content blob; evidence is never erased.
    for target, body, _row in prepared:
        if target is not None:
            _contained(target, root)
            if target.exists() or target.is_symlink():
                _verify_existing(target, body)
    if manifest.exists() or manifest.is_symlink():
        _verify_existing(manifest, manifest_body)
    for target, body, _row in prepared:
        if target is not None:_create_only(target, body, root)
    _create_only(manifest, manifest_body, root)
    return report


def _stage_count(value: Any) -> int:
    rows = value if isinstance(value, list) else value.get('items') if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise ValueError('NewsNow stage must contain an items list')
    return len(rows)


def save_stage(storage_root: str | Path, run_dir: str | Path, name: str, value: Any) -> dict:
    """Put a large frozen stage in the archive; SQL keeps only its small pointer."""
    if name not in ('raw', 'filtered'):
        raise ValueError('NewsNow stage name must be raw or filtered')
    root = Path(storage_root).resolve()
    run = _contained(Path(run_dir), root)
    path = run/('newsnow-stage-'+name+'.json')
    count = _stage_count(value)
    body = _json_bytes(value)+b'\n'
    if len(body) > MAX_STAGE_BYTES:
        raise ValueError('NewsNow stage exceeds archive byte limit')
    _create_only(path, body, root)
    return dict(schema='newsnow_stage_v1', path=path.relative_to(root).as_posix(),
                sha256=_digest(body), bytes=len(body), count=count, stage=name)


def load_stage(storage_root: str | Path, reference: dict) -> Any:
    """Read exactly the frozen stage, refusing missing/corrupt/repointed data."""
    if not isinstance(reference, dict) or reference.get('schema') != 'newsnow_stage_v1':
        raise ValueError('NewsNow stage pointer has an unknown schema')
    name = reference.get('stage')
    relative = reference.get('path')
    expected = reference.get('sha256')
    size = reference.get('bytes')
    count = reference.get('count')
    if (name not in ('raw', 'filtered') or not isinstance(relative, str)
        or not relative or '\\' in relative or Path(relative).is_absolute()
        or '..' in Path(relative).parts or not isinstance(expected, str)
        or not _HASH.fullmatch(expected) or type(size) is not int
        or not 0 <= size <= MAX_STAGE_BYTES or type(count) is not int or count < 0):
        raise ValueError('NewsNow stage pointer is invalid')
    root = Path(storage_root).resolve()
    raw_path = root/relative
    path = _contained(raw_path, root)
    if path.name != 'newsnow-stage-'+name+'.json':
        raise ValueError('NewsNow stage pointer has an invalid filename')
    if raw_path.is_symlink() or not path.is_file():
        raise ValueError('NewsNow frozen stage is missing or not a regular file')
    if path.stat().st_size != size:
        raise ValueError('NewsNow frozen stage checksum mismatch')
    body = path.read_bytes()
    if _digest(body) != expected.lower():
        raise ValueError('NewsNow frozen stage checksum mismatch')
    valid, value = _strict_json(body)
    if not valid or _stage_count(value) != count:
        raise ValueError('NewsNow frozen stage content is invalid')
    return value
