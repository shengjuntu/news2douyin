import json

import pytest
from sqlalchemy import event as sql_event
from sqlmodel import Session

from news2douyin.desk import focus_read
from news2douyin.desk.focus_models import FocusCandidate, FocusDefinition, FocusTrash
from news2douyin.desk.models import EventPlace, GkgSource, RadarEntry, Watch, WatchEvent
from news2douyin.storage.db import init_db, make_engine
from news2douyin.storage.models import Article, ArticleEventLink, ArticleVersion, Event, EventMoment, EventState
from test_postgres_integration import postgres_url


def _fixture(session):
    session.add(Watch(key='watched', name='行动跟踪', kind='人物', scope='行动', version=2))
    session.add(Watch(key='other', name='A second focus', kind='地区', scope='地区'))
    session.add(Watch(key='trash', name='A removed focus', kind='人物', scope='删除'))
    session.add(FocusTrash(watch='trash', name='A removed focus', deleted_at='2026-11-20T12:00:00Z'))
    session.add(FocusDefinition(watch='watched', description='已经确认的范围', spec_json='{}', scope_version=2))
    for key, stamp, topic, choice, scope, decision in [
        ('a', '2026-11-19T16:30:00Z', '经济', 'auto', 2, 'core'),
        ('b', '2026-11-20T01:30:00+09:00', '外交', 'include', 1, 'excluded'),
        ('c', '2026-11-20T00:10:00+09:00', '经济', 'auto', 2, 'background'),
        ('old', '2026-11-19T17:00:00Z', '经济', 'auto', 1, 'core'),
        ('pending', '2026-11-19T17:00:00Z', '经济', 'auto', 2, 'pending'),
        ('excluded', '2026-11-19T17:00:00Z', '经济', 'exclude', 2, 'core'),
        ('redirect', '2026-11-19T17:00:00Z', '经济', 'include', 2, 'core'),
        ('other_only', '2026-11-19T17:00:00Z', '经济', 'include', 2, 'core'),
    ]:
        session.add(Event(event_key=key, event_title='事件 ' + key, summary='已经入库的摘要', topic=topic))
        session.add(RadarEntry(key=key, title='事件 ' + key, seen=stamp, priority=1, band='', reason='',
                               evidence='', headings='', material='do not load', sources='[]', geography='[]'))
        session.add(WatchEvent(watch='other' if key == 'other_only' else 'watched', event=key, choice=choice,
                               branch='决定' if key == 'a' else '', reason='已保存'))
        session.add(FocusCandidate(key='fc_' + key, watch='watched', event=key, scope_version=scope,
                                   event_fingerprint='hash', status='done', decision=decision))
    session.add(EventState(event_key='redirect', merged_into='a'))
    for key, provider, country in [('article1', 'newsnow', 'us'), ('article2', 'gkg', 'jp'), ('foreign', 'gkg', 'ca')]:
        session.add(Article(article_key=key, title='标题 ' + key, url=f'https://example.test/{key}',
                            content='可读文字', provider=provider, country=country))
        session.add(ArticleEventLink(article_key=key, event_key='other_only' if key == 'foreign' else 'a'))
    session.add(ArticleEventLink(article_key='article1', event_key='a'))  # Historical duplicate links.
    session.add(GkgSource(article_key='article1', record_id='g1', attributes='{"mention_iso":["fr","fr","jp"]}'))
    session.add(GkgSource(article_key='article2', record_id='g2', attributes='{"mention_iso":"fr"}'))
    session.add(GkgSource(article_key='foreign', record_id='g3', attributes='{"mention_iso":["ca"]}'))
    for revision, body in ((1, '原始保存文字'), (2, '后来保存文字')):
        session.add(ArticleVersion(article_key='article1', revision=revision, content_hash='hash' + str(revision),
                                   payload_json=json.dumps(dict(content=body, title=f'存档标题{revision}',
                                       provider='newsnow', url='https://example.test/doc'))))
    evidence = [dict(article_key='article1', revision=1, excerpt='原始保存文字', url='https://example.test/doc')]
    session.add(EventMoment(moment_key='m1', event_key='a', title='签署', time_kind='occurred',
                            date_start='2026-11-17', reviewed=True, sources_json=json.dumps(evidence)))
    session.add(EventMoment(moment_key='m2', event_key='a', title='执行', time_kind='occurred',
                            date_start='2026-11-18', reviewed=True))
    session.add(EventMoment(moment_key='m3', event_key='a', title='未核对', time_kind='occurred',
                            date_start='2026-11-16', reviewed=False))
    session.add(EventMoment(moment_key='m4', event_key='a', title='删除', time_kind='occurred',
                            date_start='2026-11-15', reviewed=True, deleted=True))
    session.add_all([EventPlace(key='action-' + str(revision), event='a', role='action', iso='jp',
                                article='article1', revision=revision, quote='保存文字') for revision in (1, 2)])
    session.add(EventPlace(key='impact', event='a', role='impact', iso='jp', article='article1', revision=1, quote='原始保存文字'))
    session.commit()


@pytest.fixture(params=['sqlite', 'postgresql'])
def db(engine, request):
    if request.param == 'sqlite':
        yield engine
        return
    instance = make_engine(request.getfixturevalue('postgres_url'))
    try:
        init_db(instance)
        yield instance
    finally:
        instance.dispose()


def test_membership_dates_facets_and_cursor(db):
    with Session(db) as session:
        _fixture(session)
        first_focus = focus_read.list_focuses(session, limit=1)
        assert first_focus['items'][0]['key'] == 'other'
        assert focus_read.list_focuses(session, cursor=first_focus['next_cursor'])['items'][0]['key'] == 'watched'
        assert [row['key'] for row in focus_read.list_focuses(session, query='行动')['items']] == ['watched']
        with pytest.raises(KeyError):
            focus_read.focus_members(session, 'trash')

        sql = []
        def monitor(conn, cursor, statement, parameters, context, executemany):
            sql.append(statement)
        sql_event.listen(db, 'before_cursor_execute', monitor)
        try:
            first = focus_read.focus_members(session, 'watched', limit=1)
        finally:
            sql_event.remove(db, 'before_cursor_execute', monitor)
        assert len(sql) <= 3
        assert all('radarentry.material' not in stmt and 'articleversion' not in stmt for stmt in sql)
        assert [(item['key'], item['day']) for item in first['items']] == [('a', '2026-11-20')]
        rest = focus_read.focus_members(session, 'watched', limit=2, cursor=first['next_cursor'])
        assert [(item['key'], item['day']) for item in rest['items']] == [('b', '2026-11-20'), ('c', '2026-11-19')]
        assert not rest['next_cursor']
        with pytest.raises(ValueError, match='游标'):
            focus_read.focus_members(session, 'watched', category='经济', cursor=first['next_cursor'])
        with pytest.raises(ValueError, match='日期'):
            focus_read.focus_members(session, 'watched', date_from='2026-02-30')

        facets = focus_read.focus_overview(session, 'watched', group_by='day', limit=1)
        assert facets['total'] == 3 and facets['groups_truncated']
        assert facets['groups'] == [dict(key='2026-11-20', label='2026-11-20', count=2)]
        occurred = focus_read.focus_members(session, 'watched', date_from='2026-11-17',
                                            date_to='2026-11-17', date_basis='occurred', country='JP', branch='决定')
        assert [(item['key'], item['day']) for item in occurred['items']] == [('a', '2026-11-17')]
        assert focus_read.focus_overview(session, 'watched', category='经济', group_by='category')['total'] == 2
        for role, code in (('action', 'jp'), ('impact', 'jp'), ('any', 'jp'), ('mention', 'fr'), ('media', 'us')):
            result = focus_read.focus_overview(session, 'watched', country=code, country_role=role, group_by='country')
            assert result['total'] == 1 and result['groups'][0]['count'] == 1
        assert not focus_read.focus_members(session, 'watched', country='us')['items']
        mentions = focus_read.focus_overview(session, 'watched', country_role='mention', group_by='country')
        assert [group['key'] for group in mentions['groups']] == ['fr', 'jp']


def test_event_source_evidence_is_scoped_versioned_and_bounded(db):
    with Session(db) as session:
        _fixture(session)
        detail = focus_read.focus_event(session, 'watched', 'a', source_limit=1)
        assert detail['event']['stored_summary'] == '已经入库的摘要'
        assert detail['sources'][0]['key'] == 'article1' and detail['sources'][0]['revision'] == 2
        assert detail['sources'][0]['gkg_record_id'] == 'g1' and detail['sources'][0]['media_country'] == 'us'
        assert detail['sources'][0]['path'].endswith('?revision=2')
        continuation = focus_read.focus_event(session, 'watched', 'a', source_cursor=detail['next_source_cursor'])
        assert [source['key'] for source in continuation['sources']] == ['article2']
        assert [moment['key'] for moment in detail['moments']] == ['m2', 'm1', 'm3']
        assert detail['moments'][1]['sources'][0]['path'].endswith('?revision=1')
        assert {place['role'] for place in detail['countries']} == {'action', 'impact'}

        frozen = focus_read.focus_source(session, 'watched', 'a', 'article1', revision=1, limit=2)
        assert frozen['title'] == '存档标题1' and frozen['revision'] == 1
        assert frozen['text'] == '原始' and frozen['next_offset'] == 2 and frozen['lead_only']
        rest = focus_read.focus_source(session, 'watched', 'a', 'article1', revision=1, offset=2)
        assert rest['text'] == '保存文字' and rest['next_offset'] is None
        legacy = focus_read.focus_source(session, 'watched', 'a', 'article2')
        assert legacy['revision'] is None and legacy['text'] == '可读文字'
        with pytest.raises(KeyError):
            focus_read.focus_source(session, 'watched', 'a', 'foreign')
        with pytest.raises(KeyError):
            focus_read.focus_source(session, 'watched', 'a', 'article1', revision=99)
        with pytest.raises(KeyError):
            focus_read.focus_event(session, 'watched', 'other_only')
        with pytest.raises(KeyError):
            focus_read.focus_event(session, 'watched', 'excluded')
