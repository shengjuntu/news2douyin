from dataclasses import replace

import pytest

from news2douyin.digest.ranking import Candidate, grouped_candidates, rank_day


def row(key, title, **changes):
    data = dict(key='gdelt:'+str(key), title=title, observed_at='2026-06-19T08:00:00Z',
                source_url=f'https://news.test/{key}', source_kind='gdelt')
    data.update(changes)
    return Candidate(**data)


def test_same_material_and_cosmetic_titles_merge_while_new_action_is_kept():
    first = row(1, 'Council proposes 15% duty', actors=('Council',))
    duplicate = replace(first, key='local:1', source_kind='local', source_url='https://mirror.test/1',
                        title='COUNCIL proposes 15% duty - Reuters', excerpt='Council released its proposal.')
    changed = replace(first, key='gdelt:2', title='Council approves 15% duty')
    changed_number = replace(first, key='gdelt:3', title='Council proposes 5% duty')
    denial = replace(first, key='gdelt:4', title='Council denies 15% duty')
    question = replace(first, key='gdelt:5', title='Council proposes 15% duty?')
    items = [first, duplicate, changed, changed_number, denial, question]
    result = rank_day(items)
    assert result == rank_day(reversed(items))
    assert result['stats']['groups'] == 5
    grouped = next(item for item in result['items'] if item['observations'] == 2)
    assert grouped['source_key'] == duplicate.key
    assert grouped['sources'][0]['url'] == grouped['source_url']
    assert grouped['domain_count'] == 2
    replay = rank_day([replace(first, observed_at='2026-06-20T15:00:00Z'), changed],
                      prior_keys=[grouped['key']])
    assert [item['title'] for item in replay['items']] == [changed.title]
    assert replay['stats']['prior_excluded'] == 1


def test_only_readable_sources_with_valid_timestamps_enter_list():
    digest = rank_day([
        row(1, 'Access denied'),
        row(2, 'Breaking news'),
        row(3, 'Valid title here', source_url='javascript:alert(1)'),
        row(4, 'Valid link; missing time', observed_at='?'),
        row(5, 'A legal change takes effect', watches=('行业政策',))
    ])
    assert digest['stats']['scanned'] == 5
    assert digest['stats']['invalid'] == 4
    assert digest['stats']['selected'] == 1
    assert digest['items'][0]['watches'] == ['行业政策']


def test_modes_keep_meaning_distinct_and_source_sample_is_bounded():
    one = row(1, 'Port authority adopts new rules', impact=.9, is_primary=True)
    popular = row(2, 'Trade organizations discuss draft')
    rows = [one]+[replace(popular, key=f'local:{n}', source_url=f'https://site{n}.test/story')
                  for n in range(99)]
    with grouped_candidates(rows) as day:
        lead = day.select(mode='breadth', limit=1)['items'][0]
        assert lead['title'] == popular.title
        assert lead['domains_capped']
        assert len(lead['sources']) == 8
        assert len(lead['domains']) == 64
        for mode in ('editorial','balanced'):
            assert day.select(mode=mode, limit=1)['items'][0]['title'] == one.title
        assert len(list(day.iter_cards())) == 2
        excluded = day.select(prior_keys=[lead['key'], lead['key']])
        assert excluded['stats']['eligible'] == 1
        assert day.select()['stats']['eligible'] == 2


def test_set_selection_preserves_distinct_subject_without_fixed_person_quota():
    recurring = [row(i, f'New action number {i}', actors=('Example Leader',), impact=.8) for i in range(140)]
    extra = row('new', 'Port authority changes its schedule', actors=('City port',), impact=.7)
    with grouped_candidates([*recurring, extra]) as day:
        assert extra.title not in {x['title'] for x in day.select(mode='editorial')['items']}
        diverse = day.select(mode='balanced')['items']
        assert len(diverse) == 100
        assert extra.title in {x['title'] for x in diverse[:2]}
        assert sum('本期同一' in reason for x in diverse for reason in x['reasons']) == 98
        assert day.select(limit=0)['items'] == []
        with pytest.raises(ValueError):
            day.select(limit=101)
        with pytest.raises(ValueError):
            day.select(mode='irrelevant')



def test_same_generic_role_title_does_not_merge_unrelated_countries():
    rows = [row(1, 'Prime minister resigns', country='AA'),
            row(2, 'Prime minister resigns', country='BB')]
    assert rank_day(rows)['stats']['groups'] == 2



def test_scope_hint_is_weaker_than_accepted_watch_and_does_not_merge_actions():
    decision = row(1, '特朗普签署关税行政令', watches=('特朗普活动',))
    raw = row(2, '特朗普签署关税行政令', source_kind='gkg', focus_hints=('特朗普活动',))
    second = row(3, '特朗普会晤贸易代表', source_kind='gkg', focus_hints=('特朗普活动',))
    other = row(4, '行业机构公布调查报告')
    result = rank_day([decision, raw, second, other])
    assert result['stats']['groups'] == 3
    cards = {item['title']: item for item in result['items']}
    kept = cards[decision.title]
    assert kept['observations'] == 2 and kept['watches'] == ['特朗普活动']
    assert kept['focus_hints'] == []
    assert cards[second.title]['watches'] == []
    assert cards[second.title]['focus_hints'] == ['特朗普活动']
    assert any('待核' in reason for reason in cards[second.title]['reasons'])
    assert kept['score'] > cards[second.title]['score']


def test_coverage_and_reported_action_are_separate_and_baseline_uses_older_inputs():
    coded = row(1, 'A report about the council', official_coverage=dict(event_id='1', documents=41, domains=12,
                new_documents=8, new_domains=3, history_complete=True))
    recovered = row(2, 'City and council signed the measure on 2026-06-19.', excerpt='Fragment text.',
                    is_primary=False, reconstruction={'provenance': 'gdelt_web_ngrams'}, legacy_eligible=False,
                    action_evidence={'kind': 'reconstructed_action_lead', 'basis': '重建片段待核'})
    action = row(3, 'Council approves the measure', action_evidence={'kind':'dated_action_report',
                 'basis':'关联报道出现当期日期及行动表述', 'url':'https://news.test/3', 'quote':'2026-06-19 council approved the measure'})
    with grouped_candidates([coded,recovered,action]) as pool:
        new = {card['source_key']:card for card in pool.select()['items']}
        old = {card['source_key']:card for card in pool.select(mode='balanced_v031')['items']}
    assert set(old) == {coded.key,action.key} and set(new) == {coded.key,action.key,recovered.key}
    assert new[action.key]['score']-old[action.key]['score'] == 12
    assert 0 < new[coded.key]['score']-old[coded.key]['score'] <= 4
    assert 'action_evidence' not in new[coded.key]
    assert new[recovered.key]['sources'][0]['reconstruction']['provenance'] == 'gdelt_web_ngrams'
    assert any('新增报道域名 3 个' in reason for reason in new[coded.key]['reasons'])


def test_v031_augmented_empty_excerpt_cannot_raise_original_baseline_score():
    original = row(42,'Council reviews a report')
    recovered = replace(original, excerpt='Council met today.', legacy_excerpt='', reconstruction={'language':'en'})
    with grouped_candidates([recovered]) as pool:
        old = pool.select(mode='balanced_v031')['items'][0]
        new = pool.select()['items'][0]
    expected = rank_day([original])['items'][0]['score']
    assert old['score'] == expected and old['excerpt'] == ''
    assert new['excerpt'] == 'Council met today.' and new['score'] > expected


def test_coverage_of_matching_headlines_never_adds_distinct_event_counts():
    title = 'Council updates coastal rules'
    first = row(11,title,official_coverage=dict(event_id='11',documents=4,domains=1,new_documents=4,new_domains=1,history_complete=True))
    second = row(22,title,official_coverage=dict(event_id='22',documents=16,domains=3,new_documents=8,new_domains=2,history_complete=True))
    lead = rank_day([first,second])['items'][0]
    assert lead['official_coverage']['event_id'] == lead['source_key'].split(':')[1]
    assert lead['official_coverage']['documents'] == 16


def test_gkg_actions_use_representative_title_and_beijing_day_and_keep_v031_policy():
    # 16:30 UTC is already June 20 locally. Multiple copies share one signal.
    first = row(1,'2026-06-20 Council ratified import accord',source_kind='gkg',observed_at='2026-06-19T16:30:00Z')
    duplicate = replace(first,key='gkg:wire',source_url='https://wire.test/accord')
    simple = replace(first,key='gkg:other',title='2026-06-20 Council meeting bulletin')
    with grouped_candidates([first,duplicate,simple]) as grouped:
        chosen = {c['title']: c for c in grouped.select()['items']}
        baseline = {c['title']: c for c in grouped.select(mode='balanced_v031')['items']}
    lead = chosen[first.title]
    assert lead['action_evidence']['kind'] == 'dated_action_report'
    assert lead['action_evidence']['url'] == lead['source_url']
    assert lead['score'] == pytest.approx(baseline[first.title]['score'] + 12)
    assert 'official_coverage' not in lead and 'action_evidence' not in baseline[first.title]
    assert 'action_evidence' not in chosen[simple.title]
    for headline in ('Accord to be signed next week', "Council hasn't approved accord", 'Looking back on the signed accord',
                     'Signed accord in 2022 shapes debate', '2026-06-20 Briefing | Last week the council ratified accord',
                     'Ratified trade accord?', 'Council claims accord signed'):
        card = rank_day([replace(first,title=headline)])['items'][0]
        assert 'action_evidence' not in card, headline
    stage = rank_day([replace(first,title='Council ratified shipping accord')])['items'][0]
    assert stage['action_evidence']['kind'] == 'stage_report'
