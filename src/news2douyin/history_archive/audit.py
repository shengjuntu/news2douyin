"""Match the archive ledger to the mounted volume and immutable Parquet outputs."""
from collections import defaultdict
from contextlib import closing
import os
import re
from uuid import uuid4

from sqlalchemy import func, update
from sqlmodel import Session, select

from . import convert
from .models import HistoryGroup, HistoryPart, HistoryPublication, HistoryPublicationCheck, HistoryVolume


class VolumeUnavailable(OSError):
    pass


def confirm_volume(worker):
    try:
        value = (worker.root / '.news2douyin-volume').read_text(encoding='ascii').strip()
    except (OSError, UnicodeError):
        raise VolumeUnavailable('历史归档卷暂不可用，请核对挂载目录。') from None
    if value != worker.volume_token:
        raise VolumeUnavailable('历史归档卷标识不一致，请核对挂载目录。')


def bind_volume(worker):
    """Never rebuild a registered archive on an empty replacement mount."""
    if worker.volume_token:
        confirm_volume(worker)
        return
    marker = worker.root / '.news2douyin-volume'
    with Session(worker.engine) as session:
        volume = session.get(HistoryVolume, 'archive')
        token = volume.token if volume else ''
        legacy = (not token and session.exec(select(HistoryPublication.path).limit(1)).first() is not None)
    try:
        actual = marker.read_text(encoding='ascii').strip()
    except FileNotFoundError:
        if token:
            raise VolumeUnavailable('历史归档卷暂不可用，请核对挂载目录。') from None
        if legacy:
            # Existing 0.55 volumes have no marker. A surviving committed
            # batch directory identifies the populated layout even if its file was lost.
            with Session(worker.engine) as session:
                statement = select(HistoryPublication.group_key, HistoryPublication.path).execution_options(yield_per=64)
                with closing(session.exec(statement)) as rows:
                    if not any((p := _safe_batch(worker.root, rel, key)) and p.parent.is_dir() for key, rel in rows):
                        raise VolumeUnavailable('未找到原归档卷的已发布目录，请核对挂载后继续。') from None
        worker.root.mkdir(parents=True, exist_ok=True)
        actual = uuid4().hex
        temporary = marker.with_suffix('.tmp')
        temporary.write_text(actual, encoding='ascii')
        convert._synced(temporary)
        os.replace(temporary, marker)
        convert._directory_synced(worker.root)
    except UnicodeError:
        raise VolumeUnavailable('历史归档卷标识无法读取，请核对挂载目录。') from None
    if not re.fullmatch(r'[a-f0-9]{32}', actual) or (token and actual != token):
        raise VolumeUnavailable('历史归档卷标识不一致，请核对挂载目录。')
    if not token:
        with worker._write() as session:
            session.add(HistoryVolume(token=actual))
    worker.volume_token = actual


def file_signature(path):
    # ctime includes permission changes. A relocated copy is reverified once.
    def stat_value(file):
        stat = file.stat()
        return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    bad_rows = path.with_name('invalid.jsonl.gz')
    try:
        sidecar = stat_value(bad_rows)
    except FileNotFoundError:
        sidecar = None
    return repr((stat_value(path), stat_value(path.with_name('manifest.json')), sidecar))


def _safe_batch(root, relative, key):
    version, kind, period = key.split('/')
    base = root / 'parquet' / ('v' + version) / kind / ('archive_year=' + period[:4]) / (
        'archive_month=' + (period[4:6] or '00')) / ('archive_period=' + period)
    path = root / relative
    if (path.name != 'data.parquet' or path.parent.parent != base
            or not re.fullmatch(r'batch-[a-f0-9]{64}', path.parent.name) or path.resolve() != path):
        return None
    return path


def reconcile(worker, keys):
    """Audit a page of groups outside write transactions. Repair only their affected packages."""
    with Session(worker.engine) as session:
        rows = session.exec(select(HistoryPublication.group_key, HistoryPublication.path,
            HistoryPublicationCheck.file_signature, HistoryPublicationCheck.invalid_rows)
            .outerjoin(HistoryPublicationCheck, HistoryPublicationCheck.path == HistoryPublication.path)
            .where(HistoryPublication.group_key.in_(keys))).all()
        ready = session.exec(select(HistoryPart.group_key, HistoryPart.publication_path,
            func.count(), func.sum(HistoryPart.size)).where(
                HistoryPart.group_key.in_(keys), HistoryPart.state == 'ready')
            .group_by(HistoryPart.group_key, HistoryPart.publication_path)).all()

    valid, invalid = {}, defaultdict(set)
    verified = []
    for key, relative, cached, count in rows:
        convert._stopping(worker.is_set)
        path = _safe_batch(worker.root, relative, key)
        if path is None:
            invalid[key].add(relative)
            continue
        try:
            signature = file_signature(path)
            if cached != signature:
                version, kind, period = key.split('/')
                checked = convert._read_published(path.parent,
                    {'dataset': kind, 'upstream_version': int(version), 'group': period}, stop=worker.is_set)
                count = checked.invalid_rows
                verified.append(HistoryPublicationCheck(path=relative, file_signature=signature, invalid_rows=count))
            valid[key, relative] = count
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError, PermissionError, convert.ConversionError,
                ValueError, TypeError, KeyError):
            invalid[key].add(relative)
    missing = defaultdict(list)
    for key, relative, files, size in ready:
        if (key, relative) not in valid:
            missing[key].append((relative, files, size))
    if not (verified or invalid or missing):
        return False

    bind_volume(worker)
    convert._stopping(worker.is_set)
    for key, paths in invalid.items():
        for relative in paths:
            path = _safe_batch(worker.root, relative, key)
            if path and path.parent.is_dir():
                # A deterministic batch name is reused on rebuild. Move the damaged
                # output aside without touching its healthy siblings or any input ZIP.
                quarantine = worker.root / '.quarantine'
                quarantine.mkdir(exist_ok=True)
                os.replace(path.parent, quarantine / (path.parent.name + '-' + uuid4().hex))
    bind_volume(worker)
    with worker._write() as session:
        for item in verified:
            session.merge(item)
        for key, paths in invalid.items():
            for relative in paths:
                receipt = session.get(HistoryPublicationCheck, relative)
                if receipt:
                    session.delete(receipt)
                publication = session.get(HistoryPublication, relative)
                if publication:
                    session.delete(publication)
        for key, affected in missing.items():
            group = session.get(HistoryGroup, key)
            session.exec(update(HistoryPart).where(HistoryPart.group_key == key,
                HistoryPart.state == 'ready', HistoryPart.publication_path.in_([row[0] for row in affected])
                ).values(state='pending', publication_path='', error=''))
            files = sum(n for _, n, _ in affected)
            size = sum(n for _, _, n in affected)
            group.ready_files = max(0, group.ready_files - files)
            group.downloaded_files = max(0, group.downloaded_files - files)
            group.ready_bytes = max(0, group.ready_bytes - size)
            group.downloaded_bytes = max(0, group.downloaded_bytes - size)
            group.invalid_rows = sum(count for (owner, _), count in valid.items() if owner == key)
            group.state, group.phase, group.retry_at = 'pending', '', 0
            group.error = '已发布分区缺失或损坏，正在重建。'
            session.add(group)
    return bool(missing)
