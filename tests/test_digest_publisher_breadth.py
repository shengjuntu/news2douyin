from dataclasses import replace

from news2douyin.digest.publisher_families import IHEART_HOSTS, MAP_VERSION
from news2douyin.digest.ranking import Candidate, grouped_candidates, rank_day
from news2douyin.digest.source_breadth import HostSample


def story(host, key='story', title='Regional organizers discuss the radio schedule'):
    return Candidate(key=key, title=title, source_url='https://' + host + '/news/item',
                     source_kind='gkg', observed_at='2025-03-18T12:00:00Z')


def test_five_reprints_and_another_publisher_preserve_raw_evidence():
    stations = sorted(IHEART_HOSTS)[:5]
    rows = [story(host, host) for host in stations]
    split = [story(h, h, 'Regional organizers discuss the Sunday schedule')
             for h in ('nbcmiami.com', 'unknown.example')]
    with grouped_candidates([*rows, *split]) as pool:
        modern = pool.select()['items']
        old = pool.select(mode='balanced_v033')['items']
        assert 'Sunday' in modern[0]['title'] and 'Sunday' not in old[0]['title']
        item = modern[1]
        assert item['publisher_breadth'] == dict(units=1, units_capped=False, folded=True,
                mapped_families=['iHeartMedia'], unmatched_hosts=0,
                unmatched_hosts_capped=False, mapping_version=MAP_VERSION)
        assert item['score'] == rank_day(rows[:1])['items'][0]['score']
        assert item['domain_count'] == len(item['sources']) == 5
        assert {s['url'] for s in item['sources']} == {s.source_url for s in rows}
        assert modern[0]['publisher_breadth']['unmatched_hosts'] == 1
        assert pool.current_cards([item['key'], 'missing', item['key']])[item['key']]['publisher_breadth'] == item['publisher_breadth']
        assert 'publisher_breadth' not in old[0]
        assert pool.select(mode='balanced_v033', prior_keys=[item['key']])['stats']['prior_excluded'] == 1


def test_more_than_raw_cap_preserves_member_units_and_all_lower_bounds():
    hosts = sorted(IHEART_HOSTS)[:83] + ['zz-other-one.example', 'zz-other-two.example']
    extra = 'zz-other-three.example'
    entry = replace(story(extra), domains=tuple(hosts))
    reversed_entry = replace(entry, domains=tuple(reversed(hosts)))
    card = rank_day([entry])['items'][0]
    assert card == rank_day([reversed_entry])['items'][0]
    assert len(card['domains']) == 64 and card['domains_capped']
    assert not any(host.startswith('zz-other') for host in card['domains'])
    assert card['publisher_breadth']['units'] == 4 and card['publisher_breadth']['folded']

    # The SQL sources compact their host stream *before* constructing Candidate.
    # Compare that path to the per-item and multi-observation aggregation.
    accumulator = HostSample()
    for host in hosts:
        accumulator.add(host)
    compact = replace(story(extra), **accumulator.fields())
    assert rank_day([compact])['items'][0] == card
    copies = [story(host, host) for host in [*hosts, extra]]
    assert rank_day(copies)['items'][0]['publisher_breadth'] == card['publisher_breadth']
    assert rank_day(reversed(copies))['items'][0]['publisher_breadth'] == card['publisher_breadth']

    unknowns = tuple(f'a{i:03}.example' for i in range(85))
    mostly_unknown = replace(story(unknowns[0]), domains=(*unknowns, 'wgy.iheart.com', 'wrko.iheart.com'))
    bounded = rank_day([mostly_unknown])['items'][0]['publisher_breadth']
    assert bounded['units'] == bounded['unmatched_hosts'] == 64
    assert bounded['units_capped'] and bounded['unmatched_hosts_capped'] and bounded['folded']
    assert bounded['mapped_families'] == ['iHeartMedia']


def test_new_domain_count_does_not_act_as_second_diversity_signal():
    base = dict(documents=12, domains=12, new_documents=12, new_domains=1,
                day='2025-03-18', history_complete=True)
    entry = replace(story('wgy.iheart.com'), source_kind='gdelt', official_coverage=base)
    many = replace(entry, official_coverage=dict(base, new_domains=12))
    def score(card, mode='balanced'):
        return rank_day([card], mode=mode)['items'][0]['score']
    assert score(entry) == score(many)
    assert score(many, 'balanced_v033') > score(entry, 'balanced_v033')
    assert score(entry) > score(replace(entry, official_coverage=dict(base, history_complete=False)))
    old = replace(many, official_coverage=dict(base, documents=50))
    selected = rank_day([old])['items'][0]
    assert selected['coverage_growth_only'] and selected['official_coverage']['new_domains'] == 1
    assert selected['score'] == score(replace(entry, official_coverage=None))


def test_duplicates_of_one_host_alone_and_subdomains_without_mapped_owner():
    rows = [story('nbcmiami.com'), replace(story('www.nbcmiami.com'), key='second',
            domains=('NBCMIAMI.COM', 'NBCMIAMI.COM.:443'))]
    modern = rank_day(rows)['items'][0]['publisher_breadth']
    assert modern['units'] == 1 and not modern['folded']
    unknown = rank_day([story('a.publisher.example'), story('b.publisher.example')])['items'][0]
    assert unknown['publisher_breadth']['units'] == 2 and unknown['publisher_breadth']['unmatched_hosts'] == 2
    assert not unknown['publisher_breadth']['mapped_families']


def test_unverified_double_www_alias_is_not_promoted_to_a_known_station():
    source = story('www.www.nbcmiami.com')
    small = rank_day([source])['items'][0]
    assert small['domains'] == ['www.nbcmiami.com']
    assert small['publisher_breadth']['mapped_families'] == []
    hosts = HostSample()
    hosts.add('www.www.nbcmiami.com')
    assert rank_day([replace(source, **hosts.fields())])['items'][0] == small
