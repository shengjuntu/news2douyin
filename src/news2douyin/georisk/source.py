"""Read one immutable archived GKG batch at a time, releasing the lake between pages."""
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from math import isfinite
from urllib.parse import urlsplit
import zlib

from ..collect.providers.gkg import FIPS
from ..dedup.normalize import canonicalize_url
from ..digest.calendar import BEIJING
from ..intake.lake import PARSER_VERSION, unpack_payload
from ..selection.source_content import source_url, text, usable_title

PAGE_SIZE = 256
MAX_PAYLOAD = 256 * 1024
MAX_OVERLAY = 32 * 1024


@dataclass(frozen=True)
class Batch:
    feed: str
    stamp: str
    sha: str

    @property
    def day(self):
        moment = datetime.strptime(self.stamp, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
        return moment.astimezone(BEIJING).date().isoformat()


def manifests(lake, cfg, first, end):
    with lake.connection(read_only=True) as conn:
        if conn is None or 'intake_archives' not in lake.tables(conn):
            return []
        values = conn.execute(
            '''SELECT feed,stamp,sha256 FROM intake_archives
               WHERE feed=ANY(?) AND stamp>=? AND stamp<? ORDER BY stamp DESC,feed''',
            [list(cfg.feeds), first, end]
        ).fetchall()
    return [Batch(*row) for row in values]


def pages(lake, batch, cfg):
    with lake.connection(read_only=True) as conn:
        tables = lake.tables(conn)
        current = ('intake_records_v2' in tables and conn.execute(
            'SELECT 1 FROM intake_records_v2 WHERE feed=? AND stamp=? LIMIT 1',
            [batch.feed, batch.stamp]).fetchone())
    needles = sorted({theme.lower() for values in cfg.categories.values() for theme in values})
    if current:
        query = '''SELECT row_index,url,title,
                   CASE WHEN raw_bytes BETWEEN 1 AND ? AND octet_length(payload)<=?
                             AND coalesce(length(metadata_overlay_json),0)<=? THEN payload END,
                   codec,raw_sha256,raw_bytes,
                   CASE WHEN length(metadata_overlay_json)<=? THEN metadata_overlay_json END,parser_version
                   FROM intake_records_v2 WHERE feed=? AND stamp=? AND row_index>? AND valid
                     AND (coalesce(metadata_overlay_json,'{}')<>'{}'
                          OR array_length(list_filter(?, v -> contains(search_text,v)))>0)
                   ORDER BY row_index LIMIT ?'''
    elif 'intake_records' in tables:
        query = '''SELECT row_index,url,title,CASE WHEN length(metadata_json)<=? THEN metadata_json END
                   FROM intake_records WHERE feed=? AND stamp=? AND row_index>? AND valid
                   ORDER BY row_index LIMIT ?'''
    else:
        return
    after = 0
    while True:
        if current:
            values = [MAX_PAYLOAD, MAX_PAYLOAD, MAX_OVERLAY, MAX_OVERLAY,
                      batch.feed, batch.stamp, after, needles, PAGE_SIZE]
        else:
            values = [MAX_PAYLOAD, batch.feed, batch.stamp, after, PAGE_SIZE]
        with lake.connection(read_only=True) as conn:
            rows = conn.execute(query, values).fetchall()
        if not rows:
            return
        after = rows[-1][0]
        yield rows
        if len(rows) < PAGE_SIZE:
            return


def _metadata(row):
    if len(row) == 1:
        return json.loads(row[0])
    payload, codec, sha, size, overlay, version = row
    if payload is None or version != PARSER_VERSION:
        raise ValueError('GKG payload unavailable')
    raw = unpack_payload(dict(payload=payload, codec=codec, raw_sha256=sha, raw_bytes=size))
    fields = raw.split(b'\t', 16)
    if len(fields) != 17:
        raise ValueError('GKG row missing columns')
    countries = set()
    for value in fields[9].decode().split(';'):
        parts = value.split('#', 3)
        if len(parts) >= 3 and (iso := FIPS.get(parts[2].upper())):
            countries.add(iso)
    result = dict(themes=fields[7].decode().split(';'), mention_iso=countries, tone=fields[15].decode())
    if overlay and overlay != '{}':
        delta = json.loads(overlay)
        for key in delta.get('remove', []):
            result.pop(key, None)
        result.update(delta.get('set', {}))
    return result


def _negative(value):
    try:
        result = float(value.split(',', 3)[2])
        if isfinite(result) and 0 <= result <= 100:
            return result
    except (IndexError, ValueError, AttributeError, TypeError):
        pass
    return None


def candidates(batch, rows, cfg):
    """One earliest scored and one earliest titled observation per group/URL."""
    unique = {}
    errors = 0
    for index, url, title, *raw in rows:
        try:
            metadata = _metadata(raw)
            themes = {value.strip() for value in metadata.get('themes', ()) if isinstance(value, str)}
            mentions = {value.lower() for value in metadata.get('mention_iso', ()) if isinstance(value, str)}
            folded = title.casefold() if isinstance(title, str) and usable_title(title) else ''
            categories = [name for name, tags in cfg.categories.items()
                          if themes & tags and (name not in cfg.title_terms
                          or any(term in folded for term in cfg.title_terms[name]))]
        except (ValueError, TypeError, AttributeError, KeyError, zlib.error, RecursionError):
            errors += 1
            continue
        if not categories or not source_url(url) or '\\' in url:
            continue
        canonical = canonicalize_url(url)
        domain = (urlsplit(url).hostname or '').removeprefix('www.')
        doc_key = sha256(canonical.encode()).hexdigest()
        rank = f'{batch.stamp}:{batch.feed}:{index:012d}'
        tone = _negative(metadata.get('tone'))
        has_title = bool(folded and len(url) <= 4096)
        for scope, countries in cfg.scopes.items():
            if countries and not countries & mentions:
                continue
            for category in categories:
                key = (scope, category, doc_key)
                item = unique.setdefault(key, dict(scope=scope, category=category, doc_key=doc_key, domain=domain,
                    negative=None, tone_key=None, title=None, url=None, evidence_key=None))
                if tone is not None and item['tone_key'] is None:
                    item.update(negative=tone, tone_key=rank)
                if has_title and item['evidence_key'] is None:
                    item.update(title=text(title, 300), url=url, evidence_key=rank)
    return list(unique.values()), errors
