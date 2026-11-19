import csv
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from news2douyin.digest.evaluation import MODES, export_review, score_review
from news2douyin.digest.ranking import Candidate


def sample_candidates(day, counts=None):
    records = [
        ('A', 'A trade union ratifies a nationwide labor contract', 'UK', .9, ['trade'], 1, 0),
        ('B', 'Observers release findings from the island vote', 'PL', .1, [], 4, .1),
        ('C', 'Inquiry presents the final air quality report', 'FR', .5, [], 2, 0),
        ('D', 'Delegates agree to restore transborder train traffic', 'CN', .7, [], 3, 0),
        ('E', 'Auditors publish a new fuel efficiency standard', 'DE', .1, [], 1, 0),
        ('F', 'Regional commission confirms environmental restrictions', 'US', .1, [], 1, 0)]
    for key, title, country, impact, watch, domains, attention in records:
        yield Candidate(key=f'gdelt:{key}', title=title, observed_at=f'{day}T09:00:00Z',
                        source_url=f'https://paper.com/{key}', source_kind='gdelt', country=country,
                        domains=tuple(f'{i}.com' for i in range(domains)), watches=tuple(watch),
                        impact=impact, attention=attention or None)


def rows_of(file):
    with file.open(encoding='utf-8-sig', newline='') as fp:
        return list(csv.DictReader(fp))


def test_replay_once_per_day_and_mode_local_history(tmp_path):
    day = date(2026, 1, 2)
    seen = []

    def source(d, counts):
        seen.append(d)
        yield from sample_candidates(d)

    bundle = tmp_path / 'run'
    result = export_review([day, day + timedelta(days=1)], source, bundle, limit=1, outside=2)
    rows = rows_of(bundle / 'review.csv')
    assert seen == [day, day + timedelta(days=1)]
    assert all(not c in rows[0] for c in ('key', 'mode', 'score', 'reasons'))
    assert len({r['id'] for r in rows}) == len(rows)
    titles = {r['id']: r['title'] for r in rows}
    first, second = list(result['days'].values())
    assert len(set(first['audit_sample'])) == len(set(second['audit_sample'])) == 2
    for group in (first, second):
        ranked = {id_ for listing in group['lists'].values() for id_ in listing}
        assert not ranked.intersection(group['audit_sample'])
        assert group['outside_groups'] == 6 - len(ranked)
    for mode in MODES:
        assert titles[first['lists'][mode][0]] != titles[second['lists'][mode][0]]
    assert titles[first['lists']['breadth'][0]].startswith('Observers')
    assert titles[first['lists']['balanced'][0]].startswith('A trade')

    warmed = export_review([day + timedelta(days=1)], sample_candidates, tmp_path / 'warm',
                           limit=1, outside=2, warmup=1)
    assert next(iter(warmed['days'].values())) == second
    with pytest.raises(FileExistsError):
        export_review([day], sample_candidates, bundle)


def test_blind_judgments_group_duplicates_and_leave_unjudged_unknown(tmp_path):
    day = date(2026, 1, 2)
    bundle = tmp_path / 'comparison'
    manifest = export_review([day], sample_candidates, bundle, limit=2, outside=2)
    group = manifest['days'][day.isoformat()]
    picked = group['lists']['balanced']
    excluded = group['audit_sample'][0]
    file = bundle / 'review.csv'
    rows = rows_of(file)
    for row in rows:
        if row['id'] in picked:
            row.update(deep='0', fact_group='same event')
        if row['id'] == picked[0]:
            row.update(important='1', deep='1', repeat='0')
        if row['id'] == excluded:
            row.update(important='1')
    with file.open('w', encoding='utf-8-sig', newline='') as fp:
        writer = csv.DictWriter(fp, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    result = score_review(bundle)['outcomes']
    assert result['balanced']['confirmed_duplicate_slots'] == 1
    assert result['balanced']['deep'] == dict(positive=1, judged=2, unjudged=0, rate=.5)
    assert result['balanced']['judged_important_coverage']['ratio'] == .5
    assert result['audit_sample']['important']['unjudged'] == 1
    assert result['audit_sample']['deep']['rate'] is None


def test_failed_export_preserves_existing_work(tmp_path):
    path = tmp_path / 'untouched'

    def fail(day, counts):
        yield next(sample_candidates(day))
        raise ValueError('incomplete material')

    with pytest.raises(ValueError, match='incomplete material'):
        export_review([date(2026, 1, 1)], fail, path)
    assert not path.exists()


def test_cli_replay_reads_existing_legacy_database_without_journal_changes(tmp_path):
    from news2douyin.storage.db import create_schema
    from sqlalchemy import create_engine

    path = tmp_path / 'example.db'
    engine = create_engine(f'sqlite:///{path}')
    create_schema(engine)
    with engine.begin() as con:
        con.exec_driver_sql('DROP TABLE ngramtarget')
        con.exec_driver_sql('DROP TABLE ngrambudget')
    engine.dispose()
    bundle = tmp_path / 'out'
    source = Path(__file__).resolve().parents[1] / 'src'
    subprocess.run([sys.executable, '-m', 'news2douyin.digest.review_cli', 'export', '--start', '2025-01-01', '--end', '2025-01-01',
                    '--db-url', f'sqlite:///{path}', '--storage-root', str(tmp_path), '--out', str(bundle)],
                   cwd=source, check=True, capture_output=True, text=True)
    assert not rows_of(bundle / 'review.csv')
    with sqlite3.connect(path) as db:
        assert db.execute('pragma journal_mode').fetchone()[0] == 'delete'
        assert db.execute('select count(*) from dailydigestissue').fetchone()[0] == 0
        assert not db.execute("select name from sqlite_master where name='ngramtarget'").fetchone()


def test_warmup_and_boundaries_at_minimum_date(tmp_path):
    from datetime import datetime, timezone
    from news2douyin.digest.calendar import day_bounds_utc
    start, end = day_bounds_utc(date.min)
    assert start == datetime.min.replace(tzinfo=timezone.utc)
    assert day_bounds_utc(date(1, 1, 2))[0] == end
    start, end = day_bounds_utc(date(1988, 4, 17))
    assert end - start == timedelta(hours=23)
    called = []
    def empty(day, counts):
        called.append(day)
        return iter(())
    manifest = export_review([date.min], empty, tmp_path / 'ancient', warmup=7)
    assert called == [date.min] and manifest['warmup_days'] == 0


def test_replay_preserves_different_material_for_shared_headlines_and_reads_old_bundles(tmp_path):
    import json
    day = date(2026, 3, 8)
    bundle = tmp_path / 'new'
    def inputs(d, counts):
        yield Candidate('gdelt:8','Publisher summit coverage','2026-03-08T02:01:00Z','https://press.example/summit','gdelt',
                        excerpt='Leaders signed an accord.',reconstruction={'language':'en'},legacy_excerpt='')
    result = export_review([day],inputs,bundle,limit=1)
    edition = result['days'][str(day)]
    old_id, new_id = edition['lists']['balanced_v031'][0], edition['lists']['balanced'][0]
    assert old_id != new_id and edition['progress'][old_id] == edition['progress'][new_id]
    rows = {row['id']:row for row in rows_of(bundle/'review.csv')}
    assert rows[old_id]['excerpt'] == '' and '重建片段' in rows[new_id]['material_origin']
    comparison = score_review(bundle)['comparison_031_032']
    assert comparison['updated_material_in_shared_progress'] == 1
    assert comparison['entering_v032']['cards'] == comparison['displaced_v031']['cards'] == 0
    # A previously labeled v0.31 bundle need not be re-exported to score it.
    edition['lists'].pop('balanced_v031')
    edition.pop('progress')
    edition['audit_sample'] = [old_id]
    result['schema'] = 'news2douyin.daily-radar-review.v1'
    (bundle/'answer-key.json').write_text(json.dumps(result))
    assert 'comparison_031_032' not in score_review(bundle)


def test_old_policy_bundle_keeps_its_last_version_and_existing_ratings(tmp_path):
    import json

    # A saved v0.33 answer key predates publisher groups. Rank positions and
    # the meaning of its 'balanced' list belong to that original bundle.
    day = '2025-03-18'
    manifest = {
        'schema': 'news2douyin.daily-radar-review.v3', 'scope': 'frozen scope',
        'days': {day: {'lists': {'balanced_v031': ['a'], 'balanced_v032': ['a'],
                                'balanced': ['b']}, 'audit_sample': [],
                       'progress': {'a': 'older', 'b': 'newer'}}},
    }
    answer = tmp_path / 'answer-key.json'
    answer.write_text(json.dumps(manifest))
    csv_path = tmp_path / 'review.csv'
    csv_path.write_text(f'id,day,important,deep,repeat,fact_group\na,{day},0,,,\nb,{day},1,,,\n')
    original = (answer.read_bytes(), csv_path.read_bytes())
    score = score_review(tmp_path)
    assert score['comparison_032_033']['entering_v033']['important'] == dict(positive=1, judged=1, unjudged=0)
    assert score['comparison_032_033']['displaced_v032']['cards'] == 1
    assert 'comparison_033_034' not in score
    assert (answer.read_bytes(), csv_path.read_bytes()) == original
    manifest.pop('scope')
    answer.write_text(json.dumps(manifest))
    assert 'balanced_v033' not in score_review(tmp_path)['scope']


def test_rank_positions_are_compared_within_each_day_including_an_empty_edition(tmp_path):
    import json

    days = {}
    for day, older, newer in [('2025-03-18', ['a','b','c'], ['b','a','d']),
                               ('2025-03-19', ['e','f'], ['e']),
                               ('2025-03-20', [], [])]:
        days[day] = dict(lists=dict(balanced_v033=older, balanced=newer), audit_sample=[])
    (tmp_path/'answer-key.json').write_text(json.dumps(dict(days=days)))
    (tmp_path/'review.csv').write_text('id,day,important,deep,repeat\n')
    comparison = score_review(tmp_path)['comparison_033_034']
    assert comparison['paired_slots'] == 4
    assert comparison['identical_cards_at_same_rank'] == 1
    assert comparison['identical_cards_at_changed_rank'] == 2
    assert comparison['entering_v034']['cards'] == 1
    assert comparison['displaced_v033']['cards'] == 2


def test_group_weighting_creates_a_blind_comparison_with_the_v033_policy(tmp_path):
    day = date(2025, 3, 18)
    syndicated = 'Regional radio stations publish storm coverage'
    separate = 'Independent coastal outlets publish storm update'
    hosts = ['wflanews.iheart.com', 'kfiam640.iheart.com', 'wrko.iheart.com', 'press.example', 'chronicle.example']

    def inputs(day, counts):
        for index, host in enumerate(hosts):
            yield Candidate(f'gkg:{index}', syndicated if index < 3 else separate,
                            f'{day}T01:00:00Z', f'https://{host}/story', 'gkg')

    path = tmp_path / 'policies'
    answer = export_review([day], inputs, path, limit=1, outside=0)
    group = answer['days'][str(day)]
    rows = {r['id']: r for r in rows_of(path / 'review.csv')}
    assert rows[group['lists']['balanced_v033'][0]]['title'] == syndicated
    assert rows[group['lists']['balanced'][0]]['title'] == separate
    assert all('publisher' not in r and 'score' not in r and 'mode' not in r for r in rows.values())
    comparison = score_review(path)['comparison_033_034']
    assert comparison['entering_v034']['cards'] == comparison['displaced_v033']['cards'] == 1
    assert comparison['paired_slots'] == 1 and comparison['identical_cards_at_same_rank'] == 0
