from datetime import date, datetime, timezone

import duckdb

from news2douyin.history_archive import service
from news2douyin.history_archive.search import find_leads


DAY = date(2022, 2, 23)


def _write(conn, root, name, rows, columns):
    dest = root / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    conn.execute('CREATE TEMP TABLE sample (' + ','.join(column + ' VARCHAR' for column in columns) + ')')
    try:
        conn.executemany('INSERT INTO sample VALUES (' + ','.join('?' for _ in columns) + ')', rows)
        conn.execute('COPY sample TO ? (FORMAT PARQUET)', [str(dest)])
    finally:
        conn.execute('DROP TABLE sample')


def test_search_joins_committed_archives_and_respects_exact_cutoff(tmp_path, monkeypatch):
    root = tmp_path / 'history_archive'
    event_cols = ('global_event_id', 'event_date', 'actor1_name', 'actor2_name',
                  'action_geo_full_name', 'actor1_country_code', 'actor2_country_code',
                  'event_code', 'date_added', 'source_url')
    mention_cols = ('global_event_id', 'mention_time_date', 'mention_type', 'mention_identifier', 'confidence')
    gkg_cols = ('document_date', 'document_identifier', 'persons', 'organizations',
                'locations', 'themes', 'all_names', 'quotations', 'dates', 'extras')
    with duckdb.connect() as conn:
        columns = ('1', '20220223', 'Canada', 'France', 'Paris', 'CAN', 'FRA', '042')
        _write(conn, root, 'events.parquet', [
            (*columns, '20220223081500', 'https://early.test/reference'),
            ('2', *columns[1:], '20220223160500', 'https://late.test/reference'),
        ], event_cols)
        _write(conn, root, 'mentions.parquet', [
            ('1', '20220223140000', '1', 'https://joined.test/reference', '90'),
            ('3', '20220223140000', '1', 'https://gkg-linked.test/reference', '80'),
            ('1', '20220223160000', '1', 'https://late-mention.test/reference', '99'),
            ('1', '20220223130000', '2', 'https://wrong-mention-type.test/reference', '100'),
        ], mention_cols)
        _write(conn, root, 'gkg.parquet', [
            ('20220223100000', 'https://found-by-gkg.test/reference',
             'Canada', '', '', '', '', '', '', ''),
            ('20220223100000', 'https://gkg-linked.test/reference',
             '', '', '', '', '', 'Canada', '', ''),
            ('20220223160000', 'https://late-gkg.test/reference',
             'Canada', '', '', '', '', '', '', ''),
        ], gkg_cols)
        _write(conn, root, 'staged.parquet', [
            ('20220223100000', 'https://staged.test/reference', 'Canada', '', '', '', '', '', '', ''),
        ], gkg_cols)
    def groups(*_):
        return [dict(kind=kind, version=2, path=f'{kind}.parquet',
                     date_from=DAY, date_to=DAY, state='ready')
                for kind in ('events', 'mentions', 'gkg')]
    monkeypatch.setattr(service, 'query_groups', groups)
    result = find_leads(None, tmp_path, DAY, date(2022, 2, 24), ['canada'],
                        cutoff=datetime(2022, 2, 23, 16, tzinfo=timezone.utc))
    assert {item['url'] for item in result.rows} == {
        'https://early.test/reference', 'https://joined.test/reference',
        'https://found-by-gkg.test/reference', 'https://gkg-linked.test/reference'}
    assert next(item for item in result.rows if item['source_kind'] == 'gkg')['event_id'] is None
    assert {item['event_id'] for item in result.rows if item['source_kind'] == 'mentions'} == {'1', '3'}
    assert all(item['state'] == 'covered' for item in result.coverage.values())


def test_search_keeps_legacy_schema_and_rejects_cross_namespace_link(tmp_path, monkeypatch):
    early, after = date(2015, 2, 18), date(2015, 2, 20)
    root = tmp_path / 'history_archive'
    event_cols = ('global_event_id', 'event_date', 'actor1_name', 'actor2_name',
                  'action_geo_full_name', 'actor1_country_code', 'actor2_country_code',
                  'event_code', 'date_added', 'source_url')
    mention_cols = ('global_event_id', 'mention_time_date', 'mention_type', 'mention_identifier', 'confidence')
    with duckdb.connect() as conn:
        _write(conn, root, 'old.parquet', [
            ('1', '20150218', 'Canada', 'France', 'Paris', 'CAN', 'FRA', '042', '20150218', None),
        ], event_cols)
        _write(conn, root, 'later.parquet', [
            ('1', '20150219', 'Morocco', 'Spain', '', 'MAR', 'ESP', '042', '20150219120000', None, '123'),
        ], (*event_cols, 'action_geo_adm2_code'))
        _write(conn, root, 'mentions.parquet', [
            ('1', '20150219120000', '1', 'https://bad-match.test/reference', '95'),
        ], mention_cols)
        _write(conn, root, 'gkg.parquet', [
            ('20150218', 'https://first.test/doc;params<UDIV>https://second.test/doc', 'Canada', '', '', ''),
        ], ('document_date', 'source_urls', 'persons', 'organizations', 'locations', 'themes'))
    monkeypatch.setattr(service, 'query_groups', lambda *_:
        [dict(kind=kind, version=version, path=path, date_from=day, date_to=day, state=state)
         for kind, version, path, day, state in (
             ('events', 1, 'old.parquet', early, 'ready'),
             ('events', 2, 'later.parquet', date(2015,2,19), 'ready'),
             ('mentions', 2, 'mentions.parquet', early, 'partial'),
             ('gkg', 1, 'gkg.parquet', early, 'ready'),
        )])
    result = find_leads(None, tmp_path, early, after, ['canada'])
    assert {item['url'] for item in result.rows} == {'https://first.test/doc;params', 'https://second.test/doc'}
    assert result.coverage['events']['state'] == 'covered'
    assert result.coverage['mentions']['state'] == 'partial'
    assert result.coverage['gkg']['state'] == 'partial'
