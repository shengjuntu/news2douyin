from datetime import datetime, timezone
import json

import pytest
from sqlalchemy import event as sql_event
from sqlmodel import Session

from news2douyin.attention.models import AttentionItem, AttentionTopic
from news2douyin.candidates.models import SourceReport
from news2douyin.desk.models import EventPlace
from news2douyin.digest.models import DailyDigestIssue
from news2douyin.hotspots import sources
from news2douyin.intake.official_models import OfficialEvent, OfficialMention
from news2douyin.selection.source_models import SourcePreview
from news2douyin.storage.articles import identity_key
from news2douyin.storage.db import init_db, make_engine
from news2douyin.storage.models import Article, ArticleEventLink, Event
from test_postgres_integration import postgres_url


AS_OF = datetime(2026, 11, 19, 15, 12, 8, tzinfo=timezone.utc)
RECENT = '2026-11-19T12:00:00Z'
PREVIOUS = '2026-11-17T12:00:00Z'


@pytest.fixture(params=['sqlite', 'postgres'])
def engine(request, tmp_path):
    url = (request.getfixturevalue('postgres_url') if request.param == 'postgres'
           else 'sqlite:///' + str(tmp_path / 'hotspots.db'))
    database = make_engine(url)
    init_db(database)
    try:
        yield database
    finally:
        database.dispose()


def _report(index, stamp=RECENT):
    return SourceReport(key=f'report:{index}', provider='worldnewsapi', batch='day', row_index=index,
                        title=f'Earthquake in Tokyo damages homes {index}',
                        url=f'https://news.test/earthquake-{index}', observed_at=stamp,
                        content='full article must stay in storage', language='en', country='us')


def test_materialized_sources_keep_exact_provenance_and_no_writes(engine):
    main = 'https://daily.test/quake'
    sibling = 'https://sibling.test/quake'
    official = 'https://event.test/front-line'
    with Session(engine) as session:
        session.add(DailyDigestIssue(day='2026-11-19', status='ready', items_json=json.dumps([
            {'key': 'digest-1', 'title': 'Strong earthquake strikes Tokyo', 'source_url': main,
             'source_key': 'gkg:gkg_english:20261119120000:1', 'source_kind': 'gkg', 'observed_at': RECENT,
             'geography': {'primary': 'us', 'basis': 'gkg_mentions'},
             'sources': [{'title': 'Strong earthquake strikes Tokyo', 'url': sibling, 'source_kind': 'gkg'},
                         {'title': 'Suspicious URL', 'url': 'javascript:danger()'}]},
        ])))
        session.add(OfficialEvent(event_id='94', event_date='2026-11-19', last_seen=RECENT,
                                   actor1_name='Ukraine', actor2_name='Russia',
                                   actor1_country_iso2='ua', actor2_country_iso2='ru', event_code='190',
                                   event_root_code='19', action_country_iso2='ua', action_place='Kharkiv',
                                   source_url=official, feed='events_english', stamp='20261119120000', row_index=5))
        session.add(SourcePreview(key=identity_key({'url': official}), source_url=official,
                                  title='Ukraine and Russia exchange fire near Kharkiv', fetched_at=RECENT,
                                  excerpt='Officials report a new clash', language='en'))
        session.add(OfficialMention(key='m1', event_id='94', doc_identifier='https://second.test/1',
                                     first_seen=PREVIOUS, last_seen=RECENT, first_feed='mentions_english',
                                     first_stamp='20261117120000', first_row_index=1,
                                     last_feed='mentions_english', last_stamp='20261119120000', last_row_index=1))
        session.add(Event(event_key='organized', event_title='Transport outage', last_seen_at=RECENT))
        session.add(Article(article_key='a1', title='Wide power outage in Spain', fetched_at=RECENT,
                            url='https://power.test/1', country='ie'))
        session.add(ArticleEventLink(event_key='organized', article_key='a1'))
        session.add(EventPlace(key='place', event='organized', role='action', iso='es', article='a1',
                               revision=1, quote='Spain'))
        session.add(_report(1))
        session.add(AttentionTopic(key='attention', title='峰会', last_seen=RECENT))
        session.add(AttentionItem(key='app1', topic='attention', source_id='platform', platform='百度',
                                  title='重要国际峰会今日开幕', url='https://platform.test/trend', last_seen=RECENT))
        session.commit()
    statements = []

    def capture(conn, cursor, statement, params, context, many):
        del conn, cursor, params, context, many
        statements.append(statement.casefold())

    sql_event.listen(engine, 'before_cursor_execute', capture)
    try:
        with Session(engine) as session:
            result = sources.read_window(session, as_of=AS_OF)
    finally:
        sql_event.remove(engine, 'before_cursor_execute', capture)

    assert {key: status['count'] for key, status in result['sources'].items()} == {
        'daily': 2, 'official': 2, 'working': 1, 'reports': 1, 'attention': 1}
    by_url = {row['source_url']: row for row in result['items']}
    assert by_url[main]['country'] == '' and by_url[main]['source_key'].startswith('gkg:')
    assert by_url[main]['cluster_key'] == by_url[sibling]['cluster_key']
    assert by_url[official]['official_only'] is False and by_url[official]['event_key'] == 'gdelt:94'
    assert by_url[official]['actor_countries'] == ['ua', 'ru']
    assert by_url['https://second.test/1']['observed_at'] == PREVIOUS
    assert by_url['https://second.test/1']['official_only'] is True
    assert by_url['https://power.test/1']['country'] == 'es'
    assert by_url['https://power.test/1']['local_event_key'] == 'organized'
    assert by_url['https://news.test/earthquake-1']['source_key'] == 'report:1'
    assert by_url['https://platform.test/trend']['attention_only'] is True
    assert all(sql.lstrip().startswith('select') for sql in statements)
    assert all('articleversion' not in sql and 'sourcereport.content' not in sql for sql in statements)
    assert len(statements) <= 32


def test_reports_use_independent_caps_for_recent_and_comparison(engine, monkeypatch):
    monkeypatch.setattr(sources, 'REPORTS_PER_PERIOD', 2)
    with Session(engine) as session:
        for index in range(10):
            session.add(_report(index, RECENT if index % 2 else PREVIOUS))
        session.add(_report(10, '2026-11-20T00:00:00Z'))
        session.add(_report(11, '2026-11-10T00:00:00Z'))
        session.commit()
        result = sources.read_window(session, as_of=AS_OF)
        with pytest.raises(ValueError, match='1–72'):
            sources.read_window(session, as_of=AS_OF, recent_hours=73)
        with pytest.raises(ValueError, match='时区'):
            sources.read_window(session, as_of=AS_OF.replace(tzinfo=None))

    assert len(result['items']) == 4
    assert {key: sum(row['observed_at'] == key for row in result['items']) for key in (RECENT, PREVIOUS)} == {
        RECENT: 2, PREVIOUS: 2}
    assert result['sources']['reports']['limited'] is True
    assert result['window']['split'] == '2026-11-18T03:12:08Z'
