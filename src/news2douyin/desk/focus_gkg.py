"""Check stored GKG for exact focus report URLs still missing SQL evidence."""

from datetime import date, timedelta
import logging
from threading import Timer
from zlib import error as ZlibError

from ..intake.lake import Lake
from .official_graph import _document_subjects

log = logging.getLogger(__name__)

_LOOKBACK_DAYS = 7
_QUERY_SECONDS = 2.0
_QUERY = """
SELECT DISTINCT ON (r.url) r.*, a.sha256 AS archive_sha256
FROM intake_records_v2 r
JOIN intake_archives a ON a.feed=r.feed AND a.stamp=r.stamp
WHERE r.feed IN ('gkg_english','gkg_translation')
  AND r.stamp>=? AND r.stamp<=? AND r.valid AND r.payload IS NOT NULL
  AND r.url IN (SELECT unnest(?::VARCHAR[]))
ORDER BY r.url, r.stamp DESC, r.feed, r.row_index DESC
"""


def _archive_window(mentions, start, end):
    latest = None
    for mention in mentions:
        for stamp in (mention.first_stamp, mention.last_stamp):
            if stamp and len(stamp) == 14 and stamp.isascii() and stamp.isdigit():
                try:
                    day = date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
                except ValueError:
                    continue
                if start <= day <= end and (latest is None or day > latest):
                    latest = day
    stop = latest + timedelta(days=1) if latest and latest < end else end
    first = stop - timedelta(days=min(_LOOKBACK_DAYS - 1, (stop - date.min).days))
    return first, stop


def archive_subjects(storage_root, identifiers, mentions, start, end):
    """Return verified rows, provenance and bounded lookup status."""
    if storage_root is None or not identifiers:
        return {}, {}, {}
    from duckdb import Error, InterruptException

    first, last = _archive_window(mentions, date.fromisoformat(start), date.fromisoformat(end))
    lower = first.isoformat().replace('-', '') + '000000'
    upper = last.isoformat().replace('-', '') + '235959'
    status = dict(state='checked', checked_urls=len(identifiers), date_from=first.isoformat(),
                  date_to=last.isoformat(), matched_urls=0)
    lake = Lake(storage_root)
    try:
        with lake.connection(read_only=True) as connection:
            if connection is None or not {'intake_archives','intake_records_v2'} <= lake.tables(connection):
                return {}, {}, dict(status, state='unavailable')
            timeout = Timer(_QUERY_SECONDS, connection.interrupt)
            timeout.daemon = True
            timeout.start()
            try:
                cursor = connection.execute(_QUERY, [lower, upper, sorted(identifiers)])
                names = [item[0] for item in cursor.description]
                rows = cursor.fetchall()
            finally:
                timeout.cancel()
                timeout.join()
    except InterruptException:
        return {}, {}, dict(status, state='timed_out')
    except (Error, OSError):
        log.warning('Focus GKG archive lookup failed', exc_info=True)
        return {}, {}, dict(status, state='unavailable')

    matched, evidence = {}, {}
    for values in rows:
        packed = dict(zip(names, values))
        try:
            row = Lake.material(packed)
        except (ValueError, TypeError, ZlibError):
            log.warning('Could not verify archived GKG row %s/%s/%s',
                        packed['feed'], packed['stamp'], packed['row_index'])
            continue
        metadata = row['metadata']
        subjects, more = _document_subjects(metadata.get('persons'), metadata.get('orgs'))
        url = row['url']
        matched[url] = ([row['record_id'], row['feed'], row['stamp'], row['row_index'], more], subjects)
        evidence[url] = dict(gkg_source='archive', archive_sha256=packed['archive_sha256'],
                             raw_sha256=packed['raw_sha256'])
    status['matched_urls'] = len(matched)
    return matched, evidence, status
