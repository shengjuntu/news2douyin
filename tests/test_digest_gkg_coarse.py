from dataclasses import replace
from datetime import date
import hashlib

from news2douyin.digest.archive import iter_gkg
from news2douyin.digest.coarse import headline_genre
from news2douyin.digest.geography import enrich_selected
from news2douyin.intake.lake import Lake
from test_intake_projection import line
from test_intake_storage_v2 import STAMP, legacy, raw_zip


DAY = date(2026, 11, 4)


def test_trial_classification_keeps_admission_timing_and_legacy_fields(tmp_path):
    lake = Lake(tmp_path)
    rows = []
    for i, title in enumerate(['Regional summit opens in South Korea',
                               'Film review: An election changes everything',
                               'Storm warning issued', 'Policy report'], 1):
        cells = line(STAMP, i, title).rstrip('\n').split('\t')
        cells[7:15] = ['ELECTION;GENERAL_HEALTH', 'ELECTION,12;ELECTION,22;GENERAL_HEALTH,10',
                       '1#United States#US#0#0#0#0',
                       '4#Seoul#KS#KS11#1#2#3#4#15;4#Seoul#KS#KS11#1#2#3#4#6',
                       'Reporter A;Same Name', '', 'Same Name;Union B', '']
        rows.append(('\t'.join(cells) + '\n').encode())
    lake.ingest_archive('gkg_english', STAMP, raw_zip(tmp_path, b''.join(rows)))
    with lake.connection() as db:
        db.execute("UPDATE intake_archives SET completed_at='2026-11-04T12:00:00Z'")
        db.execute("UPDATE intake_records_v2 SET raw_sha256='bad' WHERE row_index=2")
        db.execute("UPDATE intake_records_v2 SET parser_version='future' WHERE row_index=3")
        db.execute("""UPDATE intake_records_v2 SET metadata_overlay_json=? WHERE row_index=4""",
                   ['{"remove":["persons","themes","enhanced_themes"],"set":{"themes":["MEDICAL"]}}'])
    before = hashlib.sha256(lake.path.read_bytes()).hexdigest()
    stats = {}
    old = list(iter_gkg(tmp_path, DAY, read_only=True))
    new = list(iter_gkg(tmp_path, DAY, read_only=True, coarse=True, stats=stats))
    assert [replace(c, coarse=None) for c in new] == old
    assert [r.key for r in new] == [f'gkg:gkg_english:{STAMP}:{i}' for i in range(1,5)]
    first = new[0]
    card = dict(source_kind='gkg', source_key=first.key, source_url=first.source_url)
    enrich_selected([card], storage_root=tmp_path)
    assert first.coarse['category'] == card['category']['label'] == '政治与治理'
    assert first.coarse['theme'] == card['category']['theme'] == 'ELECTION'
    assert first.coarse['mention_country'] == card['geography']['primary'] == 'KR'
    assert first.coarse['entities'] == ['Reporter A','Same Name','Union B']
    assert first.country == first.channel == '' and not first.actors
    assert first.coarse['genre'] == 'reported_action'
    assert new[1].coarse == {'genre':'review','genre_basis':'Film review:'}
    assert not new[2].coarse and new[3].coarse['category'] == '健康与医疗'
    assert new[3].coarse['entities'] == ['Same Name','Union B']
    assert stats['gkg_coarse_metadata_read'] == stats['gkg_coarse_metadata_unavailable'] == 2
    assert list(iter_gkg(tmp_path, DAY, read_only=True, cutoff='2026-11-04T12:00:00Z',coarse=True)) == []
    assert hashlib.sha256(lake.path.read_bytes()).hexdigest() == before


def test_trial_legacy_metadata_ties_and_missing_metadata_keep_title(tmp_path):
    raw = line(STAMP, 1, 'Music festival returns').encode()
    lake, _ = legacy(tmp_path, raw, extra=dict(mention_iso=['PL','US'], themes=['MEDICAL','ELECTION'],
                                               enhanced_locations='', enhanced_themes=''))
    legacy_row, = iter_gkg(tmp_path, DAY, read_only=True, coarse=True)
    assert set(legacy_row.coarse or {}).isdisjoint({'mention_country','category'})
    with lake.connection() as db:
        db.execute("UPDATE intake_records SET metadata_json='broken'")
    legacy_row, = iter_gkg(tmp_path, DAY, read_only=True, coarse=True)
    assert not legacy_row.coarse and legacy_row.title == 'Music festival returns'


def test_headline_cues_distinguish_actions_reviews_service_and_uncertain_wording():
    for title, expected in {
        'Leaders open annual summit despite setbacks':'reported_action',
        'Rail services resumed following inspection':'reported_action',
        '两国签署合作协议':'reported_action',
        'Film review: The new parliament':'review',
        'Weekend shoppers find designer handbag slashed to £60':'promotion',
        'How to watch the debate':'service',
        'New taxes might cause factory delays':'forecast',
    }.items():
        got = headline_genre(title)
        assert got['genre'] == expected and got['genre_basis'] in title
    for title in ['Can talks resume?', 'Leaders deny plans to hold a summit',
                  'Senate might pass bill', 'Recalling how summit opened last year',
                  'Summit to open tomorrow', 'Markets rise in May', 'Promotion for health minister']:
        assert headline_genre(title) == {}


def test_explicit_restoration_preserves_historical_clock_and_counts(tmp_path):
    # One batch received three days later; only a named repair can admit it.
    lake = Lake(tmp_path)
    lake.ingest_archive('gkg_english', STAMP, raw_zip(tmp_path, line(STAMP,1,'Senate passes tax bill').encode()))
    with lake.connection() as db:
        db.execute("UPDATE intake_archives SET completed_at='2026-11-07T12:00:00Z'")
    cutoff='2026-11-04T13:00:00Z'
    assert not list(iter_gkg(tmp_path, DAY, read_only=True, coarse=True, cutoff=cutoff))
    row, = iter_gkg(tmp_path, DAY, read_only=True, coarse=True, cutoff=cutoff,
                    restored_stamps=[('gkg_english',STAMP)])
    assert row.source_clock == {'repair_day':'2026-11-04'}
    assert row.observed_at.startswith('2026-11-07') and row.coarse['genre'] == 'reported_action'
