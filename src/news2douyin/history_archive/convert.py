"""Verify and publish immutable groups of complete GDELT rows as Parquet."""
from __future__ import annotations

from base64 import b64encode
from dataclasses import dataclass
from datetime import datetime
import gzip
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
from tempfile import mkdtemp
from zipfile import BadZipFile, ZipFile


_GKG_V1_HEADER = tuple(
    'DATE NUMARTS COUNTS THEMES LOCATIONS PERSONS ORGANIZATIONS TONE CAMEOEVENTIDS SOURCES SOURCEURLS'.split()
)
_GKG_V1 = tuple(
    'document_date num_articles counts themes locations persons organizations tone cameo_event_ids sources source_urls'.split()
)
_GKG_V2 = tuple(
    ('gkg_record_id document_date source_collection_identifier source_common_name document_identifier '
     'counts v2_counts themes v2_themes locations v2_locations persons v2_persons organizations v2_organizations '
     'tone dates gcam sharing_image related_images social_image_embeds social_video_embeds '
     'quotations all_names amounts translation_info extras').split()
)
_MENTIONS_V2 = tuple(
    ('global_event_id event_time_date mention_time_date mention_type mention_source_name mention_identifier '
     'sentence_id actor1_char_offset actor2_char_offset action_char_offset in_raw_text confidence '
     'mention_doc_len mention_doc_tone mention_doc_translation_info extras').split()
)
_EVENT_PREFIX = tuple('global_event_id event_date month_year year fraction_date'.split())
_ACTOR_FIELDS = tuple(
    'code name country_code known_group_code ethnic_code religion1_code religion2_code type1_code type2_code type3_code'.split()
)
_EVENT_FIELDS = tuple(
    'is_root_event event_code event_base_code event_root_code quad_class goldstein_scale num_mentions num_sources num_articles avg_tone'.split()
)
_GEO_FIELDS = tuple('type full_name country_code adm1_code adm2_code lat long feature_id'.split())
_PROVENANCE = ('archive_name', 'archive_stamp', 'archive_row')
_CONVERSION_VERSION = 1
_MAX_LINE_BYTES = 16 * 1024 ** 2
_MAX_MEMBER_BYTES = 8 * 1024 ** 3
_PARQUET_OPTIONS = 'FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 8192'


def _event_columns(version: int) -> tuple[str, ...]:
    actors = tuple(f'{actor}_{field}' for actor in ('actor1', 'actor2') for field in _ACTOR_FIELDS)
    geos = tuple(
        f'{kind}_geo_{field}' for kind in ('actor1', 'actor2', 'action') for field in _GEO_FIELDS
        if (version == 2 or field != 'adm2_code')
    )
    return (*_EVENT_PREFIX, *actors, *_EVENT_FIELDS, *geos, 'date_added', 'source_url')


def source_columns(dataset: str, version: int) -> tuple[str, ...]:
    """Names for all original fields in source order.

    V1 Events includes a empty source_url for the 57-field older form.
    """
    if version not in (1, 2):
        raise ValueError('unknown archive schema version')
    if dataset == 'events':
        return _event_columns(version)
    if dataset == 'gkg':
        return _GKG_V2 if version == 2 else _GKG_V1
    if dataset == 'mentions' and version == 2:
        return _MENTIONS_V2
    raise ValueError('unknown archive dataset and version')


@dataclass(frozen=True)
class SourceZip:
    path: Path
    name: str
    stamp: str
    sha256: str


@dataclass(frozen=True)
class PublishedGroup:
    directory: Path
    parquet: Path
    bad_rows: Path | None
    manifest: dict
    rows: int
    valid_rows: int
    invalid_rows: int


class ConversionError(RuntimeError):
    """The ZIP is retained when a group cannot be safely published."""


def _stopping(stop) -> None:
    if stop is not None and stop():
        raise InterruptedError('historical archive conversion paused')


def _check_date(value: str) -> None:
    formats = {4: '%Y', 6: '%Y%m', 8: '%Y%m%d', 14: '%Y%m%d%H%M%S'}
    if not re.fullmatch(r'[0-9]+', value) or len(value) not in formats:
        raise ValueError('invalid archive period')
    datetime.strptime(value, formats[len(value)])


def _validate_source(source: SourceZip, dataset: str, version: int, group: str) -> None:
    _check_date(source.stamp)
    if not re.fullmatch(r'[a-f0-9]{64}', source.sha256):
        raise ValueError('invalid archive digest')
    if version == 2:
        suffix = {'events': '.export.CSV.zip', 'gkg': '.gkg.csv.zip', 'mentions': '.mentions.CSV.zip'}[dataset]
        expected = source.stamp + suffix
        valid_stamp = len(source.stamp) == 14 and source.stamp[10:12] in {'00', '15', '30', '45'} and source.stamp.endswith('00')
        valid_group = len(group) == 8 and source.stamp[:8] == group
    else:
        valid_stamp = len(source.stamp) in (4, 6, 8)
        valid_group = source.stamp == group
        if dataset == 'gkg':
            expected = source.stamp + '.gkg.csv.zip'
            valid_stamp &= len(source.stamp) == 8
        elif len(source.stamp) == 8:
            expected = source.stamp + '.export.CSV.zip'
        else:
            expected = source.stamp + '.zip'
    if not valid_stamp or not valid_group or source.name.casefold() != expected.casefold():
        raise ValueError('source ZIP does not match archive group')


def _member_name(source: SourceZip) -> str:
    name = source.name[:-4]
    return name if name.casefold().endswith('.csv') else name + '.csv'


def _source_hash(path: Path, stop=None) -> str:
    digest = sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            _stopping(stop)
            digest.update(chunk)
    return digest.hexdigest()


def _fields(raw: bytes, width: int) -> tuple[str, bytes]:
    line = raw[:-1].removesuffix(b'\r') if raw.endswith(b'\n') else raw
    if b'\r' in line or b'\x00' in line:
        return 'unsupported_control_byte', line
    if line.count(b'\t') != width - 1:
        return 'unexpected_field_count', line
    try:
        line.decode('utf8')
    except UnicodeDecodeError:
        return 'invalid_utf8', line
    return '', line


def _write_source(source, dataset, version, width, rows, bad_rows, stop):
    if _source_hash(source.path, stop) != source.sha256:
        raise ConversionError('archive SHA-256 changed before conversion')
    info = {
        'name': source.name, 'stamp': source.stamp, 'sha256': source.sha256,
        'zip_bytes': source.path.stat().st_size, 'rows': 0, 'valid_rows': 0,
        'invalid_rows': 0, 'header_rows': 0,
    }
    try:
        with ZipFile(source.path) as archive:
            members = archive.infolist()
            if (len(members) != 1 or members[0].is_dir()
                    or members[0].filename.casefold() != _member_name(source).casefold()):
                raise ConversionError('archive has an unexpected data member')
            member = members[0]
            if member.file_size > _MAX_MEMBER_BYTES:
                raise ConversionError('archive exceeds expanded file limit')
            expected_width = 57 if version == 1 and dataset == 'events' and len(source.stamp) < 8 else width
            legacy_gkg = dataset == 'gkg' and version == 1
            prefix = source.name.encode() + b'\t' + source.stamp.encode() + b'\t'
            consumed = 0
            with archive.open(member) as reader:
                for index, raw in enumerate(iter(lambda: reader.readline(_MAX_LINE_BYTES + 1), b''), 1):
                    if index % 1024 == 1:
                        _stopping(stop)
                    consumed += len(raw)
                    if len(raw) > _MAX_LINE_BYTES:
                        raise ConversionError('archive row exceeds size limit')
                    if index == 1 and legacy_gkg:
                        if tuple(raw.decode('ascii', 'replace').strip().upper().split('\t')) != _GKG_V1_HEADER:
                            raise ConversionError('unexpected GKG 1.0 header')
                        info['header_rows'] += 1
                        continue
                    info['rows'] += 1
                    error, line = _fields(raw, expected_width)
                    if error:
                        info['invalid_rows'] += 1
                        info_row = {'archive_name': source.name, 'archive_stamp': source.stamp, 'archive_row': index,
                                    'error': error, 'sha256': sha256(raw).hexdigest(), 'raw_base64': b64encode(raw).decode()}
                        bad_rows.write(json.dumps(info_row, separators=(',', ':')).encode() + b'\n')
                        continue
                    info['valid_rows'] += 1
                    # Old Events omitted source_url; append an empty placeholder.
                    if expected_width != width:
                        line += b'\t'
                    rows.write(prefix + str(index).encode() + b'\t' + line + b'\n')
            if consumed != member.file_size:
                raise ConversionError('archive expanded byte count mismatch')
    except (BadZipFile, EOFError) as error:
        raise ConversionError('archive does not pass ZIP integrity checks') from error
    return info


def _write_parquet(rows_path: Path, parquet: Path, columns: tuple[str, ...], count: int, stage: Path):
    import duckdb

    names = (*_PROVENANCE, *columns)
    types = {name: 'BIGINT' if name == 'archive_row' else 'VARCHAR' for name in names}
    with duckdb.connect(':memory:') as connection:
        connection.execute("SET threads=2")
        connection.execute("SET memory_limit='512MB'")
        connection.execute("SET temp_directory=?", [str(stage / 'duckdb-tmp')])
        if count:
            # Validation uses Python's byte reader; DuckDB gets only verified UTF-8
            # rows and does one bulk, quote-free streaming scan. Preserve empties.
            relation = connection.read_csv(
                str(rows_path), columns=types, force_not_null=list(columns), header=False,
                delimiter='\t', quotechar='', escapechar='', parallel=False, max_line_size=17 * 1024 ** 2,
                buffer_size=72 * 1024 ** 2, ignore_errors=False,
            )
            relation.create_view('validated_archive')
            connection.execute(f'COPY validated_archive TO ? ({_PARQUET_OPTIONS})', [str(parquet)])
        else:
            projected = ', '.join(f'CAST(NULL AS {type_}) AS "{name}"' for name, type_ in types.items())
            connection.execute(f'COPY (SELECT {projected} WHERE FALSE) TO ? ({_PARQUET_OPTIONS})',
                               [str(parquet)])
        verified = connection.execute('SELECT count(*) FROM read_parquet(?)', [str(parquet)]).fetchone()[0]
        if verified != count:
            raise ConversionError('published row count differs from validated archive count')


def _synced(path: Path):
    with path.open('rb') as stream:
        os.fsync(stream.fileno())


def _directory_synced(path: Path):
    if hasattr(os, 'O_DIRECTORY'):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _read_published(path: Path, identity: dict, *, verify_digest=True, stop=None):
    metadata = json.loads((path / 'manifest.json').read_text(encoding='utf-8'))
    if not isinstance(metadata, dict) or any(metadata.get(key) != value for key, value in identity.items()):
        raise ConversionError('existing archive publication has conflicting source identity')
    parquet = path / 'data.parquet'
    invalid = int(metadata['invalid_rows'])
    bad_rows = path / 'invalid.jsonl.gz' if invalid else None
    if (not parquet.is_file() or parquet.stat().st_size != metadata.get('parquet_bytes') or
            (bad_rows is not None and (not bad_rows.is_file() or bad_rows.stat().st_size != metadata.get('bad_row_bytes')))):
        raise ConversionError('existing archive publication is incomplete')
    if verify_digest and (
        _source_hash(parquet, stop) != metadata.get('parquet_sha256')
        or (bad_rows is not None and _source_hash(bad_rows, stop) != metadata.get('bad_row_sha256'))
    ):
        raise ConversionError('existing archive output hash mismatch')
    return PublishedGroup(path, parquet, bad_rows, metadata, int(metadata['rows']), int(metadata['valid_rows']), invalid)


def discard_staging(archive_root: Path) -> None:
    """Clear interrupted conversions while holding the archive worker lock."""
    root_work = Path(archive_root) / '.conversion'
    if root_work.is_dir():
        for child in root_work.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)


def convert_group(archive_root: Path, dataset: str, version: int, group: str, sources: list[SourceZip], *, stop=None) -> PublishedGroup:
    """Publish one immutable UTC day or original legacy package; retain all inputs.

    Catalog owners only register the result after this returns. It is safe to
    repeat with the exact same source hashes without rereading original ZIPs.
    """
    columns = source_columns(dataset, version)
    _check_date(group)
    if not sources or len(sources) != len({source.name.casefold() for source in sources}):
        raise ValueError('archive group needs distinct source ZIPs')
    sources = sorted(sources, key=lambda source: (source.stamp, source.name))
    for source in sources:
        _validate_source(source, dataset, version, group)
    source_identity = [[source.name, source.stamp, source.sha256] for source in sources]
    identity = {'conversion_version': _CONVERSION_VERSION, 'dataset': dataset, 'upstream_version': version,
                'group': group, 'input_identity': source_identity, 'columns': list(columns)}
    group_id = sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
    partition = (
        Path(archive_root) / 'parquet' / f'v{version}' / dataset / f'archive_year={group[:4]}' /
        f'archive_month={group[4:6] or "00"}' / f'archive_period={group}'
    )
    partition.mkdir(parents=True, exist_ok=True)
    target = partition / f'batch-{group_id}'
    if target.exists():
        return _read_published(target, identity, stop=stop)
    _stopping(stop)
    root_work = Path(archive_root) / '.conversion'
    root_work.mkdir(parents=True, exist_ok=True)
    stage = Path(mkdtemp(prefix=f'{group}-', dir=root_work))
    try:
        normalized = stage / 'validated.tsv'
        rejected = stage / 'invalid.jsonl.gz'
        with normalized.open('wb') as accepted_file, gzip.open(rejected, 'wb') as rejected_file:
            receipts = [
                _write_source(source, dataset, version, len(columns), accepted_file, rejected_file, stop)
                for source in sources
            ]
        _stopping(stop)
        stats = {key: sum(receipt[key] for receipt in receipts) for key in ('rows', 'valid_rows', 'invalid_rows')}
        parquet = stage / 'data.parquet'
        _write_parquet(normalized, parquet, columns, stats['valid_rows'], stage)
        _stopping(stop)
        normalized.unlink()
        shutil.rmtree(stage / 'duckdb-tmp', ignore_errors=True)
        if not stats['invalid_rows']:
            rejected.unlink()
        manifest = stage / 'manifest.json'
        metadata = {**identity, **stats, 'sources': receipts, 'parquet_bytes': parquet.stat().st_size,
                    'parquet_sha256': _source_hash(parquet, stop), 'bad_row_bytes': 0, 'bad_row_sha256': ''}
        if rejected.exists():
            metadata.update(bad_row_bytes=rejected.stat().st_size, bad_row_sha256=_source_hash(rejected, stop))
        manifest.write_text(json.dumps(metadata, separators=(',', ':')), encoding='utf-8')
        for item in stage.iterdir():
            _synced(item)
        _directory_synced(stage)
        try:
            os.rename(stage, target)
        except OSError:
            if not target.is_dir():
                raise
            return _read_published(target, identity, stop=stop)
        _directory_synced(partition)
        return _read_published(target, identity, verify_digest=False)
    finally:
        shutil.rmtree(stage, ignore_errors=True)
