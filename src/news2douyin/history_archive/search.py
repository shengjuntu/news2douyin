"""Find source leads using committed, date-scoped GDELT Parquet groups."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta, timezone

from .audit import file_signature
from .paths import archive_root
from tempfile import TemporaryDirectory
from threading import Timer


FIRST_DAY = {'events': date(1979, 1, 1), 'gkg': date(2013, 4, 1), 'mentions': date(2015, 2, 18)}
DEFAULT_SECONDS = 90

# All timestamps use the archive clock here. Causal history independently
# verifies the publisher's publication time before accepting a source.
_EVENT_SQL = """
SELECT DISTINCT _version AS version, global_event_id AS event_id,
    source_url AS url, CAST(date_added AS VARCHAR) AS observed,
    array_length(list_filter($keywords, term -> strpos(
        regexp_replace(lower(concat_ws(' ', actor1_name, actor2_name,
            action_geo_full_name, actor1_country_code, actor2_country_code,
            event_code, left(source_url, 4096))), '[-_/]+', ' ', 'g'), term) > 0)) AS score
FROM archive_events
WHERE CAST(event_date AS VARCHAR) BETWEEN $from_day AND $last_day
  AND (regexp_full_match(CAST(date_added AS VARCHAR), '[0-9]{8}')
       AND CAST(date_added AS VARCHAR) <= $last_day
       OR regexp_full_match(CAST(date_added AS VARCHAR), '[0-9]{14}')
       AND CAST(date_added AS VARCHAR) < $until_stamp)
"""

_MENTION_SQL = """
SELECT m._version AS version, m.global_event_id AS event_id,
    m.mention_identifier AS url, CAST(m.mention_time_date AS VARCHAR) AS observed,
    greatest(COALESCE(e.score, 0), COALESCE(g.score, 0),
        2 * array_length(list_filter($keywords, term -> strpos(
            regexp_replace(lower(left(m.mention_identifier, 4096)), '[-_/]+', ' ', 'g'), term) > 0)))
        AS score,
    COALESCE(try_cast(m.confidence AS INTEGER), 0) AS confidence
FROM archive_mentions AS m LEFT JOIN event_matches AS e
    ON e.version = m._version AND e.event_id = m.global_event_id
LEFT JOIN gkg_matches AS g ON g.version = m._version AND g.url = m.mention_identifier
WHERE CAST(m.mention_type AS VARCHAR) = '1'
  AND CAST(m.mention_time_date AS VARCHAR) >= $from_stamp
  AND CAST(m.mention_time_date AS VARCHAR) < $until_stamp
"""

_GKG_SQL = """
SELECT _version AS version, CAST(NULL AS VARCHAR) AS event_id, document_identifier AS url,
    CAST(document_date AS VARCHAR) AS observed,
    array_length(list_filter($keywords, term -> strpos(
        regexp_replace(lower(concat_ws(' ', left(document_identifier, 4096),
            persons, organizations, locations, themes, all_names,
            left(quotations, 12000), dates,
            regexp_extract(extras, '<PAGE_TITLE>([^<]*)</PAGE_TITLE>', 1))),
            '[-_/]+', ' ', 'g'), term) > 0)) AS score
FROM archive_gkg
WHERE CAST(document_date AS VARCHAR) >= $from_stamp AND CAST(document_date AS VARCHAR) < $until_stamp
"""

_LEGACY_GKG_SQL = """
SELECT _version AS version, CAST(NULL AS VARCHAR) AS event_id,
    unnest(string_split(source_urls, '<UDIV>')) AS url, CAST(document_date AS VARCHAR) AS observed,
    array_length(list_filter($keywords, term -> strpos(
        regexp_replace(lower(concat_ws(' ', persons, organizations, locations, themes)),
            '[-_/]+', ' ', 'g'), term) > 0)) AS score
FROM archive_legacy_gkg
WHERE CAST(document_date AS VARCHAR) BETWEEN $from_day AND $last_day
"""


@dataclass
class ArchiveLeads:
    rows: list[dict]
    coverage: dict[str, dict]


def _day(value):
    return value if type(value) is date else date.fromisoformat(value)


def _paths(storage_root, groups, start, end):
    root = archive_root(storage_root)
    paths, coverage = defaultdict(list), defaultdict(set)
    for group in groups:
        path = (root / group['path']).resolve()
        kind, version = group['kind'], group['version']
        if kind not in FIRST_DAY or version not in (1, 2):
            continue
        days = max(start, FIRST_DAY[kind], _day(group['date_from']))
        finish = min(end - timedelta(days=1), _day(group['date_to']))
        if days > finish:
            continue
        signature = group.get('file_signature')
        try:
            readable = path.is_relative_to(root) and path.suffix == '.parquet' and path.is_file()
            if readable and signature:
                readable = file_signature(path) == signature
            elif readable:
                # Before the first 0.56 audit, catch missing/truncated legacy outputs
                # without hashing a whole historical window on an interactive read.
                with path.open('rb') as stream:
                    header = stream.read(4)
                    stream.seek(-4, 2)
                    readable = header == stream.read(4) == b'PAR1'
                if path.name == 'data.parquet':
                    # The manifest is published after the immutable payload. If an
                    # old shard was later edited in place, let the worker verify it.
                    readable = readable and path.stat().st_mtime_ns <= path.with_name('manifest.json').stat().st_mtime_ns
        except OSError:
            readable = False
        if readable:
            paths[kind, version].append(str(path))
            state = 'ready' if group['state'] == 'ready' else 'partial'
        else:
            state = 'unreadable'
        for step in range((finish - days).days + 1):
            coverage[kind, state].add(days + timedelta(days=step))
    report = {}
    for kind, earliest in FIRST_DAY.items():
        expected = max(0, (end - max(earliest, start)).days)
        covered = coverage[kind, 'ready'] | coverage[kind, 'partial']
        ready = coverage[kind, 'ready'] - coverage[kind, 'unreadable']
        state = ('not_expected' if not expected else 'gap' if not covered else
                 'covered' if len(ready) == expected else 'partial')
        report[kind] = dict(state=state, ready_days=len(ready),
                            available_days=len(covered), expected_days=expected)
    return paths, report


def _view(connection, view, paths):
    scans = []
    for version, values in sorted(paths.items()):
        if values:
            relation = connection.read_parquet(sorted(set(values)), union_by_name=True, hive_partitioning=False)
            scans.append(relation.project(f'{version}::INTEGER AS _version, *'))
    if len(scans) == 2:
        # DuckDB's relation union is positional, so use named UNION ALL for
        # old Events without ADM2 columns and modern Events with them.
        scans[0].create_view(view + '_v1')
        scans[1].create_view(view + '_v2')
        connection.execute(f'CREATE VIEW {view} AS SELECT * FROM {view}_v1 UNION ALL BY NAME SELECT * FROM {view}_v2')
    elif scans:
        scans[0].create_view(view)
    return bool(scans)


def _parameters(sql, params):
    return {key: value for key, value in params.items() if '$' + key in sql}


def _query(connection, paths, params):
    sources = []
    if _view(connection, 'archive_events', {v: paths['events', v] for v in (1, 2)}):
        connection.execute(f'''
            CREATE TEMP TABLE event_candidates AS
            SELECT * FROM ({_EVENT_SQL}) AS ranked
            WHERE score > 0 ORDER BY score DESC, observed DESC, event_id LIMIT 4096
            ''', _parameters(_EVENT_SQL, params))
        sources.append("""SELECT *, 0 AS confidence, 'events' AS source_kind
                          FROM event_candidates""")
    have_events = bool(sources)
    have_gkg = _view(connection, 'archive_gkg', {2: paths['gkg', 2]})
    if have_gkg:
        connection.execute(f'''
            CREATE TEMP TABLE gkg_candidates AS
            SELECT * FROM ({_GKG_SQL}) AS ranked
            WHERE score > 0 AND (starts_with(url, 'http://') OR starts_with(url, 'https://'))
            ORDER BY score DESC, observed DESC, url LIMIT 4096
            ''', _parameters(_GKG_SQL, params))
        sources.append("""SELECT *, 0 AS confidence, 'gkg' AS source_kind
                          FROM gkg_candidates""")
    if _view(connection, 'archive_mentions', {2: paths['mentions', 2]}):
        connection.execute('''CREATE TEMP TABLE event_matches AS SELECT version, event_id, max(score) AS score
                              FROM event_candidates GROUP BY version, event_id''' if have_events else
                           '''CREATE TEMP TABLE event_matches(version INTEGER, event_id VARCHAR, score INTEGER)''')
        connection.execute('''CREATE TEMP TABLE gkg_matches AS SELECT version, url, max(score) AS score
                              FROM gkg_candidates GROUP BY version, url''' if have_gkg else
                           '''CREATE TEMP TABLE gkg_matches(version INTEGER, url VARCHAR, score INTEGER)''')
        sources.append(f"SELECT *, 'mentions' AS source_kind FROM ({_MENTION_SQL}) WHERE score > 0")
    if _view(connection, 'archive_legacy_gkg', {1: paths['gkg', 1]}):
        sources.append(f"SELECT *, 0 AS confidence, 'gkg' AS source_kind FROM ({_LEGACY_GKG_SQL}) WHERE score > 0")
    if not sources:
        return []
    combined = ' UNION ALL '.join(sources)
    sql = f"""
        WITH candidates AS (
            SELECT *, lower(regexp_extract(url, '^https?://([^:/?#]+)', 1)) AS domain
            FROM ({combined}) WHERE starts_with(url, 'http://') OR starts_with(url, 'https://')
        ), unique_urls AS (
            SELECT * FROM candidates WHERE domain <> ''
            QUALIFY row_number() OVER (
                PARTITION BY url ORDER BY score DESC, (event_id IS NOT NULL) DESC,
                confidence DESC, observed DESC, version DESC, event_id
            ) = 1
        )
        SELECT version, event_id, url, source_kind FROM unique_urls
        QUALIFY row_number() OVER (
            PARTITION BY domain ORDER BY score DESC, confidence DESC, observed DESC, url
        ) <= 4
        ORDER BY score DESC, confidence DESC, observed DESC, url LIMIT $limit
    """
    return [dict(version=version, event_id=event_id, url=url, source_kind=kind)
            for version, event_id, url, kind in connection.execute(sql, _parameters(sql, params)).fetchall()]


def find_leads(engine, storage_root, start, end, keywords, limit=48, *, cutoff=None):
    """Search committed shards in [start, end), with an exact UTC upper clock."""
    from .service import query_groups

    if end <= start:
        return ArchiveLeads([], {})
    groups = query_groups(engine, start, end - timedelta(days=1))
    paths, coverage = _paths(storage_root, groups, start, end)
    if not paths or not keywords:
        return ArchiveLeads([], coverage)
    params = dict(
        keywords=keywords, from_day=start.strftime('%Y%m%d'),
        last_day=(end - timedelta(days=1)).strftime('%Y%m%d'),
        from_stamp=start.strftime('%Y%m%d000000'),
        until_stamp=(cutoff.astimezone(timezone.utc).strftime('%Y%m%d%H%M%S') if cutoff else end.strftime('%Y%m%d000000')),
        limit=limit,
    )
    import duckdb
    with TemporaryDirectory(prefix='news2douyin-history-query-') as work:
        with duckdb.connect(':memory:', config={
            'threads': 2, 'memory_limit': '512MB', 'max_temp_directory_size': '2GB',
            'temp_directory': work,
        }) as connection:
            timer = Timer(DEFAULT_SECONDS, connection.interrupt)
            timer.daemon = True
            timer.start()
            try:
                return ArchiveLeads(_query(connection, paths, params), coverage)
            finally:
                timer.cancel()
                timer.join()
