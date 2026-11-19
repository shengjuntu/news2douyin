"""Read immutable GKG headline observations already received by the local archive."""
from datetime import date, datetime, timezone
from pathlib import Path
import re
from urllib.parse import urlsplit
from .calendar import day_bounds_utc

from ..dedup.normalize import canonicalize_url
from ..intake.lake import Lake
from ..selection.source_content import source_url, text, usable_title

PAGE_SIZE = 4096


def iter_gkg(storage_root: str | Path, day: date, *, stats=None, cutoff=None, checkpoint=None, read_only=False, restored_stamps=(), coarse=False, since=None, receipts=False):
    """Scan narrow columns in small slices, releasing the intake lock before yielding.

    Archive completion time is the observation clock. A backfilled batch belongs
    to the day we received it except for explicitly assigned repair batches. Source batch stamps are not used
    as substitutes for local availability.
    """
    from .ranking import Candidate

    counts = stats if stats is not None else {}
    counts.update(gkg_archives=0, gkg_seen=0, gkg_invalid=0,
                  gkg_no_readable=0, gkg_emitted=0, gkg_archive_present=False)
    if coarse:
        from . import coarse as coarse_fields
        counts.update(gkg_coarse_metadata_read=0, gkg_coarse_metadata_unavailable=0,
                      gkg_coarse_genre=0, gkg_coarse_category=0, gkg_coarse_mention_country=0)
    start, end = day_bounds_utc(day)
    if since is not None:
        if since.tzinfo is None:
            raise ValueError('since must include a timezone')
        start = max(start, since.astimezone(timezone.utc))
    if cutoff is not None:
        dt = datetime.fromisoformat(cutoff.replace("Z", "+00:00")) if isinstance(cutoff, str) else cutoff
        if dt.tzinfo is None:
            raise ValueError("cutoff must include a timezone")
        end = min(end, dt.astimezone(timezone.utc))

    def timestamp(moment):
        return moment.isoformat(timespec="seconds").replace("+00:00", "Z")

    lake = Lake(storage_root)
    with lake.connection(read_only=read_only) as db:
        if db is None:
            if restored_stamps:
                raise RuntimeError('historical repair archive no longer present')
            return
        counts["gkg_archive_present"] = True
        tables = lake.tables(db)
        available = [name for name in ("intake_records_v2", "intake_records") if name in tables]
        if not available or "intake_archives" not in tables:
            raise RuntimeError("GKG archive schema is incomplete")
        scope = ''
        if not receipts and 'intake_digest_repairs' in tables:
            scope = (" AND NOT EXISTS (SELECT 1 FROM intake_digest_repairs r "
                     "WHERE r.feed=a.feed AND r.stamp=a.stamp AND r.target_day<>?)")
        args = [timestamp(start), timestamp(end)]
        if scope:
            args.append(day.isoformat())
        query = ("SELECT a.feed,a.stamp,a.rows,a.completed_at FROM intake_archives a "
                 "WHERE a.feed IN ('gkg_english','gkg_translation') ")
        batches = db.execute(query + 'AND completed_at >= ? AND completed_at < ?' + scope
                             + ' ORDER BY a.feed,a.stamp', args).fetchall()
        if restored_stamps:
            # Only exact slots acknowledged by this date's background repair may
            # cross the historical observation cutoff. Keep real completion times.
            allowed = sorted(set(restored_stamps))
            if len(allowed) > 192:
                raise ValueError('too many GKG time slots on a repair date')
            bounds = day_bounds_utc(day)
            for feed, stamp in allowed:
                observed = datetime.strptime(stamp,'%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
                if feed not in ('gkg_english','gkg_translation') or not bounds[0] <= observed < bounds[1]:
                    raise ValueError('repair slot is outside target day')
            placeholders = ','.join('(?,?)' for _ in allowed)
            requested = db.execute(query + f'AND (a.feed,a.stamp) IN (VALUES {placeholders})',
                                   [item for entry in allowed for item in entry]).fetchall()
            if len(requested) != len(allowed):
                raise RuntimeError('historical repair archive no longer present')
            seen = {(row[0],row[1]) for row in batches}
            late = [row for row in requested if (row[0],row[1]) not in seen]
            counts['gkg_restored_archives'] = len(late)
            counts['gkg_restored_rows'] = sum(row[2] for row in late)
            counts['gkg_restored_batches'] = {f'{row[0]}:{row[1]}':row[3] for row in late}
            batches = sorted(batches + late)
    counts["gkg_archives"] = len(batches)
    restored_batches = counts.get('gkg_restored_batches') or {}
    for feed, stamp, size, observed_at in batches:
        source_clock = dict(repair_day=day.isoformat()) if f'{feed}:{stamp}' in restored_batches else None
        chunk_size = min(PAGE_SIZE, coarse_fields.PAGE_SIZE) if coarse else PAGE_SIZE
        for first in range(1, size + 1, chunk_size):
            if checkpoint is not None:
                checkpoint()
            # Valid legacy and v2 archives share these narrow columns; during
            # conversion prefer the v2 record for the same immutable row key.
            stop = min(size + 1, first + chunk_size)
            rows = {}
            with lake.connection(read_only=read_only) as db:
                if db is None:
                    raise RuntimeError("GKG archive disappeared during daily build")
                for table in available:
                    extra = coarse_fields.projection(table) if coarse else ''
                    chunk = db.execute(
                        f"SELECT row_index, left(title, 1000), left(url, 16001), valid{extra} FROM {table} "
                        "WHERE feed=? AND stamp=? AND row_index>=? AND row_index<?",
                        [feed, stamp, first, stop],
                    ).fetchall()
                    for row in chunk:
                        rows.setdefault(row[0], row)
            if len(rows) != stop - first:
                raise RuntimeError(f"GKG archive manifest mismatch for {feed}/{stamp}")
            for idx in sorted(rows):
                row = rows[idx]
                title, url, valid = row[1:4]
                counts["gkg_seen"] += 1
                if not valid:
                    counts["gkg_invalid"] += 1
                    continue
                url = source_url(url)
                if not url or "\\" in url or not usable_title(title):
                    counts["gkg_no_readable"] += 1
                    continue
                counts["gkg_emitted"] += 1
                headline = text(title, 500)
                hints = coarse_fields.classify(headline, row[4:], counts) if coarse else None
                yield Candidate(key=f"gkg:{feed}:{stamp}:{idx}", title=headline,
                                observed_at=observed_at, source_url=url, source_kind="gkg", source_clock=source_clock,
                                evidence_key=canonicalize_url(url), domains=(urlsplit(url).hostname,), coarse=hints)


def read_gkg_reference(storage_root, key, *, cutoff=''):
    """Resolve one exact GKG observation with its immutable batch provenance."""
    match = re.fullmatch(r'gkg:(gkg_english|gkg_translation):(\d{14}):([1-9]\d{0,8})', key)
    if match is None:
        raise KeyError('未找到可用的来源候选')
    feed, stamp, index = match.groups()
    index = int(index)
    from ..intake.lake import validate_key
    try:
        validate_key(feed, stamp)
    except ValueError:
        raise KeyError('未找到可用的来源候选') from None
    lake = Lake(storage_root)
    with lake.connection() as db:
        if db is None:
            raise KeyError('未找到可用的来源候选')
        tables = lake.tables(db)
        if 'intake_archives' not in tables:
            raise KeyError('未找到可用的来源候选')
        record = db.execute('SELECT completed_at,sha256,rows FROM intake_archives WHERE feed=? AND stamp=?',
                            (feed, stamp)).fetchone()
        if record is None or not record[0] or not 1 <= index <= record[2] or (cutoff and record[0] >= cutoff):
            raise KeyError('未找到可用的来源候选')
        for name in ('intake_records_v2', 'intake_records'):
            if name not in tables:
                continue
            raw_column = 'payload' if name.endswith('_v2') else 'metadata_json'
            row = db.execute(f'SELECT title,url,valid,{raw_column} IS NOT NULL FROM {name} '
                             'WHERE feed=? AND stamp=? AND row_index=?', (feed, stamp, index)).fetchone()
            if row is not None:
                break
        else:
            raise KeyError('未找到可用的来源候选')
    title, url, valid, raw_present = row
    url = source_url(url)
    if not valid or not url or "\\" in url or not usable_title(title):
        raise KeyError('未找到可用的来源候选')
    language = ''
    if raw_present:
        observation, = lake.rows(feed, stamp, after=index - 1, limit=1)
        if observation['row_index'] != index:
            raise KeyError('未找到可用的来源候选')
        language = (observation.get('metadata') or {}).get('source_language') or ''
    observed, archive_sha, _ = record
    reference = dict(provider='gkg', feed=feed, stamp=stamp, row_index=index, archive_sha256=archive_sha or '',
                     source_language=language)
    return dict(title=text(title, 500), url=url, observed_at=observed, source_language=language,
                source_ref=reference)
