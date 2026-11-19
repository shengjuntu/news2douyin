from dataclasses import replace

import pytest

from news2douyin.digest.candidate_pool import Candidate
from news2douyin.digest.strategies import DEFAULT_STRATEGY, STRATEGIES, StrategyRegistry


def reports(n):
    for index in range(n):
        yield Candidate(key=f'row-{index}', title=f'Activities reported in district number {index}',
                        source_kind='gkg', source_url=f'https://news.example/reports/{index}',
                        observed_at='2025-03-20T09:00:00Z')


def test_legacy_builtins_reuse_one_pool_and_keep_their_compatibility_output():
    specs = [spec for spec in STRATEGIES.all() if spec.family != 'video']
    material = reports(8)
    ids = [spec.id for spec in specs]
    assert STRATEGIES.requirements(DEFAULT_STRATEGY).coarse is False
    assert STRATEGIES.requirements(ids).keep_baseline and STRATEGIES.requirements(ids).coarse
    with STRATEGIES.prepare(material, ids) as pool:
        for spec in specs:
            actual = STRATEGIES.select(pool, spec.id, limit=3)
            if spec.id.startswith('shortlist_'):
                old = pool.select_shortlist_trial(limit=3, variant=spec.id.removeprefix('shortlist_'))
            else:
                old = pool.select(mode=spec.id, limit=3)
            assert old == actual
            assert len(actual['items']) == 3 and spec.version
            outside = STRATEGIES.select(pool, spec.id, limit=2, prior_keys=[actual['items'][0]['key']])
            assert actual['items'][0]['key'] not in {card['key'] for card in outside['items']}


def test_requirements_fail_before_unavailable_input_is_used():
    with STRATEGIES.prepare(reports(1), DEFAULT_STRATEGY) as pool:
        with pytest.raises(ValueError, match='keep_baseline'):
            STRATEGIES.select(pool, 'balanced_v031')
        with pytest.raises(ValueError, match='shortlist_trial'):
            STRATEGIES.select(pool, 'shortlist_combined')
        with pytest.raises(ValueError, match='unknown'):
            STRATEGIES.select(pool, 'arbitrary.module')
        for n in (False, -1, 0.5):
            with pytest.raises(ValueError, match='limit'):
                STRATEGIES.select(pool, DEFAULT_STRATEGY, limit=n)
        assert STRATEGIES.select(pool, DEFAULT_STRATEGY, limit=0)['items'] == []


def test_new_topn_interface_exceeds_old_hundred_seat_cap():
    ids = [spec.id for spec in STRATEGIES.all() if spec.family != 'video']
    with STRATEGIES.prepare(reports(117), ids) as pool:
        for strategy_id in ids:
            result = STRATEGIES.select(pool, strategy_id, limit=114)
            assert len(result['items']) == len({item['key'] for item in result['items']}) == 114
        assert len(STRATEGIES.select(pool, DEFAULT_STRATEGY, limit=10**30)['items']) == 117


def test_registering_an_internal_grouped_policy_needs_one_callable():
    def reverse_key(pool, request):
        cards = sorted(pool.iter_cards(), key=lambda c: c['key'], reverse=True)
        items = [c for c in cards if c['key'] not in request.prior_keys][:request.limit]
        return {'items': items, 'stats': {'selected': len(items)}}

    prototype = replace(STRATEGIES.get(DEFAULT_STRATEGY), id='reverse_key', run=reverse_key)
    with pytest.raises(ValueError, match='duplicate'):
        StrategyRegistry([prototype, prototype])
    registry = StrategyRegistry([*STRATEGIES.all(), prototype])
    with registry.prepare(reports(5), 'reverse_key') as pool:
        cards = registry.select(pool, 'reverse_key', limit=2)['items']
        assert [c['key'] for c in cards] == sorted((c['key'] for c in pool.iter_cards()), reverse=True)[:2]
