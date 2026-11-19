from datetime import date, datetime, timezone

import pytest
from sqlmodel import Session

from news2douyin.candidates.service import candidate_token, iter_day, list_reports, record_reports, resolve_material
from news2douyin.digest.service import build_issue, default_day
from news2douyin.intake.lake import Lake


def test_collected_reports_keep_source_language_body_and_local_availability(engine):
    observed = '2026-03-14T03:15:30Z'
    before = datetime(2026, 3, 14, 3, 15, 30, tzinfo=timezone.utc)
    after = datetime(2026, 3, 14, 3, 15, 31, tzinfo=timezone.utc)
    raw = dict(title='Waziri wa fedha ametoa taarifa mpya', content='Taarifa kamili.\nMaelezo ya pili.',
               content_kind='text', url='https://east.example/story', language='sw', source_language='swa',
               country='tz', fetched_at='2026-03-12T01:00:00Z', published_at='2026-03-12T00:00:00Z')
    with Session(engine) as session:
        rejected = dict(raw, url='https://user:secret@east.example/story')
        first, = record_reports(session, [rejected, raw], provider='worldnewsapi', batch='stable/batch',
                                observed_at=observed)
        replay, = record_reports(session, [rejected, dict(raw, title='Changed on a bad retry')],
                                 provider='worldnewsapi', batch='stable/batch', observed_at=observed)
        assert replay == first
        session.commit()

        row, = list_reports(session, query='taarifa', language='sw', provider='worldnewsapi')
        assert row.key == first and row.title == raw['title']
        assert row.language == 'sw' and row.source_ref['source_language'] == 'swa'
        assert row.country == '' and row.source_ref['media_country'] == 'tz'
        assert row.source_ref['source_observed_at'] == raw['fetched_at']
        assert row.source_ref['row_index'] == 1
        material = resolve_material(session, None, first)
        assert material['content'] == raw['content'] and material['content_kind'] == 'text'
        assert material['published_at'] == raw['published_at'] and material['observed_at'] == observed
        assert candidate_token(material['candidate']) == candidate_token(row)
        assert not list_reports(session, query='_')
        assert not list_reports(session, before=(row.observed_at, row.key))
        with pytest.raises(KeyError):
            resolve_material(session, None, first, cutoff=before)
        assert not list(iter_day(session, None, date(2026, 3, 14), cutoff=before))
        assert [candidate.key for candidate in iter_day(session, None, date(2026, 3, 14),
                                                       since=before, cutoff=after)] == [first]
        assert not list(iter_day(session, None, date(2026, 3, 14), cutoff=after, include_reports=False))


def test_ranker_receives_unassigned_collected_material_and_emits_trace(engine):
    now = datetime(2026, 3, 14, 15, 1, 0, tzinfo=timezone.utc)
    observed = '2026-03-14T15:00:00Z'
    item = dict(title='அமைச்சர் புதிய ஒப்பந்தத்தை அறிவித்தார்', url='https://tamil.example/news',
                content='அறிக்கை', language='ta', source_language='tam', country='in')
    with Session(engine) as session:
        key, = record_reports(session, [item], provider='worldnewsapi', batch='run/1', observed_at=observed)
        session.commit()
    result = build_issue(engine, default_day(now), now=now)
    row, = result['items']
    assert row['source_key'] == key and row['source_kind'] == 'collected'
    assert row['title'] == item['title'] and row['language'] == 'ta'
    assert row['source_ref']['provider'] == 'worldnewsapi' and row['country'] == ''
    assert result['stats']['sources']['candidate_layer'] == 1


def test_exact_archive_key_uses_original_batch_identity_and_receipt(engine, tmp_path):
    day = date(2026, 3, 14)
    stamp = '20260314030000'
    observed = '2026-03-14T03:16:00Z'
    with Lake(tmp_path).connection(create=True) as connection:
        connection.execute(
            'INSERT INTO intake_archives(feed,stamp,sha256,rows,completed_at) VALUES (?,?,?,?,?)',
            ('gkg_english',stamp,'archive-sha',1,observed),
        )
        connection.execute(
            'INSERT INTO intake_records_v2(feed,stamp,row_index,url,title,valid) VALUES (?,?,?,?,?,?)',
            ('gkg_english',stamp,1,'https://dispatch.example/report','Government published new guidelines',True),
        )
    with Session(engine) as session:
        row, = iter_day(session, tmp_path, day)
        material = resolve_material(session, tmp_path, row.key)
        assert material['candidate'].key == row.key and material['url'] == row.source_url
        assert material['content_kind'] == 'metadata' and material['observed_at'] == observed
        assert material['source_ref']['archive_sha256'] == 'archive-sha'
        for bad in (row.key[:-1] + '2', 'gkg:gkg_english:20260314030001:1', 'gkg:events_english:' + stamp + ':1'):
            with pytest.raises(KeyError):
                resolve_material(session, tmp_path, bad)


def test_focus_sees_late_official_and_repaired_gkg_on_local_arrival_day(engine, tmp_path):
    from news2douyin.intake.official_models import OfficialEvent, OfficialFocusUpdate
    from news2douyin.selection.source_models import SourcePreview
    from news2douyin.storage.articles import identity_key

    day = date(2026, 3, 14)
    upstream = '2026-03-13T03:00:00Z'
    seen = '2026-03-14T03:00:00Z'
    receipt = '2026-03-14T03:05:00Z'
    stamp = '20260313030000'
    start = datetime(2026, 3, 14, 3, 0, tzinfo=timezone.utc)
    cutoff = datetime(2026, 3, 14, 4, 0, tzinfo=timezone.utc)
    url = 'https://publisher.example/story'
    lake = Lake(tmp_path)
    with lake.connection(create=True) as connection:
        connection.execute(
            'INSERT INTO intake_archives(feed,stamp,sha256,rows,completed_at) VALUES (?,?,?,?,?)',
            ('gkg_english', stamp, 'repair-sha', 1, receipt),
        )
        connection.execute(
            'INSERT INTO intake_records_v2(feed,stamp,row_index,url,title,valid) VALUES (?,?,?,?,?,?)',
            ('gkg_english', stamp, 1, url, 'Government published new guidelines', True),
        )
        connection.execute('CREATE TABLE intake_digest_repairs (feed VARCHAR, stamp VARCHAR, target_day VARCHAR)')
        connection.execute('INSERT INTO intake_digest_repairs VALUES (?,?,?)', ('gkg_english', stamp, '2026-03-13'))
    with Session(engine) as session:
        session.add_all([
            OfficialEvent(event_id='22', event_date='2026-03-13', last_seen=upstream,
                          feed='events_english', stamp=stamp, row_index=1, source_url=url, ingested_at=seen),
            OfficialFocusUpdate(event_id='22', available_at=receipt),
            SourcePreview(key=identity_key({'url': url}), source_url=url, title='Government published guidelines',
                          fetched_at=seen),
        ])
        session.commit()
        assert not list(iter_day(session, tmp_path, day, since=start, cutoff=cutoff))
        rows = {row.key: row for row in iter_day(session, tmp_path, day, since=start, cutoff=cutoff, receipts=True)}
        assert set(rows) == {'gdelt:22', 'gkg:gkg_english:' + stamp + ':1'}
        assert rows['gdelt:22'].observed_at == upstream
        assert not list(iter_day(session, tmp_path, day, since=start, cutoff=start, receipts=True))
