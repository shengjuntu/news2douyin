from datetime import date, datetime, timezone
from urllib.parse import parse_qs, urlsplit

from news2douyin.digest.video_story import attach_history, focus_url


def _card(key, title, day, *, url=None, kind='collected', source=None, **extra):
    return dict(key=key, title=title, source_kind=kind, source_key=source or 'report:' + key,
                source_url=url or 'https://press.example/report/' + key,
                observed_at=day + 'T03:00:00Z', **extra)


def _edition(day, *cards, cutoff=None):
    return dict(day=day, items=list(cards), cutoff_at=cutoff or day + 'T12:00:00Z')


def test_gdelt_id_provides_links_without_promoting_similar_actor_or_scope():
    yesterday, today = '2026-11-18', '2026-11-19'
    anchor = dict(kind='gdelt', source='gdelt:129')
    first = _card('early', 'Rail line shut after equipment failure', yesterday, **anchor)
    repeated = _card('reprint', 'Rail line reopened after staff repaired signals', yesterday, **anchor)
    current = _card('later', 'Rail line reopened after staff repaired signals', today,
                    actors=['Mayor Chen'], watches=['Transport'], **anchor)
    other = _card('other', 'Mayor Chen announces new fares', yesterday,
                  actors=['Mayor Chen'], watches=['Transport'], kind='gdelt', source='gdelt:130')
    attach_history([current], [_edition(yesterday, other, repeated, first)], day=today)
    assert current['story_history'] == [dict(day=yesterday, key='early', title=first['title'],
                                             url=first['source_url'], reason='同一 GDELT 编码事件')]


def test_updates_to_same_page_and_saved_link_have_distinct_meanings():
    yesterday, today = '2026-11-18', '2026-11-19'
    previous = _card('early', '公交停运，居民借直通车返家', yesterday,
                     url='https://press.example/news/live-rail?utm_source=home')
    current = _card('later', '公交恢复通车，首班车载居民返家', today,
                    url='https://press.example/news/live-rail?utm_medium=search')
    home_yesterday = _card('home-prev', 'Extra buses for evening commuters', yesterday,
                           url='https://press.example/world/news')
    home_today = _card('home-new', 'New charges for night bus passengers', today,
                       url='https://press.example/world/news')
    attach_history([current, home_today], [_edition(yesterday, previous, home_yesterday)], day=date.fromisoformat(today))
    assert current['story_history'][0]['reason'] == '同一来源网址的较早标题'
    assert current['story_history'][0]['url'] == previous['source_url']
    assert 'story_history' not in home_today
    href = urlsplit(focus_url(current, day=today))
    assert href.path == '/focus' and parse_qs(href.query) == {'new': ['1'], 'video_day': [today], 'video_key': ['later']}


def test_complete_quoted_court_reference_can_link_new_action_but_not_partial_ref():
    yesterday, today = '2026-11-18', '2026-11-19'
    prior = _card('filed', '法院受理（2026）沪0101民初123号楼道漏水纠纷', yesterday)
    partial = _card('short', '法院受理民初123号楼道漏水纠纷', yesterday)
    other_place = _card('other-court', '法院受理（2026）粤0101民初123号楼道漏水纠纷', yesterday)
    current = _card('trial', '楼道漏水纠纷（2026）沪0101民初123号今日开庭', today)
    attach_history([current], [_edition(yesterday, partial, other_place, prior)], day=today)
    assert [(row['key'], row['reason']) for row in current['story_history']] == [('filed', '报道引用了相同完整案号')]


def test_history_caps_orders_expires_and_does_not_link_later_observed_material():
    anchor = dict(kind='local', source='local:evt_anchor')
    today = '2026-11-19'
    current = _card('today', 'City opens temporary rail service', today, **anchor)
    a = _card('a', 'City approves rail replacement', '2026-11-17', **anchor)
    b = _card('b', 'Engineer identifies fault in the tunnel', '2026-11-16', **anchor)
    c = _card('c', 'Council inspects flooded rail tunnel', '2026-11-15', **anchor)
    fourth = _card('fourth', 'Commuters report tunnel flooding', '2026-11-14', **anchor)
    too_old = _card('old', 'Earlier inspection of the tunnel', '2026-11-01', **anchor)
    future = _card('future', 'City suspends temporary rail service', '2026-11-20', **anchor)
    repaired = _card('repaired', 'Bridge records show delay', '2026-11-19', **anchor)
    future_cutoff = _card('future-cutoff', 'City buys temporary rolling stock', '2026-11-18', **anchor)
    histories = [
        _edition('2026-11-14', fourth), _edition('2026-11-01', too_old),
        _edition('2026-11-17', a), _edition('2026-11-20', future),
        _edition('2026-11-18', repaired, cutoff='2026-11-19T03:30:00Z'),
        _edition('2026-11-18', future_cutoff, cutoff='2026-11-19T09:00:00Z'),
        _edition('2026-11-16', b), _edition('2026-11-15', c),
    ]
    attach_history([current], histories, day=today, cutoff=datetime(2026, 11, 19, 4, tzinfo=timezone.utc))
    assert [(row['key'], row['reason']) for row in current['story_history']] == [
        ('a', '同一已整理事件'), ('b', '同一已整理事件'), ('c', '同一已整理事件'),
    ]
