"""Attention links are review candidates, with real SQL backend contracts."""
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import event as sql_event, func
from sqlalchemy.exc import OperationalError
from sqlmodel import Session, select

from news2douyin.attention import matching
from news2douyin.attention.models import AttentionTopic, AttentionItem, AttentionEventMatch, AttentionWatchMatch
from news2douyin.desk.focus_models import FocusTrash
from news2douyin.desk.models import Watch, WatchEvent, GraphPending
from news2douyin.intake.official_models import OfficialEvent, OfficialMention
from news2douyin.storage.db import init_db, make_engine
from test_postgres_integration import postgres_url


NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)


def iso(when=NOW):
    return when.isoformat(timespec='seconds').replace('+00:00', 'Z')


@pytest.fixture(params=['sqlite', 'postgres'])
def attention_engine(request, tmp_path):
    url = request.getfixturevalue('postgres_url') if request.param == 'postgres' else 'sqlite:///' + str(tmp_path / 'attention.db')
    engine = make_engine(url)
    init_db(engine)
    yield engine
    engine.dispose()


def topic(session, title='特朗普向乌克兰提供援助', key='attention_a', url=''):
    session.add(AttentionTopic(key=key, title=title, generation='g1', last_seen=iso(),
                               summary_json='{"observed_platforms":2}'))
    session.add(AttentionItem(key='item_' + key, topic=key, title=title, source_id='weibo',
                              url=url, canonical_url=matching._safe_url(url), last_seen=iso()))


def event(session, key='101', **changes):
    values = dict(event_id=key, event_date='2026-11-14', last_seen=iso(),
                  actor1_name='DONALD TRUMP', actor2_name='UKRAINE',
                  actor1_country_iso2='us', actor2_country_iso2='ua', action_country_iso2='ua',
                  action_place='Kyiv, Ukraine', event_root_code='07', source_url='https://wire.example/' + key,
                  feed='events_english', stamp='20261114120000', row_index=int(key))
    values.update(changes)
    session.add(OfficialEvent(**values))


def prepare(engine, key='attention_a'):
    with Session(engine) as session:
        return matching.prepare_match(session, key, now=NOW)


def rows(engine):
    with Session(engine) as session:
        return session.exec(select(AttentionEventMatch).order_by(AttentionEventMatch.event_id)).all()


def test_specific_actor_action_country_candidates_and_no_membership(attention_engine):
    with Session(attention_engine) as session:
        topic(session)
        event(session)
        event(session, '102', event_root_code='19')
        event(session, '103', actor2_name='FRANCE', actor2_country_iso2='fr', action_country_iso2='fr', action_place='Paris')
        event(session, '104', actor1_name='DONALD TRUMPER')
        session.commit()
    prepared = prepare(attention_engine)
    assert [row['event_id'] for row in prepared['matches']] == ['101']
    assert prepared['matches'][0]['method'] == 'structured_candidate'
    assert 'actor1_name' in '\n'.join(prepared['matches'][0]['reasons'])
    assert prepared['scope']['score_type'] == 'rule_score_not_probability'
    result = matching.refresh_topic(attention_engine, 'attention_a', now=NOW)
    assert result['changed'] and result['events'] == 1
    with Session(attention_engine) as session:
        match = session.exec(select(AttentionEventMatch)).one()
        assert match.status == 'candidate' and match.generation == 'g1'
        current = session.get(AttentionTopic, 'attention_a')
        summary = json.loads(current.summary_json)
        assert summary['observed_platforms'] == 2
        assert summary['matching']['scope']['event_limit'] == 200
        assert current.match_next_at == NOW.timestamp() + 300
        assert session.get(GraphPending, 'attention/attention_a')
        assert session.exec(select(func.count()).select_from(WatchEvent)).one() == 0


@pytest.mark.parametrize('title', ['特朗普最新消息', '特朗普援助', '特朗普在乌克兰的消息'])
def test_person_or_cooccurrence_does_not_define_issue(attention_engine, title):
    with Session(attention_engine) as session:
        topic(session, title)
        event(session)
        session.commit()
    result = prepare(attention_engine)
    assert not result['matches']
    assert any('不代表' in reason for reason in result['unmatched_reasons'])


@pytest.mark.parametrize('title', ['特朗普否认向乌克兰提供援助', '传闻特朗普可能会援助乌克兰',
                                  '特朗普向乌克兰援助100亿美元', '特朗普向乌克兰援助200亿美元'])
def test_unverifiable_negation_and_different_amounts_do_not_get_semantic_identity(attention_engine, title):
    with Session(attention_engine) as session:
        topic(session, title)
        event(session)
        session.commit()
    result = prepare(attention_engine)
    assert not result['matches']
    assert any('跳过纯语义' in reason for reason in result['unmatched_reasons'])


def test_same_source_document_is_candidate_even_with_conflicting_time_or_negation(attention_engine):
    with Session(attention_engine) as session:
        topic(session, '特朗普否认2026年11月14日向乌克兰援助100亿美元', url='https://wire.example/article?utm_source=weibo')
        event(session, source_url='https://wire.example/article', event_date='2020-01-01')
        session.commit()
    result = prepare(attention_engine)
    match = result['matches'][0]
    assert match['method'] == 'source_document_url'
    text = '\n'.join(match['reasons'])
    assert '不证明同一事实' in text and '相差超过2天' in text and '否定' in text and '数值' in text


def test_mentions_are_bounded_to_candidate_ids_and_dates_not_global_document_scan(attention_engine):
    with Session(attention_engine) as session:
        topic(session, url='https://story.example/article?utm_source=x')
        event(session)
        event(session, '102', actor1_name='UNRELATED', actor1_country_iso2='fr', actor2_name='',
              actor2_country_iso2='fr', action_country_iso2='fr', event_root_code='01')
        for key, event_id in [('m1', '101'), ('m2', '102')]:
            session.add(OfficialMention(key=key, event_id=event_id, doc_identifier='https://story.example/article',
                last_seen=iso(), first_feed='mentions_english', first_stamp='20261114120000', first_row_index=1,
                last_feed='mentions_english', last_stamp='20261114120000', last_row_index=1))
        session.commit()
    statements = []
    def record(_conn, _cursor, statement, *_):
        statements.append(statement.lower())
    sql_event.listen(attention_engine, 'before_cursor_execute', record)
    try:
        result = prepare(attention_engine)
    finally:
        sql_event.remove(attention_engine, 'before_cursor_execute', record)
    assert [row['event_id'] for row in result['matches']] == ['101']
    assert result['matches'][0]['method'] == 'mention_document_url'
    assert all(statement.lstrip().startswith('select') for statement in statements)
    assert not any('articleversion' in statement for statement in statements)
    mention_sql = next(statement for statement in statements if 'from officialmention' in statement)
    assert 'officialmention.event_id in' in mention_sql and 'limit' in mention_sql
    assert not 'where officialmention.doc_identifier' in mention_sql
    assert len(statements) <= 5


def test_event_occurrence_date_and_observation_date_are_distinct(attention_engine):
    with Session(attention_engine) as session:
        topic(session, '2026年11月14日特朗普向乌克兰提供援助')
        event(session)
        event(session, '102', event_date='2020-01-01')  # Newly observed old event is not new occurrence.
        event(session, '103', last_seen=iso(NOW - timedelta(days=10)))
        event(session, '104', event_date='2026-11-13')
        session.commit()
    result = prepare(attention_engine)
    assert {row['event_id'] for row in result['matches']} == {'101', '104'}
    assert result['scope']['events_read'] == 3
    assert any('发生时间' in reason for reason in result['unmatched_reasons'])


def test_plain_recent_topic_does_not_semantically_absorb_years_old_occurrence(attention_engine):
    with Session(attention_engine) as session:
        topic(session)
        event(session, event_date='2020-01-01')
        session.commit()
    assert not prepare(attention_engine)['matches']


def test_cannot_splice_actor_and_action_from_different_headlines(attention_engine):
    with Session(attention_engine) as session:
        topic(session, '特朗普最新活动')
        session.add(AttentionItem(key='another_item', topic='attention_a', title='乌克兰提供援助',
                                  source_id='zhihu', last_seen=iso()))
        event(session)
        session.commit()
    assert not prepare(attention_engine)['matches']


def test_manual_decisions_survive_recompute_and_generation_fences_publication(attention_engine):
    with Session(attention_engine) as session:
        topic(session)
        event(session)
        event(session, '102')
        session.commit()
    matching.refresh_topic(attention_engine, 'attention_a', now=NOW)
    with Session(attention_engine) as session:
        current = session.get(AttentionTopic, 'attention_a')
        current.generation = 'g2'
        for match in session.exec(select(AttentionEventMatch)).all():
            match.status = 'confirmed' if match.event_id == '101' else 'rejected'
            session.add(match)
        session.add(current)
        session.commit()
    matching.refresh_topic(attention_engine, 'attention_a', now=NOW)
    assert [(row.event_id, row.status, row.generation) for row in rows(attention_engine)] == [
        ('101', 'confirmed', 'g2'), ('102', 'rejected', 'g2')]
    stale = prepare(attention_engine)
    with Session(attention_engine) as session:
        current = session.get(AttentionTopic, 'attention_a')
        current.generation = 'g3'
        session.add(current)
        session.commit()
    with Session(attention_engine) as session:
        assert matching.apply_match(session, stale)['stale']
        session.commit()
    assert all(row.generation == 'g2' for row in rows(attention_engine))


def test_stale_candidate_cleanup_retains_out_of_scope_human_decisions(attention_engine):
    with Session(attention_engine) as session:
        topic(session)
        for key in ('101', '102', '103'):
            event(session, key)
        session.commit()
    matching.refresh_topic(attention_engine, 'attention_a', now=NOW)
    with Session(attention_engine) as session:
        for match in session.exec(select(AttentionEventMatch)).all():
            if match.event_id in ('101', '102'):
                match.status = 'confirmed' if match.event_id == '101' else 'rejected'
                session.add(match)
        current = session.get(AttentionTopic, 'attention_a')
        current.title, current.generation = '人物未知的新闻', 'g2'
        member = session.get(AttentionItem, 'item_attention_a')
        member.title = current.title
        session.add(current); session.add(member)
        session.commit()
    matching.refresh_topic(attention_engine, 'attention_a', now=NOW)
    assert [(row.event_id, row.status, row.generation) for row in rows(attention_engine)] == [
        ('101', 'confirmed', 'g1'), ('102', 'rejected', 'g1')]


def test_watch_scope_exclusions_pause_and_version_tombstone_fences(attention_engine):
    with Session(attention_engine) as session:
        topic(session)
        for key, dimensions, exclude, enabled in [('w1', '援助', '', True), ('w2', '军演', '', True),
                                                 ('w3', '', '乌克兰', True), ('w4', '', '', False),
                                                 ('w5', '', '', True)]:
            session.add(Watch(key=key, name=key, kind='人物', scope='特朗普', dimensions=dimensions,
                              exclude=exclude, enabled=enabled))
        session.add(FocusTrash(watch='w5', name='w5', deleted_at=iso()))
        session.commit()
    prepared = prepare(attention_engine)
    assert {row['watch'] for row in prepared['watches']} == {'w1', 'w4'}
    assert any('已暂停' in reason for row in prepared['watches'] if row['watch'] == 'w4' for reason in row['reasons'])
    with Session(attention_engine) as session:
        watch = session.get(Watch, 'w1'); watch.version = 2; session.add(watch)
        session.add(FocusTrash(watch='w4', name='w4', deleted_at=iso()))
        session.commit()
    with Session(attention_engine) as session:
        result = matching.apply_match(session, prepared)
        session.commit()
        assert result['watches'] == 0
        assert session.exec(select(AttentionWatchMatch)).all() == []
        assert session.exec(select(WatchEvent)).all() == []


def test_query_caps_and_missing_data_are_explicit_unknown_not_zero(attention_engine):
    with Session(attention_engine) as session:
        topic(session)
        for index in range(matching.EVENT_LIMIT + 3):
            event(session, str(1000 + index))
        session.commit()
    result = prepare(attention_engine)
    assert result['scope']['events_read'] == matching.EVENT_LIMIT
    assert result['scope']['event_truncated'] and len(result['matches']) == matching.MATCH_LIMIT
    assert any('上限' in reason for reason in result['unmatched_reasons'])
    with Session(attention_engine) as session:
        current = session.get(AttentionTopic, 'attention_a')
        assert current.attention_score is None and current.rise_score is None


def test_step_does_one_due_topic_and_defers_empty_and_failed_matches(attention_engine, monkeypatch):
    with Session(attention_engine) as session:
        topic(session, '无具体内容', key='attention_a')
        topic(session, '另一条内容', key='attention_b')
        session.commit()
    monkeypatch.setattr(matching, '_now', lambda value=None: NOW if value is None else datetime.fromtimestamp(value, timezone.utc)
                        if isinstance(value, (int, float)) else value)
    assert matching.step(attention_engine)
    with Session(attention_engine) as session:
        assert session.get(AttentionTopic, 'attention_a').match_next_at == NOW.timestamp() + 300
        assert session.get(AttentionTopic, 'attention_b').match_next_at == 0
    def fail(*_, **__):
        raise ValueError('测试失败，请稍后重试')
    monkeypatch.setattr(matching, 'refresh_topic', fail)
    assert matching.step(attention_engine)
    with Session(attention_engine) as session:
        failed = session.get(AttentionTopic, 'attention_b')
        assert failed.match_next_at == NOW.timestamp() + 900
        assert failed.match_error == 'ValueError: 测试失败，请稍后重试'
    assert not matching.step(attention_engine)


def test_database_failure_never_attempts_another_error_write(attention_engine, monkeypatch):
    with Session(attention_engine) as session:
        topic(session)
        session.commit()
    monkeypatch.setattr(matching, '_now', lambda value=None: NOW)
    def fail(*_, **__):
        raise OperationalError('SELECT', {}, Exception('connection unavailable'))
    monkeypatch.setattr(matching, 'refresh_topic', fail)
    statements = []
    def record(_conn, _cursor, statement, *_):
        statements.append(statement.lower())
    sql_event.listen(attention_engine, 'before_cursor_execute', record)
    try:
        with pytest.raises(OperationalError):
            matching.step(attention_engine)
    finally:
        sql_event.remove(attention_engine, 'before_cursor_execute', record)
    assert statements and all(statement.lstrip().startswith('select') for statement in statements)
    with Session(attention_engine) as session:
        current = session.get(AttentionTopic, 'attention_a')
        assert current.match_error == '' and current.match_next_at == 0
