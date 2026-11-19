from datetime import date, timedelta

from sqlmodel import Session

from news2douyin.digest import service
from news2douyin.digest.models import DailyDigestIssue
from news2douyin.digest.ranking import Candidate, card_progress_key, grouped_candidates, rank_day


def report(n, title, **fields):
    return Candidate('gkg:' + str(n), title, '2025-03-16T00:01:00Z',
                     f'https://publisher{n}.example/article', fields.pop('source_kind', 'gkg'), **fields)


def test_syndicated_commitment_uses_one_slot_and_preserves_original_source_material():
    canonical = 'EU Regulation 2024/1689 enters into force on 2 August'
    first = report(1, '(Reuters) EU Regulation 2024/1689 takes effect on 2 August')
    second = report(2, canonical + ' | BBC News')
    copies = [first, second, report(3, canonical)]
    others = ['EU Regulation 2024/1689 is proposed on 2 August',
              'EU Regulation 2024/1689 enters into force on 3 August',
              'EU Regulation 2024/1690 enters into force on 2 August',
              'EU Regulation 2024/1689 could enter into force on 2 August',
              'EU Regulation 2024/1689 enters into force on 2 August?',
              'EU Regulation 2024/1689 enters into force on 2 August except for imports',
              'Council says EU Regulation 2024/1689 enters into force on 2 August']
    records = copies + [report(i + 10, t) for i, t in enumerate(others)]
    edition = rank_day(records)
    assert edition == rank_day(reversed(records))
    assert len(edition['items']) == 1 + len(others)
    group = next(c for c in edition['items'] if c['observations'] == 3)
    assert group['headline_variants'] == 3 and group['domain_count'] == 3
    assert {(s['title'], s['url']) for s in group['sources']} == {(c.title, c.source_url) for c in copies}
    for mode in ('balanced_v032', 'balanced_v031', 'breadth', 'editorial'):
        previous = rank_day(records, mode=mode)
        assert previous['stats']['groups'] == len(records)
        assert all('headline_variants' not in card for card in previous['items'])


def test_keep_jurisdiction_direction_release_versions_and_generic_actions_distinct():
    titles = ['(Reuters) Mayor resigns', '(AP News) Mayor resigns', 'Mayor resigns',
              'Update 1 - US applies 2% tariff on Canada', 'Update 2 - US applies 2% tariff on Canada',
              'Canada applies 2% tariff on US', 'US applies 2% tariff on Canada',
              'A new accord takes effect', 'A new accord enters into force',
              'Council meeting | Reuters reports council will adjourn', 'Council meeting']
    assert rank_day([report(i, title) for i, title in enumerate(titles)])['stats']['groups'] == len(titles)


def test_old_and_current_snapshot_aliases_work_without_rewriting_old_editions(engine):
    old = report(1, 'EU law 101 takes effect')
    contemporary = report(2, 'EU law 101 enters into force')
    changed = report(3, 'EU law 102 enters into force')
    saved = rank_day([old], mode='balanced_v032')['items'][0]
    old_key = saved['key']
    assert old_key != card_progress_key(saved)
    with grouped_candidates([old, contemporary, changed], keep_baseline=False) as day:
        assert day.current_key_map([old_key, 'unseen']) == {old_key: card_progress_key(saved), 'unseen': 'unseen'}
        chosen = day.select(prior_keys=[old_key])['items']
        assert [c['title'] for c in chosen] == [changed.title]
    stamp = date(2025, 3, 16)
    import json
    with Session(engine) as session:
        row = DailyDigestIssue(day=stamp.isoformat(), status='ready',
                               keys_json=json.dumps([old_key]), items_json=json.dumps([saved]))
        session.add(row)
        session.commit()
        history = service.prior_keys(session, stamp + timedelta(days=1))
        assert history == {old_key, card_progress_key(saved)}
        assert rank_day([contemporary, changed], prior_keys=history)['items'][0]['title'] == changed.title
        assert session.get(DailyDigestIssue, stamp.isoformat()).items_json == json.dumps([saved])


def test_coverage_growth_on_previously_reported_code_has_no_action_bonus():
    counts = dict(event_id='13', day='2025-03-16', documents=20, new_documents=8, domains=12,
                  new_domains=4, history_complete=True)
    lead = report(4, 'Several outlets discuss the council decision', source_kind='gdelt', official_coverage=counts)
    current = rank_day([lead])['items'][0]
    old = rank_day([lead], mode='balanced_v032')['items'][0]
    assert current['coverage_growth_only'] and 'action_evidence' not in current
    assert 'coverage_growth_only' not in old
    assert old['score'] > current['score']
    for different_counts in [dict(counts, history_complete=False), dict(counts, documents=8), dict(counts, new_documents=0)]:
        ambiguous = report(4, lead.title, source_kind='gdelt', official_coverage=different_counts)
        assert 'coverage_growth_only' not in rank_day([ambiguous])['items'][0]
    new_action = report(4, lead.title, source_kind='gdelt', official_coverage=counts,
                        action_evidence=dict(kind='dated_action_report', basis='可在源材料核对'))
    assert 'coverage_growth_only' not in rank_day([new_action])['items'][0]


def test_station_masthead_requires_exact_article_slug_and_matching_affiliate():
    from dataclasses import replace

    title = 'Transit agency reopens the river bridge'
    raw = report(1, title)
    slug = 'transit-agency-reopens-the-river-bridge'
    radio = replace(report(2, title + ' | News Radio 103.1 and 810 WGY'),
                    source_url='https://wgy.iheart.com/content/2025-03-15-' + slug + '/')
    nbc = replace(report(3, title + ' – NBC 6 South Florida'),
                  source_url='https://www.nbcmiami.com/news/economy/' + slug + '/123/')
    mirror = replace(radio, source_url='https://thirdparty.example/983', key='gkg:other')
    # A matching original headline on a mirror inherits the verified station
    # variant; the position of the proving observation in the stream is immaterial.
    rows = [raw, radio, nbc, mirror]
    edition = rank_day(rows)
    assert edition == rank_day(reversed(rows))
    group, = edition['items']
    assert group['headline_variants'] == 3 and len(group['sources']) == 4
    assert rank_day(rows, mode='balanced_v032')['stats']['groups'] == 3
    for changed in [
        replace(radio, source_url='https://thirdparty.example/content/2025-03-15-' + slug + '/'),
        replace(radio, source_url='https://wgy.iheart.com/content/2025-03-15-different-bridge/'),
        replace(radio, title=title + ' | more closures reported by WGY'),
        replace(nbc, title=title + ' – NBC Connecticut'),
        replace(nbc, title=title + ' – NBCTV launches a new inquiry'),
        replace(nbc, title=title + ' except for trucks – NBC 6 South Florida'),
        replace(nbc, source_url='https://www.nbcmiami.com.evil.example/news/' + slug + '/123/'),
    ]:
        assert rank_day([raw, changed])['stats']['groups'] == 2, changed.title


def test_breadth_and_editorial_still_work_without_v031_replay_state():
    rows = [report(1, 'Treaty 101 enters into force'), report(2, 'Treaty 101 takes effect')]
    with grouped_candidates(rows, keep_baseline=False) as pool:
        for mode in ('breadth', 'editorial'):
            assert pool.select(mode=mode) == rank_day(rows, mode=mode)
            assert pool.select(mode=mode)['stats']['groups'] == 2
