from datetime import datetime, timezone
import json

import pytest
from sqlmodel import Session

from news2douyin.desk.models import EventPlace
from news2douyin.intake.official_models import OfficialEvent, OfficialMention
from news2douyin.selection.source_models import SourcePreview
from news2douyin.storage.articles import identity_key
from news2douyin.storage.db import init_db, make_engine
from news2douyin.storage.models import Article, ArticleEventLink, ArticleVersion, Event, EventMoment, EventState
from news2douyin.world_radar.events import developments
from test_postgres_integration import postgres_url  # Imported pytest fixture.


START = datetime(2026, 11, 25, tzinfo=timezone.utc)
END = datetime(2026, 11, 26, tzinfo=timezone.utc)
OBSERVED = '2026-11-25T03:00:00Z'


@pytest.fixture(params=['sqlite', 'postgres'])
def radar_db(request, tmp_path):
    uri = request.getfixturevalue('postgres_url') if request.param == 'postgres' else 'sqlite:///' + str(tmp_path / 'radar.db')
    engine = make_engine(uri)
    init_db(engine)
    try:
        with Session(engine) as session:
            yield session
    finally:
        engine.dispose()


def preview(session, url, title, when=OBSERVED):
    session.add(SourcePreview(key=identity_key({'url': url}), source_url=url, title=title,
                              excerpt='The saved context for this report.', fetched_at=when))


def official(session, ident, url, *, country='cl', place='Santiago', lat=-33.4, lon=-70.6,
             stamp=OBSERVED, event_date='2026-11-25', root='17'):
    session.add(OfficialEvent(event_id=ident, event_date=event_date, last_seen=stamp,
                 actor1_name='Actor A', actor2_name='Actor B', actor2_country_iso2='ca',
                 event_code=root + '0', event_root_code=root,
                 action_country_iso2=country, action_place=place, latitude=lat, longitude=lon,
                 source_url=url, feed='events_english', stamp='20261125030000', row_index=int(ident)))


def test_reviewed_progresses_stay_separate_and_link_the_pinned_version(radar_db):
    address = 'https://news.test/sea-facility'
    radar_db.add_all([
        Event(event_key='local', event_title='港口中断和恢复', last_seen_at=OBSERVED),
        ArticleEventLink(event_key='local', article_key='wire'),
        Article(article_key='wire', title='A port incident', url=address, country='us', fetched_at=OBSERVED),
        ArticleVersion(article_key='wire', revision=1, content_hash='initial',
                       payload_json=json.dumps(dict(url=address, title='港口关闭和恢复', content='港口关闭并于当日恢复。')),
                       observed_at=OBSERVED),
        EventPlace(key='act', event='local', role='action', iso='jp', article='wire', revision=1, quote='港口关闭'),
        EventPlace(key='imp', event='local', role='impact', iso='us', article='wire', revision=1, quote='港口关闭'),
    ])
    for key, title in [('closure', '港口关闭'), ('plan', '当局宣布港口将恢复'), ('resume', '港口恢复')]:
        evidence = json.dumps([dict(article_key='wire', revision=1, excerpt='港口关闭并于当日恢复。',
                                    url=address, title='港口关闭和恢复', content_hash='initial')])
        radar_db.add(EventMoment(moment_key=key, event_key='local', title=title, time_kind='occurred',
                                 date_start='2026-11-25', date_end='2026-11-25', certainty='exact',
                                 reviewed=True, sources_json=evidence))
    radar_db.commit()
    cards = developments(radar_db, start=START, end=END, country='jp')['items']
    assert {(r['moment_key'], r['stage']) for r in cards} == {
        ('closure', '暂停或中断'), ('plan', '宣布或拟议'), ('resume', '恢复或解除')}
    assert all(r['category'] == 'transport' and r['precision'] == 'country' and r['latitude'] is None for r in cards)
    assert all(r['source_path'] == '/articles/wire?revision=1' and r['event_path'] == '/events/local' for r in cards)
    assert developments(radar_db, start=START, end=END, country='us')['items'] == []


def test_coded_rows_use_usable_originals_and_actual_action_country(radar_db):
    first = 'https://policy.test/export-duty'
    second = 'https://ports.test/closure'
    official(radar_db, '1001', first, place='Chile', lat=-30, lon=-71)
    official(radar_db, '1002', second, country='tr', place='Istanbul', lat=41, lon=29)
    official(radar_db, '1003', 'https://bad.test/not-found')
    official(radar_db, '1004', first + '?utm_source=copy', place='Chile', lat=-30, lon=-71)
    official(radar_db, '1005', 'https://late.test/', stamp='2026-11-26T00:00:00Z')
    official(radar_db, '1006', 'https://news.test/past-action', event_date='2026-11-23')
    official(radar_db, '1007', 'https://unseen.test/needs-metadata', country='', place='No action place')
    mention_url = 'https://wire.test/topic'
    radar_db.add(OfficialMention(key='m1', event_id='1007', doc_identifier=mention_url, last_seen=OBSERVED,
                     first_feed='mentions_english', first_stamp='20261125030000', first_row_index=1,
                     last_feed='mentions_english', last_stamp='20261125030000', last_row_index=1))
    preview(radar_db, first, 'Chile announces an export tax on copper concentrate')
    preview(radar_db, second, 'Port closed after an earthquake')
    preview(radar_db, 'https://bad.test/not-found', '404 not found')
    preview(radar_db, 'https://late.test/', 'Forecast article must be excluded')
    preview(radar_db, 'https://news.test/past-action', 'Historical port closure')
    preview(radar_db, mention_url, 'Another copper refinery suspends operations')
    radar_db.commit()
    cards = {r['title']: r for r in developments(radar_db, start=START, end=END)['items']}
    assert len(cards) == 3
    duty = cards['Chile announces an export tax on copper concentrate']
    assert duty['category'] == 'policy' and duty['precision'] == 'country' and duty['latitude'] is None
    assert duty['candidate_key'].startswith('gdelt:') and 'candidate=gdelt%3A' in duty['event_path']
    assert duty['status'] == 'coded'
    port = cards['Port closed after an earthquake']
    assert (port['precision'], port['country'], port['latitude'], port['longitude']) == ('location', 'tr', 41, 29)
    assert cards['Another copper refinery suspends operations']['precision'] == 'unknown'
    assert developments(radar_db, start=START, end=END, country='ca')['items'] == []
    assert developments(radar_db, start=START, end=END, category='policy')['items'] == [duty]
    assert developments(radar_db, start=START, end=END, category='transport')['items'] == [port]


def test_current_source_version_is_pinned_and_merged_event_is_excluded(radar_db):
    address = 'https://news.test/extraction'
    article = Article(article_key='latest', title='The newest version already in the database',
                      url=address, fetched_at='2026-11-27T02:00:00Z', country='fr')
    radar_db.add_all([article,
        Event(event_key='left', event_title='旧题', last_seen_at=OBSERVED),
        Event(event_key='merged', event_title='已并入', last_seen_at=OBSERVED),
        EventState(event_key='left', title_override='新题'),
        EventState(event_key='merged', merged_into='left'),
        ArticleEventLink(event_key='left', article_key='latest'),
        ArticleEventLink(event_key='merged', article_key='latest'),
        ArticleVersion(article_key='latest', revision=1, content_hash='first',
                       payload_json=json.dumps(dict(url=address, title='矿山暂停生产', content='生产暂停，暂无复产时间。')),
                       observed_at=OBSERVED),
        ArticleVersion(article_key='latest', revision=2, content_hash='newer',
                       payload_json=json.dumps(dict(url=address, title=article.title, content='Updated contents.')),
                       observed_at=article.fetched_at),
    ])
    radar_db.commit()
    cards = developments(radar_db, start=START, end=END)['items']
    assert len(cards) == 1 and cards[0]['title'] == '新题'
    assert cards[0]['source_title'] == '矿山暂停生产'
    assert cards[0]['source_path'] == '/articles/latest?revision=1'
    assert cards[0]['country'] == ''


def test_story_timeline_keeps_actions_and_counts_shared_source_once():
    from news2douyin.world_radar.stories import group_stories

    def action(key, title, date, event=''):
        return dict(key=key, title=title, occurred_on=date, observed_at=date, event_key=event,
                    sources=[dict(url='https://wire.test/ports')])

    rows = [action('a', '关闭港口', '2026-11-23', 'port'),
            action('b', '恢复通行', '2026-11-25', 'port'),
            action('c', '另一个港口恢复通行', '2026-11-25'),
            action('d', 'Proposal on Regulation (EU) 2024/1234', '2026-11-23'),
            action('e', 'Regulation (EU) 2024/1234 takes effect', '2026-11-25')]
    stories = group_stories(rows)
    assert [s['keys'] for s in stories] == [['b', 'a'], ['c'], ['e', 'd']]
    assert stories[0]['count'] == 2 and stories[0]['reports'] == 1
