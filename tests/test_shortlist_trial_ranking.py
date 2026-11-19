from collections import Counter
from dataclasses import replace

import pytest

from news2douyin.digest.ranking import Candidate, grouped_candidates, rank_day
from news2douyin.digest.shortlist_trial import GENRE_POINTS


def report(i, title=None, **kwargs):
    return Candidate(key=f'gkg:fixture:{i}', source_url=f'https://publisher.test/story/{i}',
                     title=title or f'Municipal bulletin field note number {i}',
                     source_kind='gkg', observed_at='2026-11-15T04:00:00Z', **kwargs)


def test_all_arms_isolate_production_metadata_and_representative_identity():
    rows = [report(1, 'Alpine regional fuel reserve', coarse=dict(genre='promotion', genre_basis='x')),
            report(2, 'Alpine regional fuel reserve', excerpt='Attached to this edition.'),
            report(3, 'Committee approves emergency reserve',
                   coarse=dict(category='公共服务', mention_country='DE', genre='reported_action',
                               genre_basis='Committee approves'))]
    baseline = rank_day([replace(row, coarse=None) for row in rows])
    assert rank_day(rows) == baseline
    with grouped_candidates(rows, shortlist_trial=True, keep_baseline=False) as grouped:
        for variant in ['classification', 'merge', 'combined']:
            result = grouped.select_shortlist_trial(variant=variant)
            assert len(result['items']) == 2
            representative = next(i for i in result['items'] if i['source_key'] == rows[1].key)
            assert representative['shortlist_trial']['coarse'] == {}
            assert grouped.select() == baseline
            excluded_key = result['items'][0]['key']
            partial = grouped.select_shortlist_trial(variant=variant, prior_keys=[excluded_key])
            assert partial['stats']['prior_excluded'] == 1 and len(partial['items']) == 1
            assert all(item['key'] != excluded_key for item in partial['items'])
    with grouped_candidates(rows) as grouped:
        with pytest.raises(ValueError, match='shortlist_trial'):
            grouped.select_shortlist_trial()


def test_bucket_heads_equal_exhaustive_marginal_coverage_and_ignore_arrival_order():
    # Every group has a different old lane. Vary genuine metadata, include unknown and soft genres.
    rows = []
    for i in range(87):
        feature = dict(genre=('','promotion','review','service','forecast','reported_action')[i%6],
                       category=('', '科技', '外交', '科技', '健康')[i%5],
                       mention_country=('', 'DE', 'US', 'US', 'ID', 'FR', '')[i%7])
        rows.append(report(i, coarse=feature))
    selected = []
    with grouped_candidates(rows, keep_baseline=False, shortlist_trial=True) as grouped:
        expected = grouped.select(limit=100)
        all_cards = {c['source_key']: c for c in expected['items']}
        by_key = {all_cards[r.key]['key']: r.coarse for r in rows}
        beats, countries = Counter(), Counter()
        remaining = dict(by_key)
        for _ in range(61):
            def priority(k):
                f = remaining[k]
                genre = GENRE_POINTS.get(f['genre'], 0)
                # All candidates have exactly the same baseline score and timestamp here.
                cover = (4/(1+beats[f['category']]) if f['category'] else 0)
                cover += (2/(1+countries[f['mention_country']]) if f['mention_country'] else 0)
                return -(genre + cover), -genre, k
            best = min(remaining, key=priority)
            f = remaining.pop(best)
            beats[f['category']] += 1
            countries[f['mention_country']] += 1
            selected.append(best)
        assert [c['key'] for c in grouped.select_shortlist_trial(variant='classification',limit=61)['items']] == selected
        assert grouped.select_shortlist_trial(limit=0)['items'] == []
        with pytest.raises(ValueError, match='variant'):
            grouped.select_shortlist_trial(variant='bad')
        with pytest.raises(ValueError, match='limit'):
            grouped.select_shortlist_trial(limit=101)
    with grouped_candidates(reversed(rows), keep_baseline=False, shortlist_trial=True) as grouped:
        assert [c['key'] for c in grouped.select_shortlist_trial(variant='classification',limit=61)['items']] == selected


def test_refill_considers_full_pool_and_audit_receives_each_absorbed_observation(monkeypatch):
    from news2douyin.digest import action_groups, shortlist_trial
    # Exercise the selector independently of the linguistic matching rules.
    class TestIndex:
        def __init__(self):
            self.representative = None
        def add(self, card):
            if card['title'].startswith('River Basin'):
                self.representative = card['key']
        def find(self, card):
            if self.representative and card['title'].startswith('River Basin'):
                return self.representative, 'same test incident'
            return None
    monkeypatch.setattr(action_groups,'ActionSeatIndex',TestIndex)
    monkeypatch.setattr(shortlist_trial,'MAX_SAVED_MEMBERS',2)
    rows = [report(i,f'River Basin barrier dispatch {i}',impact=0.9) for i in range(132)]
    rows += [report(303, 'A distinct water regulator issues its summary')]
    seen = []
    with grouped_candidates(rows, keep_baseline=False, shortlist_trial=True) as grouped:
        assert len(grouped.select_shortlist_trial(variant='classification',limit=2)['items']) == 2
        result = grouped.select_shortlist_trial(variant='merge',limit=2,on_merge=seen.append)
    head, tail = result['items']
    annotation = head['shortlist_trial']
    assert tail['source_key'] == rows[-1].key
    assert result['stats']['candidates_examined'] == 133
    assert result['stats']['unexamined'] == 0
    assert result['stats']['merged_count'] == annotation['merged_count'] == len(seen) == 131
    assert annotation['merged_members_truncated'] and len(annotation['merged_members']) == 2
    assert set(x['representative_key'] for x in seen) == {head['key']}
    assert len({x['member']['key'] for x in seen}) == 131
    assert all(x['member']['sources'][0]['url'] == x['member']['source_url'] for x in seen)
