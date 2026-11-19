import csv
from datetime import date, datetime, timezone
import json
import re

import pytest

from news2douyin.digest.evaluation import score_review
from news2douyin.digest.ranking import Candidate, rank_day
from news2douyin.digest.shortlist import render_shortlist
from news2douyin.digest.shortlist_comparison import _published_window, export_comparison
from news2douyin.digest.strategies import DEFAULT_STRATEGY, STRATEGIES

DAY = date(2026, 11, 15)


def row(key, title, **extra):
    return Candidate(str(key), title, '2026-11-15T00:00:00Z', f'https://paper.example/story/{key}',
                     'gkg', **extra)


def issue_for(rows):
    return dict(rank_day(rows), day=DAY.isoformat(), status='ready', revision=4,
                edition_kind='intraday', cutoff_at='2026-11-15T01:00:00Z')


def test_complete_packet_keeps_members_beyond_per_seat_cap_and_scores_offline_form(tmp_path, monkeypatch):
    from news2douyin.digest import shortlist_trial
    monkeypatch.setattr(shortlist_trial, 'MAX_SAVED_MEMBERS', 1)
    rows = [row(i, title, impact=1) for i, title in enumerate([
        'French Senate approves the Normandy rail project',
        'The Normandy rail project was approved by the French Senate',
        'French Senate gave its approval to the Normandy rail project',
        'French Senate rejects the Normandy rail project',
    ])]
    rows += [row(i, f'Regional dispatch office {i}') for i in range(10, 125)]
    issue = issue_for(rows)
    output = tmp_path/'bundle'
    report = export_comparison(DAY, issue, iter(rows), set(), output, outside=2)
    assert list(report['arms']) == ['baseline', 'classification', 'merge', 'combined']
    assert report['cutoff_exclusive'] == '2026-11-15T01:00:01+00:00'
    assert all(len(a['items']) == 100 for a in report['arms'].values())
    assert report['arms']['baseline']['items'] == issue['items']
    assert len(report['arms']['combined']['changes']['entering']) == 2
    pairs = [json.loads(line) for line in (output/'merges.jsonl').read_text().splitlines()]
    assert len(pairs) == 4 and {p['variant'] for p in pairs} == {'merge','combined'}
    with (output/'merge-review.csv').open(encoding='utf-8-sig') as handle:
        reviews = list(csv.DictReader(handle))
    assert {(r['variant'],r['member_id']) for r in reviews} == {(p['variant'],p['member']['key']) for p in pairs}
    assert all(r['same_development'] == '' and r['member_url'].startswith('https://') for r in reviews)
    md = (output/'shortlist.md').read_text()
    assert len(re.findall(r'^## \d{3}\. ', md, re.M)) == 100 and '试验' in md and '第 4 版' in md
    assert '同一行动候选　选席时并入 2 组，附 1 组材料' in md and 'merges.jsonl' in md
    online_markdown = render_shortlist(dict(issue, items=report['arms']['combined']['items']))
    assert '同一行动候选　选席时并入 2 组，附 1 组材料' in online_markdown
    assert 'merges.jsonl' not in online_markdown
    assert 'French Senate rejects the Normandy rail project' in md
    assert 'comparison.json' not in (output/'review.html').read_text()
    data = json.loads((output/'answer-key.json').read_text())
    ids = data['days'][str(DAY)]['lists']['combined']
    rated = tmp_path/'ratings.csv'
    with rated.open('w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(('id','day','important','fact_group','note'))
        for key in ids[:2]:
            writer.writerow((key,str(DAY),'1','-',''))
    result = score_review(output, rated)['outcomes']['combined']
    assert result['important']['positive'] == 2
    assert result['confirmed_duplicate_slots'] == 0 and result['deep']['unjudged'] == 100
    with pytest.raises(FileExistsError):
        export_comparison(DAY, issue, [], set(), output)


def test_refuses_drift_without_partial_files_and_handles_empty_day(tmp_path):
    issue = issue_for([row(1,'An independent regional news report')])
    with pytest.raises(ValueError, match='差异'):
        export_comparison(DAY, issue, [], set(), tmp_path/'drift')
    with pytest.raises(ValueError, match='日期不同'):
        export_comparison(DAY, dict(issue,day='2026-11-14'), [], set(), tmp_path/'date')
    issue['items'][0]['score'] += 1
    with pytest.raises(ValueError, match='排序依据'):
        export_comparison(DAY, issue, [row(1,'An independent regional news report')], set(), tmp_path/'score')
    assert not list(tmp_path.iterdir())
    empty = tmp_path/'empty'
    report = export_comparison(DAY, issue_for([]), [], set(), empty)
    assert not report['arms']['combined']['items'] and '0 条' in (empty/'shortlist.md').read_text()
    assert (empty/'merges.jsonl').read_text() == ''


def test_comparison_honors_published_budget_and_rejects_another_strategy(tmp_path):
    rows = [row(i, f'Regional funding decision number {i}') for i in range(12)]
    issue = issue_for(rows)
    issue.update(rank_day(rows, limit=3))
    issue['stats']['selection'] = dict(strategy=DEFAULT_STRATEGY, version=STRATEGIES.get(DEFAULT_STRATEGY).version,
                                      options={}, limit=3, api_version=1)
    report = export_comparison(DAY, issue, iter(rows), set(), tmp_path/'small', outside=0)
    assert [len(arm['items']) for arm in report['arms'].values()] == [3, 3, 3, 3]
    assert 'Top 3' in (tmp_path/'small'/'shortlist.md').read_text()

    def unused_stream():
        raise AssertionError('Read candidates for an incompatible baseline')
        yield

    issue['stats']['selection']['strategy'] = 'distinct_regions'
    with pytest.raises(ValueError, match='distinct_regions'):
        export_comparison(DAY, issue, unused_stream(), set(), tmp_path/'other')
    assert not (tmp_path/'other').exists()


def test_legacy_closed_and_repaired_windows_are_explicit():
    day = date(2025, 3, 18)
    issue = dict(status='ready', edition_kind='closed', stats={'sources':{'gkg_restored_batches':{'gkg_english:20250318010000':'2025-06-10T10:10:00Z'}}})
    ceiling, repaired = _published_window(issue, day)
    assert ceiling == datetime(2025,3,18,16,tzinfo=timezone.utc)
    assert repaired == [('gkg_english','20250318010000')]
    with pytest.raises(ValueError, match='截止时间'):
        _published_window(dict(issue, edition_kind='intraday'), day)
