import csv
import json
import sqlite3
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from news2douyin.digest.daily_review import export_daily, format_daily, score_daily
from news2douyin.digest.ranking import Candidate, rank_day


DAY = date(2025, 3, 16)


def candidate(i, day=DAY):
    return Candidate(f'gdelt:{i}', f'City council votes on proposal number {i}', f'{day}T04:00:00Z',
                     f'https://press{i}.example/story', 'gdelt', country='US', excerpt=f'Report number {i}')


def bundle(tmp_path):
    items = rank_day((candidate(i) for i in range(3)), mode='balanced')['items']
    def published(day):
        if day == DAY:
            return dict(status='ready', generated_at='2025-03-17T00:01:00Z',
                        items=[dict(row, excerpt='Frozen source excerpt') for row in items])
        return dict(status='pending')
    seen = []
    def source(day, counts):
        seen.append(day)
        yield from (candidate(i, day) for i in range(8))
    out = tmp_path/'review'
    key = export_daily([DAY, DAY+timedelta(days=1)], source, out, issue_for_day=published, outside=4)
    assert seen == [DAY]
    return out, key['days'][str(DAY)]


def write_ratings(path, changes):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.DictReader(handle)
        fields = reader.fieldnames
        rows = list(reader)
    for row in rows:
        row.update(changes.get(row['id'], {}))
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def test_actual_snapshot_blindness_duplicate_and_sampled_miss(tmp_path):
    out, key = bundle(tmp_path)
    a,b,c = key['selected']
    d,e,f,g = key['sampled']
    assert key['outside_groups'] == 5
    rows = write_ratings(out/'review.csv', {
        a:{'important':'1','fact_group':'ferry'},
        b:{'important':'0','fact_group':'ferry'},
        d:{'important':'1','fact_group':'ferry'},
        e:{'important':'1','fact_group':'other'},
        f:{'important':'1','fact_group':'-'},
    })
    assert (out/'review.html').is_file()
    assert {r['excerpt'] for r in rows if r['id'] in (a,b,c)} == {'Frozen source excerpt'}
    assert all(x not in rows[0] for x in ('rank', 'selected', 'score', 'reasons'))
    report = score_daily(out)
    assert report['edition'] == 'published'
    selected = report['days'][str(DAY)]['selected']
    assert selected == dict(total=3,worthwhile=1,judged=2,unjudged=1,
                            duplicate_slots=1,grouping_checked=2,grouping_unjudged=1,
                            confirmed_worthwhile_groups=1,worthwhile_grouping_pending=0)
    outside = report['days'][str(DAY)]['outside_sample']
    assert (outside['population'],outside['judged'],outside['unjudged']) == (5,3,1)
    assert (outside['confirmed_missed_cards'], outside['covered_worthwhile_cards'],
            outside['unresolved_worthwhile_cards']) == (1,1,1)
    assert [r['id'] for r in report['days'][str(DAY)]['missed_examples']] == [f]
    assert report['days'][str(DAY+timedelta(days=1))]['status'] == 'pending'
    assert '无已发布日报' in format_daily(report)
    # Once every selected slot has been grouped, the new named group is a confirmed miss too.
    write_ratings(out/'review.csv', {c:{'fact_group':'-'}})
    assert score_daily(out)['days'][str(DAY)]['outside_sample']['confirmed_missed_cards'] == 2


def test_replay_day_separation_empty_and_invalid_ratings(tmp_path):
    out = tmp_path/'review'
    source_days = []
    def source(day, counts):
        source_days.append(day)
        return [candidate(1,day), candidate(2,day)]
    manifest = export_daily([DAY,DAY+timedelta(days=1)],source,out,edition='replay',outside=0,limit=1,warmup=1)
    assert len(source_days) == 3
    assert manifest['days'][str(DAY)]['outside_groups'] == 0
    assert manifest['days'][str(DAY+timedelta(days=1))]['outside_groups'] == 0
    assert manifest['days'][str(DAY+timedelta(days=1))]['prior_read_excluded'] == 2
    assert score_daily(out)['days'][str(DAY)]['selected']['unjudged'] == 1
    assert score_daily(out)['days'][str(DAY+timedelta(days=1))]['selected']['total'] == 0
    write_ratings(out/'review.csv', {manifest['days'][str(DAY)]['selected'][0]:{'important':'nan'}})
    with pytest.raises(ValueError, match='important'):
        score_daily(out)


def test_cli_reads_frozen_slots_and_missing_days_without_modifying_db(tmp_path):
    from sqlalchemy import create_engine
    from sqlmodel import Session
    from news2douyin.digest.models import DailyDigestIssue
    from news2douyin.storage.db import create_schema
    db = tmp_path/'archive.db'
    url = f'sqlite:///{db}'
    engine = create_engine(url)
    create_schema(engine)
    title = 'Council approved bridge financing'
    card = dict(key='frozen-10',title=title,source_url='https://city.example/minutes',
                excerpt='Stored at publication.', observed_at=f'{DAY}T01:00:00Z')
    with Session(engine) as session:
        session.add(DailyDigestIssue(day=str(DAY),status='ready',items_json=json.dumps([card]),generated_at='real-stamp'))
        session.commit()
    engine.dispose()
    out = tmp_path/'blind'
    command = [sys.executable, '-m', 'news2douyin.digest.review_cli']
    source = Path(__file__).resolve().parents[1] / 'src'
    subprocess.run([*command,'export-daily','--start',str(DAY),'--end',str(DAY+timedelta(days=1)),
                    '--db-url',url,'--storage-root',str(tmp_path),'--out',str(out)],cwd=source,check=True,capture_output=True)
    with (out/'review.csv').open(encoding='utf-8-sig',newline='') as f:
        row = next(csv.DictReader(f))
    assert row['title'] == title
    labels = tmp_path/'ratings.csv'
    labels.write_bytes((out/'review.csv').read_bytes())
    write_ratings(labels,{row['id']:{'important':'1','fact_group':'-'}})
    score = subprocess.check_output([*command,'score-daily',str(out),'--ratings',str(labels),'--json'], cwd=source)
    assert json.loads(score)['days'][str(DAY)]['selected']['worthwhile'] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute('select count(*) from dailydigestissue').fetchone()[0] == 1
        assert conn.execute('pragma journal_mode').fetchone()[0] == 'delete'


def test_published_aliases_and_prior_days_are_excluded_from_omissions(tmp_path):
    raw_titles = ['(Reuters) EU Regulation 2024/1689 takes effect on 2 August',
                  'EU Regulation 2024/1689 enters into force on 2 August | BBC News',
                  'EU Regulation 2024/1691 enters into force on 2 August']
    def row(title, number):
        return Candidate(f'gkg:{number}',title,f'{DAY}T02:00:00Z',f'https://paper{number}.example/bill','gkg')
    former = rank_day([row(raw_titles[0],0)],mode='balanced_v032')['items']
    present = rank_day([candidate(9)],mode='balanced_v032')['items']
    def published(day):
        slots = former if day == DAY - timedelta(days=1) else present if day == DAY else []
        return dict(status='ready',items=slots,generated_at='frozen')
    def source(day, counts):
        return [row(raw_titles[1],1),row(raw_titles[2],2),candidate(9)]
    info = export_daily([DAY],source,tmp_path/'pool',issue_for_day=published)['days'][str(DAY)]
    assert info['prior_read_excluded'] == 1
    assert info['outside_groups'] == len(info['sampled']) == 1


def test_version_comparison_counts_merged_progress_once(tmp_path):
    from news2douyin.digest.evaluation import export_review, score_review
    titles = ['(Reuters) EU Regulation 2024/1689 takes effect on 2 August',
              'EU Regulation 2024/1689 enters into force on 2 August | BBC News',
              'EU Regulation 2024/1689 enters into force on 2 August']
    source = lambda day, counts: [Candidate(f'gkg:{i}',title,f'{DAY}T01:00:00Z',f'https://paper{i}.example/bill','gkg')
                                  for i,title in enumerate(titles)]
    manifest = export_review([DAY],source,tmp_path/'versions')
    group = manifest['days'][str(DAY)]
    assert len(group['lists']['balanced_v032']) == 3
    assert len(group['lists']['balanced']) == 1
    assert group['audit_sample'] == []
    result = score_review(tmp_path/'versions')
    assert result['comparison_031_032']['shared_progress'] == 3
    assert result['comparison_032_033']['shared_progress'] == 1
    assert result['comparison_032_033']['updated_material_in_shared_progress'] >= 0


def test_review_uses_readonly_duckdb_with_concurrent_archive_reader(tmp_path):
    import duckdb
    from news2douyin.intake.lake import Lake
    from news2douyin.digest.archive import iter_gkg
    lake = Lake(tmp_path)
    with lake.connection(create=True):
        pass
    # DuckDB rejects opening the same file simultaneously with incompatible access modes.
    with duckdb.connect(str(lake.path), read_only=True):
        counts = {}
        assert list(iter_gkg(tmp_path, DAY, stats=counts, read_only=True)) == []
        assert counts['gkg_archive_present']
