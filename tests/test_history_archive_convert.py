from base64 import b64decode
import gzip
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile

import duckdb
import pytest

from news2douyin.history_archive.convert import (
    ConversionError, SourceZip, convert_group, discard_staging, source_columns,
)


def _archive(folder, name, stamp, contents, member=None):
    path = folder / name
    with ZipFile(path, 'w') as archive:
        archive.writestr(member or name[:-4], contents)
    return SourceZip(path, name, stamp, sha256(path.read_bytes()).hexdigest())


def _line(columns, values):
    return ('\t'.join(values.get(name, '') for name in columns) + '\n').encode('utf8')


def _query(path, select):
    with duckdb.connect(':memory:') as connection:
        return connection.execute(
            f'SELECT {select} FROM read_parquet(?, hive_partitioning=false)', [str(path)]
        ).fetchall()


def test_gkg_day_publishes_all_fields_and_keeps_raw_invalid_rows(tmp_path):
    columns = source_columns('gkg', 2)
    assert len(columns) == 27
    first, second = '20250211000000', '20250211001500'
    valid = _line(columns, {'gkg_record_id': first + '-1', 'themes': 'TEST', 'extras': '<TITLE>中英文 \"text\"</TITLE>'})
    invalid = (valid.replace(b'TEST', b'\xff'), b'too\tfew\r\n')
    s1 = _archive(tmp_path, first + '.gkg.csv.zip', first, valid + invalid[0])
    s2 = _archive(tmp_path, second + '.gkg.csv.zip', second, invalid[1] + valid)
    group = convert_group(tmp_path, 'gkg', 2, '20250211', [s2, s1])

    assert (group.rows, group.valid_rows, group.invalid_rows) == (4, 2, 2)
    assert group.manifest['columns'] == list(columns)
    assert group.manifest['sources'][0]['valid_rows'] == 1
    assert _query(group.parquet, 'archive_stamp, archive_row, themes, persons, extras') == [
        (first, 1, 'TEST', '', '<TITLE>中英文 "text"</TITLE>'),
        (second, 2, 'TEST', '', '<TITLE>中英文 "text"</TITLE>'),
    ]
    assert group.manifest['bad_row_sha256'] == sha256(group.bad_rows.read_bytes()).hexdigest()
    records = [json.loads(row) for row in gzip.decompress(group.bad_rows.read_bytes()).splitlines()]
    assert [(row['archive_row'], row['error'], b64decode(row['raw_base64'])) for row in records] == [
        (2, 'invalid_utf8', invalid[0]), (1, 'unexpected_field_count', invalid[1]),
    ]
    assert s1.path.is_file() and s2.path.is_file(), 'the worker owns input deletion'
    s1.path.unlink()
    assert convert_group(tmp_path, 'gkg', 2, '20250211', [s1, s2]) == group

    # A crash after publish may leave a revision awaiting catalog commit.
    # Reopening it must reject a damaged Parquet file even at the same size.
    binary = group.parquet.read_bytes()
    group.parquet.write_bytes(b'X' + binary[1:])
    with pytest.raises(ConversionError, match='hash mismatch'):
        convert_group(tmp_path, 'gkg', 2, '20250211', [s1, s2])


def test_legacy_schemas_keep_original_fields_and_do_not_invent_mentions(tmp_path):
    columns = source_columns('events', 1)
    assert len(columns) == 58 and len(source_columns('events', 2)) == 61
    assert len(source_columns('mentions', 2)) == 16
    with pytest.raises(ValueError):
        source_columns('mentions', 1)
    annual = _archive(
        tmp_path, '1979.zip', '1979',
        _line(columns[:-1], {'event_date': '19790623', 'event_code': '020'}), member='1979.csv',
    )
    file = convert_group(tmp_path, 'events', 1, '1979', [annual]).parquet
    assert _query(file, 'event_date, event_code, source_url') == [('19790623', '020', '')]

    day = '20130401'
    header = (
        'DATE\tNUMARTS\tCOUNTS\tTHEMES\tLOCATIONS\tPERSONS\tORGANIZATIONS\tTONE\t'
        'CAMEOEVENTIDS\tSOURCES\tSOURCEURLS\n'
    ).encode()
    values = {'document_date': day, 'cameo_event_ids': '123,456', 'source_urls': 'https://example.test/story'}
    legacy = _archive(tmp_path, day + '.gkg.csv.zip', day, header + _line(source_columns('gkg', 1), values))
    group = convert_group(tmp_path, 'gkg', 1, day, [legacy])
    assert group.rows == group.valid_rows == 1 and group.invalid_rows == 0
    assert group.bad_rows is None and group.manifest['sources'][0]['header_rows'] == 1
    assert _query(group.parquet, 'archive_row, cameo_event_ids, source_urls') == [(2, '123,456', values['source_urls'])]


def test_converting_bad_or_paused_input_leaves_no_published_revision(tmp_path):
    stamp = '20250211000000'
    source = _archive(
        tmp_path, stamp + '.export.CSV.zip', stamp, b'junk\n', member='unexpected.txt'
    )
    with pytest.raises(ConversionError):
        convert_group(tmp_path, 'events', 2, '20250211', [source])
    with pytest.raises(InterruptedError):
        convert_group(tmp_path, 'events', 2, '20250211', [source], stop=lambda: True)
    assert not list(tmp_path.rglob('manifest.json'))
    assert source.path.is_file()
    stale = tmp_path / '.conversion' / 'interrupted-one'
    stale.mkdir()
    (stale / 'validated.tsv').touch()
    discard_staging(tmp_path)
    assert not stale.exists()
    assert source.path.is_file()


def test_zero_valid_rows_still_writes_queryable_parquet_schema_and_sidecar(tmp_path):
    stamp = '20250211000000'
    source = _archive(tmp_path, stamp + '.mentions.CSV.zip', stamp, b'invalid')
    group = convert_group(tmp_path, 'mentions', 2, stamp[:8], [source])
    assert group.rows == group.invalid_rows == 1 and group.valid_rows == 0
    assert _query(group.parquet, 'global_event_id') == []
    assert group.bad_rows is not None
