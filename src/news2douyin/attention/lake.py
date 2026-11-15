"""Derived NewsNow observations. Only background workers open this DuckDB.

The immutable HTTP archives remain the evidence of record. Returned array
positions describe a sampled list, never an official rank or audience size.
Every operation takes both a process and a per-path thread lock; connections
are short lived so a PostgreSQL projection can safely fail and replay.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from threading import Lock, RLock

import duckdb

from ..collect.providers.newsnow import SOURCE_ALIASES, SOURCE_CATALOG, _to_item, parse_time
from ..dedup.normalize import canonicalize_url, normalize_title

_GUARD = Lock()
_LOCKS: dict[str, RLock] = {}
_HASH = re.compile(r'[0-9a-fA-F]{64}')
_SOURCE = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,79}')
MAX_MANIFEST_BYTES = 512 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_BATCH_BYTES = 32 * 1024 * 1024
MAX_ITEMS = 10000
MAX_KEYS = 2000


class AttentionLakeBusy(RuntimeError):
    """Another cooperating process owns the derived observation store."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _hash(value):
    return hashlib.sha256(value if isinstance(value, bytes) else _json(value).encode()).hexdigest()


def _time(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    return parse_time(value)['value']


def _stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() if value else None


def _decode(body):
    def invalid(_value):
        raise ValueError('non-finite JSON')
    try:
        result = json.loads(body, parse_constant=invalid)
        # JSON exponent overflow is finite syntactically, but not valid derived data.
        _json(result)
        return result
    except (ValueError, UnicodeError, RecursionError, TypeError) as exc:
        raise ValueError('NewsNow archive contains invalid finite JSON') from exc


def _platform(source):
    reverse = {alias: parent for parent, alias in SOURCE_ALIASES.items()}
    return reverse.get(source, source)


def _scope(source):
    platform = _platform(source)
    spec = SOURCE_CATALOG.get(platform, {})
    scope = 'global' if platform in ('github', 'producthunt') else 'cn_platform' if spec else 'unknown'
    return platform, spec.get('kind', 'unknown'), scope


def _eligible(row):
    return row['kind'] == 'hottest' and row['audience_scope'] == 'cn_platform'


class AttentionLake:
    def __init__(self, storage_root):
        self.root = Path(storage_root).resolve()
        self.path = self.root / 'newsnow' / 'attention.duckdb'
        self.lock_path = self.root / 'newsnow' / 'attention.lock'
        with _GUARD:
            self._lock = _LOCKS.setdefault(str(self.path), RLock())

    def _contained(self, name):
        if not isinstance(name, (str, Path)) or not str(name):
            raise ValueError('NewsNow archive path is required')
        candidate = Path(name)
        candidate = candidate if candidate.is_absolute() else self.root / candidate
        # Refuse symlink final files as well as escapes through parent directories.
        if candidate.is_symlink():
            raise ValueError('NewsNow archive must be a regular file, not a symlink')
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError('NewsNow archive path must stay inside storage_root')
        return resolved

    def _read(self, name, digest, limit):
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise ValueError('NewsNow archive checksum is required')
        path = self._contained(name)
        if not path.is_file() or path.stat().st_size > limit:
            raise ValueError('NewsNow archive is missing or exceeds byte limit')
        with path.open('rb') as stream:
            body = stream.read(limit + 1)
        if len(body) > limit or _hash(body) != digest.lower():
            raise ValueError('NewsNow archive checksum mismatch or byte limit exceeded')
        return path, body

    @contextmanager
    def _connect(self):
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            for target in (self.path, self.lock_path):
                if target.is_symlink() or not target.resolve().is_relative_to(self.root):
                    raise ValueError('NewsNow derived storage must stay inside storage_root without symlinks')
            # The lock inode is persistent. Unlinking it permits split ownership.
            handle = self.lock_path.open('a+b')
            locked = False
            connection = None
            try:
                try:
                    if sys.platform == 'win32':
                        import msvcrt
                        handle.seek(0, 2)
                        if not handle.tell():
                            handle.write(b'0'); handle.flush()
                        handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                except OSError as exc:
                    raise AttentionLakeBusy('NewsNow attention store is owned by another process') from exc
                connection = duckdb.connect(str(self.path))
                connection.execute('SET threads=1')
                self._schema(connection)
                yield connection
            finally:
                if connection is not None:
                    connection.close()
                try:
                    if locked and sys.platform == 'win32':
                        import msvcrt
                        handle.seek(0); msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    elif locked:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()

    @staticmethod
    def _schema(db):
        db.execute('''CREATE TABLE IF NOT EXISTS attention_contents (
            content_key VARCHAR PRIMARY KEY, source_id VARCHAR NOT NULL,
            items_json VARCHAR NOT NULL, list_size INTEGER NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS attention_content_items (
            content_key VARCHAR NOT NULL, item_key VARCHAR NOT NULL, source_id VARCHAR NOT NULL,
            position INTEGER NOT NULL, item_json VARCHAR NOT NULL,
            PRIMARY KEY(content_key, item_key))''')
        db.execute('''CREATE TABLE IF NOT EXISTS attention_observations (
            observation_key VARCHAR PRIMARY KEY, source_id VARCHAR NOT NULL,
            platform VARCHAR NOT NULL, kind VARCHAR NOT NULL, audience_scope VARCHAR NOT NULL,
            status VARCHAR NOT NULL, observed_at VARCHAR NOT NULL, observed_ts DOUBLE NOT NULL,
            snapshot_reported_at VARCHAR NOT NULL, upstream_status VARCHAR NOT NULL,
            content_key VARCHAR NOT NULL, archive_ref VARCHAR NOT NULL, archive_sha256 VARCHAR NOT NULL,
            list_size INTEGER NOT NULL, error_code VARCHAR NOT NULL)''')
        db.execute('''CREATE TABLE IF NOT EXISTS attention_receipts (
            receipt VARCHAR PRIMARY KEY, manifest_path VARCHAR NOT NULL, manifest_sha256 VARCHAR NOT NULL,
            observed_at VARCHAR NOT NULL, batch_json VARCHAR NOT NULL)''')
        db.execute('CREATE INDEX IF NOT EXISTS attention_item_key ON attention_content_items(item_key)')
        db.execute('CREATE INDEX IF NOT EXISTS attention_source_time ON attention_observations(source_id, observed_ts)')
        db.execute('CREATE INDEX IF NOT EXISTS attention_content_time ON attention_observations(content_key, observed_ts)')

    def _manifest(self, manifest_path, expected_sha256):
        path, body = self._read(manifest_path, expected_sha256, MAX_MANIFEST_BYTES)
        report = _decode(body)
        if not isinstance(report, dict) or report.get('schema_version') != 1:
            raise ValueError('Unsupported NewsNow manifest schema')
        reports, snapshots = report.get('sources'), report.get('snapshots')
        if not isinstance(reports, list) or not 1 <= len(reports) <= 128 or not isinstance(snapshots, list) or len(snapshots) > 128:
            raise ValueError('NewsNow manifest source count is invalid')
        if report.get('manifest_path') != path.relative_to(self.root).as_posix():
            raise ValueError('NewsNow manifest path disagrees with its receipt')
        successful = {row.get('source_id') for row in reports if isinstance(row,dict)
            and isinstance(row.get('source_id'),str) and row.get('status') in ('success','empty')}
        by_source = {}
        total = 0
        for snapshot in snapshots:
            if not isinstance(snapshot, dict):
                raise ValueError('NewsNow snapshot must be an object')
            source = snapshot.get('source_id')
            if not isinstance(source, str) or not _SOURCE.fullmatch(source) or source in by_source:
                raise ValueError('NewsNow snapshot source is invalid or duplicated')
            reference, digest = snapshot.get('archive_ref', ''), snapshot.get('archive_sha256', '')
            payload = None
            if reference:
                archive_path, blob = self._read(reference, digest, MAX_RESPONSE_BYTES)
                expected_path = 'newsnow/snapshots/' + digest.lower() + ('.json' if snapshot.get('json_valid') else '.body')
                if archive_path.relative_to(self.root).as_posix() != expected_path:
                    raise ValueError('NewsNow content archive path disagrees with checksum')
                if snapshot.get('archive_bytes') != len(blob):
                    raise ValueError('NewsNow content archive size mismatch')
                total += len(blob)
                if total > MAX_BATCH_BYTES:
                    raise ValueError('NewsNow manifest exceeds batch byte limit')
                if snapshot.get('json_valid') and source in successful:
                    payload = _decode(blob)
            elif digest or snapshot.get('archive_bytes', 0):
                raise ValueError('NewsNow no-response snapshot contains invalid reference')
            by_source[source] = (snapshot, payload)
        normalized, seen = [], set()
        for diagnostic in reports:
            if not isinstance(diagnostic, dict):
                raise ValueError('NewsNow source diagnostic must be an object')
            source = diagnostic.get('source_id')
            if not isinstance(source, str) or not _SOURCE.fullmatch(source) or source in seen:
                raise ValueError('NewsNow source identifier is invalid or duplicated')
            seen.add(source)
            snapshot, payload = by_source.get(source, ({}, None))
            observed = _time(snapshot.get('observed_at') or diagnostic.get('observed_at'))
            status = diagnostic.get('status', 'not_attempted')
            if status not in ('success', 'empty', 'failed', 'not_attempted'):
                raise ValueError('NewsNow source diagnostic status is invalid')
            # A source that was never requested has no observation clock. Preserve
            # it in the receipt; it cannot be a measured zero or overwrite state.
            if not observed and status != 'not_attempted':
                raise ValueError('NewsNow attempted source lacks local observation time')
            platform, kind, scope = _scope(source)
            row = dict(source_id=source, platform=platform, kind=kind, audience_scope=scope,
                status=status, observed_at=observed,
                snapshot_reported_at=_time(snapshot.get('snapshot_reported_at')),
                upstream_status=str(snapshot.get('response_status', ''))[:32],
                archive_ref=snapshot.get('archive_ref', ''), archive_sha256=snapshot.get('archive_sha256', ''),
                error_code=str(diagnostic.get('error_code', ''))[:80], list_size=0, content_key='', items=[])
            if status in ('success', 'empty'):
                if not isinstance(payload, dict) or not isinstance(payload.get('items'), list) or payload.get('error'):
                    raise ValueError('NewsNow successful source has no valid archived list')
                accepted = {source, platform, SOURCE_ALIASES.get(platform, platform)}
                if payload.get('id') not in accepted or payload.get('status') not in ('success', 'cache'):
                    raise ValueError('NewsNow archived source or upstream status mismatch')
                if not row['snapshot_reported_at'] or _time(payload.get('updatedTime')) != row['snapshot_reported_at']:
                    raise ValueError('NewsNow archived reported snapshot time mismatch')
                if len(payload['items']) > MAX_ITEMS:
                    raise ValueError('NewsNow archived list exceeds item limit')
                if bool(payload['items']) != (status == 'success'):
                    raise ValueError('NewsNow empty/source status mismatch')
                row['list_size'] = len(payload['items'])
                row['upstream_status'] = payload['status']
                # Different cosmetic/cache timestamps never manufacture new content.
                keys = set()
                for position, raw in enumerate(payload['items'], 1):
                    item = _to_item(platform, raw, position, dict(snapshot_reported_at=row['snapshot_reported_at'],
                        response_status=row['upstream_status'], raw_sha256=row['archive_sha256']), observed)
                    if item is None:
                        continue
                    meta = item['newsnow']
                    url = canonicalize_url(item['url'])
                    key = _hash([source, meta['item_id'] or url, normalize_title(item['title'])])
                    if key in keys:
                        continue
                    keys.add(key)
                    metrics = meta['raw_metrics']
                    metrics = {k: v[:256] if isinstance(v,str) else v for k, v in metrics.items()
                        if isinstance(v, (str, int, float)) and not isinstance(v, bool)}
                    value = dict(key=key, source_id=source, platform=platform, source_kind=kind, audience_scope=scope,
                        native_id=meta['item_id'][:512], title=item['title'], url=item['url'], canonical_url=url,
                        excerpt=item['content'][:1000], observed_list_position=position,
                        position_kind='returned_list_position', raw_metric_text=meta['heat_text'][:256], metric_raw=metrics)
                    row['items'].append(value)
                semantic = dict(source_id=source, list_size=row['list_size'], items=row['items'])
                row['content_key'] = _hash(semantic)
                if status == 'success' and not row['items']:
                    raise ValueError('NewsNow successful archive contains no valid article links')
            normalized.append(row)
        if set(by_source) - seen:
            raise ValueError('NewsNow snapshot has no source diagnostic')
        observed = max((row['observed_at'] for row in normalized if row['observed_at']), default='')
        receipt = _hash([path.relative_to(self.root).as_posix(), expected_sha256.lower()])
        return dict(receipt=receipt, observed_at=observed, sources=normalized), path

    def ingest_manifest(self, manifest_path, expected_sha256):
        """Verify complete archived lists, then atomically append derived observations.

        Replaying a receipt returns exactly its original normalized projection,
        even if observations from a newer receipt have arrived in between.
        Files are verified on every replay, including already committed receipts.
        """
        batch, path = self._manifest(manifest_path, expected_sha256)
        with self._connect() as db:
            saved = db.execute('SELECT batch_json FROM attention_receipts WHERE receipt=?', [batch['receipt']]).fetchone()
            if saved:
                replay = json.loads(saved[0])
                for row in replay['sources']:
                    content = db.execute('SELECT items_json FROM attention_contents WHERE content_key=?',
                        [row['content_key']]).fetchone() if row['content_key'] else None
                    row['items'] = json.loads(content[0]) if content else []
                    for item in row['items']:
                        item.update(observed_at=row['observed_at'], snapshot_reported_at=row['snapshot_reported_at'],
                            archive_ref=row['archive_ref'], archive_sha256=row['archive_sha256'], content_key=row['content_key'])
                return replay
            db.execute('BEGIN TRANSACTION')
            try:
                for row in batch['sources']:
                    previous = db.execute('''SELECT content_key FROM attention_observations
                        WHERE source_id=? AND observed_ts<? AND status IN ('success','empty')
                        ORDER BY observed_ts DESC, observation_key DESC LIMIT 1''',
                        [row['source_id'], _stamp(row['observed_at']) or 0]).fetchone()
                    row['changed'] = bool(row['content_key'] and previous and previous[0] != row['content_key'])
                    row['first_observation'] = previous is None
                    if row['content_key']:
                        inserted = db.execute('''INSERT INTO attention_contents VALUES (?,?,?,?)
                            ON CONFLICT DO NOTHING RETURNING content_key''',
                            [row['content_key'], row['source_id'], _json(row['items']), row['list_size']]).fetchone()
                        if inserted and row['items']:
                            db.executemany('INSERT INTO attention_content_items VALUES (?,?,?,?,?)',
                                [[row['content_key'], item['key'], row['source_id'], item['observed_list_position'], _json(item)]
                                 for item in row['items']])
                    for item in row['items']:
                        item.update(observed_at=row['observed_at'], snapshot_reported_at=row['snapshot_reported_at'],
                            archive_ref=row['archive_ref'], archive_sha256=row['archive_sha256'], content_key=row['content_key'])
                    if row['observed_at'] and row['status'] != 'not_attempted':
                        observation_key = _hash([row['source_id'], row['observed_at'], row['status'], row['content_key'],
                            row['archive_sha256'], row['snapshot_reported_at']])
                        db.execute('INSERT INTO attention_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING',
                            [observation_key, row['source_id'], row['platform'], row['kind'], row['audience_scope'],
                             row['status'], row['observed_at'], _stamp(row['observed_at']), row['snapshot_reported_at'],
                             row['upstream_status'], row['content_key'], row['archive_ref'], row['archive_sha256'],
                             row['list_size'], row['error_code']])
                receipt_data = dict(batch, sources=[{key:value for key,value in row.items() if key!='items'}
                    for row in batch['sources']])
                db.execute('INSERT INTO attention_receipts VALUES (?,?,?,?,?)', [batch['receipt'],
                    path.relative_to(self.root).as_posix(), expected_sha256.lower(), batch['observed_at'], _json(receipt_data)])
                db.execute('COMMIT')
            except Exception:
                db.execute('ROLLBACK')
                raise
        return batch

    def metrics(self, item_keys, now=None, hours=72, max_points=48):
        """Bounded topic analytics for background PG projection, never HTTP handlers."""
        return self.metrics_many({'topic':item_keys},now=now,hours=hours,max_points=max_points)['topic']

    def metrics_many(self, topics, now=None, hours=72, max_points=48):
        """Compute a worker batch with one connection and one shared history read.

        The PostgreSQL transaction must be closed before this is called. Source
        histories and decoded immutable items are shared across all topics.
        """
        if not isinstance(topics,dict) or len(topics)>2000:
            raise ValueError('Attention metrics accepts at most 2000 topics per batch')
        grouped = {}
        for topic, item_keys in topics.items():
            keys = list(dict.fromkeys(item_keys))
            if len(keys) > MAX_KEYS or any(not isinstance(k, str) or len(k) > 128 for k in keys):
                raise ValueError('Attention metrics accepts at most 2000 bounded item keys per topic')
            grouped[topic] = keys
        keys = list(dict.fromkeys(key for values in grouped.values() for key in values))
        if len(keys)>200000:
            raise ValueError('Attention metrics batch exceeds 200000 distinct item keys')
        hours = max(1, min(168, float(hours)))
        max_points = max(2, min(72, int(max_points)))
        moment = _time(now) if now is not None else _time(datetime.now(timezone.utc))
        if not moment:
            raise ValueError('Attention metrics now is invalid')
        now_ts = _stamp(moment)
        start = now_ts - hours * 3600
        empty = dict(attention_score=None, rise_score=None, platform_count=0, item_count=0,
            first_seen='', last_seen='', baseline_state='insufficient', platforms=[], series=[],
            notes=['列表位置是本次返回数组的位置，并非官方排名；未返回、失败和未知均不表示热度为零。'],
            scope='observed_cn_hotlists', observed_at='', last_observed_at='', computed_at=moment,
            position_kind='returned_list_position', rise_kind='normalized_same_source_position_change')
        if not keys or not self.path.exists():
            return {topic:dict(empty) for topic in topics}
        with self._connect() as db:
            item_rows = db.execute('''SELECT content_key,item_key,source_id,position,item_json
                FROM attention_content_items WHERE item_key IN (SELECT unnest(?::VARCHAR[]))''', [keys]).fetchall()
            spans = db.execute('''SELECT content_key,min(observed_at),max(observed_at)
                FROM attention_observations WHERE content_key IN (
                    SELECT content_key FROM attention_content_items WHERE item_key IN (SELECT unnest(?::VARCHAR[])))
                AND observed_ts<=? GROUP BY content_key''', [keys,now_ts]).fetchall()
            sources = sorted({row[2] for row in item_rows})
            if not sources:
                return {topic:dict(empty) for topic in topics}
            # One pre-window observation supplies state at the left boundary.
            rows = db.execute('''SELECT * FROM attention_observations
                WHERE source_id IN (SELECT unnest(?::VARCHAR[])) AND observed_ts<=? AND observed_ts>=?
                UNION ALL
                SELECT * FROM attention_observations WHERE observation_key IN (
                    SELECT observation_key FROM attention_observations
                    WHERE source_id IN (SELECT unnest(?::VARCHAR[])) AND observed_ts<?
                    QUALIFY row_number() OVER (PARTITION BY source_id ORDER BY observed_ts DESC,observation_key DESC)=1)
                ORDER BY observed_ts,observation_key''', [sources,now_ts,start,sources,start]).fetchall()
            names = [column[0] for column in db.description]
        observations = [dict(zip(names, row)) for row in rows]
        items_by_key, observations_by_source = {}, {}
        spans_by_content = {row[0]:row for row in spans}
        for content,key,source,position,value in item_rows:
            items_by_key.setdefault(key,[]).append((content,key,source,position,json.loads(value)))
        for row in observations:
            observations_by_source.setdefault(row['source_id'],[]).append(row)
        result = {}
        for topic, keys in grouped.items():
            topic_items = [row for key in keys for row in items_by_key.get(key,[])]
            if not topic_items:
                result[topic] = dict(empty)
                continue
            topic_sources = {row[2] for row in topic_items}
            topic_contents = {row[0] for row in topic_items}
            topic_observations = sorted((row for source in topic_sources for row in observations_by_source.get(source,[])),
                key=lambda row:(row['observed_ts'],row['observation_key']))
            topic_spans = [spans_by_content[content] for content in topic_contents if content in spans_by_content]
            result[topic] = self._compute(topic_items,topic_observations,topic_spans,now_ts,start,max_points,empty)
        return result

    @staticmethod
    def _compute(item_rows, observations, spans, now_ts, start, max_points, empty):
        by_content = {}
        for content, key, source, position, value in item_rows:
            by_content.setdefault(content, []).append(value)
        # Retain the best matching item of a topic in each returned source list.
        matches = {content: min(values, key=lambda value: value['observed_list_position'])
                   for content, values in by_content.items()}
        timeline = {}
        for row in observations:
            timeline.setdefault(row['source_id'], []).append(row)
        latest = {source: values[-1] for source, values in timeline.items()}

        def present(row):
            return matches.get(row['content_key']) if row['status'] == 'success' else None

        def percentile(row, item):
            length = row['list_size']
            return 100.0 if length <= 1 else 100.0 * (length-item['observed_list_position'])/(length-1)

        def score(state, at):
            platforms = {}
            for row in state.values():
                item = present(row)
                if item and _eligible(row) and 0 <= at-row['observed_ts'] <= 6*3600:
                    platforms[row['platform']] = max(platforms.get(row['platform'], 0), percentile(row, item))
            return (round(sum(platforms.values())/len(platforms), 2) if platforms else None), len(platforms)

        output = dict(empty)
        output['platforms'] = []
        output['observed_at'] = output['last_observed_at'] = max((row['observed_at'] for row in observations),default='')
        first, last, rises = [row[1] for row in spans], [row[2] for row in spans], {}
        content_first = {row[0]:row[1] for row in spans}
        current_items = set()
        for source, row in sorted(latest.items()):
            history = timeline[source]
            item = present(row)
            prior = history[-2] if len(history) > 1 else None
            prior_item = present(prior) if prior else None
            if item and prior_item:
                # A topic may own several articles on one platform. A newly
                # observed headline must not inherit a different item's rank.
                prior_item = next((value for value in by_content.get(prior['content_key'],[])
                    if value['key']==item['key']),None)
            stale = now_ts-row['observed_ts'] > 6*3600
            comparable = bool(item and prior_item and not stale and row['observed_ts'] > prior['observed_ts'])
            delta = prior_item['observed_list_position']-item['observed_list_position'] if comparable else None
            rise = None
            if comparable and _eligible(row):
                rise = 0.0 if row['content_key'] == prior['content_key'] else 100.0*delta/max(1, max(row['list_size'], prior['list_size'])-1)
                # Provider aliases cannot increase platform breadth or double weight it.
                rises[row['platform']] = max(rises.get(row['platform'], -100), rise)
            current_valid = bool(item and not stale)
            if current_valid and row['kind'] != 'non_news':
                current_items.update(value['key'] for value in by_content[row['content_key']])
            display = dict(source_id=source, platform=row['platform'], source_kind=row['kind'],
                audience_scope=row['audience_scope'], status=row['status'], source_status=row['status'],
                upstream_status=row['upstream_status'], cached=row['upstream_status']=='cache',
                observed_at=row['observed_at'], snapshot_reported_at=row['snapshot_reported_at'], stale=stale,
                list_position=item['observed_list_position'] if item else None,
                comparison_item_key=item['key'] if item else '',
                previous_list_position=prior_item['observed_list_position'] if prior_item else None,
                position_delta=delta, normalized_position=round(percentile(row,item),2) if item else None,
                list_size=row['list_size'], raw_metric_text=item['raw_metric_text'] if item else '',
                metric_raw=item['metric_raw'] if item else {}, archive_ref=row['archive_ref'],
                changed=bool(prior and row['content_key'] and row['content_key'] != prior['content_key']),
                continuity='cached_content_repeat' if comparable and row['upstream_status']=='cache' and row['content_key']==prior['content_key']
                    else 'cached_observation' if row['upstream_status']=='cache' else 'comparable' if comparable
                    else 'first_observation' if prior is None else 'not_continuously_observed',
                observation_state='stale' if stale else 'observed' if item else 'source_failed' if row['status']=='failed' else 'not_returned',
                counts_for_attention=bool(current_valid and _eligible(row)), position_kind='returned_list_position',
                rise_kind='normalized_same_source_position_change',
                rise_interval_seconds=row['observed_ts']-prior['observed_ts'] if comparable else None,
                upstream_freshness='unknown', content_first_observed_at=content_first.get(row['content_key'],''))
            unchanged = row['observed_at']
            for candidate in reversed(history[:-1]):
                if not row['content_key'] or candidate['content_key'] != row['content_key']:
                    break
                unchanged = candidate['observed_at']
            display['content_unchanged_since'] = unchanged if row['content_key'] else ''
            display['observations'] = [dict(observed_at=entry['observed_at'],
                list_position=present(entry)['observed_list_position'] if present(entry) else None,
                cached=entry['upstream_status']=='cache', status=entry['status'],
                snapshot_reported_at=entry['snapshot_reported_at']) for entry in history[-8:]]
            output['platforms'].append(display)
        output['attention_score'], output['platform_count'] = score(latest, now_ts)
        output['item_count'] = len(current_items)
        output['first_seen'] = min(first, default='')
        output['last_seen'] = max(last, default='')
        output['rise_score'] = round(sum(rises.values())/len(rises),2) if rises else None
        output['baseline_state'] = 'comparable' if rises else 'insufficient'
        # Bins limit output without erasing repeated-cache/failed observations in
        # storage. The end of each bin retains its actual local observation time.
        samples = {}
        state = {}
        for row in observations:
            state[row['source_id']] = row
            if row['observed_ts'] < start:
                continue
            attention, coverage = score(state, row['observed_ts'])
            samples[row['observed_at']] = dict(observed_at=row['observed_at'], attention_score=attention,
                platform_count=coverage, cached=any(v['upstream_status']=='cache' for v in state.values()),
                partial=any(v['status']=='failed' for v in state.values()), continuity='sampled_observations')
        series = list(samples.values())
        if len(series) > max_points:
            series = [series[(index*(len(series)-1))//(max_points-1)] for index in range(max_points)]
        output['series'] = series
        output['notes'] = empty['notes'] + [
            '关注分为同源返回列表位置的归一化值，不是阅读量、搜索量或社会影响；仅统计已识别的中文平台热榜。',
            '时间轴使用本地观察时间；缓存内容不变不产生升温，不能据重复缓存确认持续真实上榜；快照报告时间不保证是上游真实抓取时刻。',
            '升位分是同源可比列表的位置变化分，不是传播增长率或每小时速度。',
            '当前覆盖以最近6小时有效观察为准；新平台首次出现、空榜、来源失败均不用于计算涨速。']
        return output
