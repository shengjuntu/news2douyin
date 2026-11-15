"""Concrete issues, replay and latest-only projection on real SQL backends."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest
from sqlalchemy import event as sql_event, func
from sqlmodel import Session, select

from news2douyin.attention import grouping
from news2douyin.attention.models import AttentionItem, AttentionTopic
from news2douyin.desk.models import GraphPending
from news2douyin.storage.db import init_db, make_engine
from test_postgres_integration import postgres_url


TARIFF = '特朗普宣布对进口钢铝加征25%关税'
CEASEFIRE = '特朗普与乌克兰总统讨论俄乌停火方案'
NOW = '2026-11-14T06:00:00Z'


@pytest.fixture(params=['sqlite', 'postgres'])
def grouping_engine(request, tmp_path):
    url = request.getfixturevalue('postgres_url') if request.param == 'postgres' else 'sqlite:///' + str(tmp_path/'grouping.db')
    engine = make_engine(url)
    init_db(engine)
    yield engine
    engine.dispose()


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def source(name, titles, observed=NOW, *, kind='hottest', status='success', url=None):
    items = []
    for position, title in enumerate(titles, 1):
        key = _hash(name + title)
        items.append(dict(key=key, source_id=name, platform=name, source_kind=kind,
            audience_scope='cn_platform', native_id=key, title=title,
            url=url or f'https://{name}.example/news/{key}', excerpt='来源标题线索，尚未核验正文。',
            observed_at=observed, observed_list_position=position, archive_ref='newsnow/snapshots/test.json',
            content_key=_hash(''.join(titles)), raw_metric_text='', metric_raw={}))
    return dict(source_id=name, platform=name, kind=kind, audience_scope='cn_platform',
        status=status, observed_at=observed, archive_sha256='a'*64, items=items)


def batch(*sources):
    return dict(observed_at=max(row['observed_at'] for row in sources), sources=list(sources))


def metrics(prepared, *, position=1):
    return {key: dict(attention_score=80., rise_score=None, platform_count=2, item_count=len(value['item_keys']),
        baseline_state='insufficient', observed_at=prepared['observed_at'],
        platforms=[dict(source_id='zhihu', list_position=position, status='success')],
        notes=['测试数据不是真实传播量'], series=[dict(observed_at=prepared['observed_at'], attention_score=80.)])
        for key, value in prepared['topics'].items()}


def publish(engine, data, *, projection=None):
    with Session(engine) as session:
        prepared = grouping.prepare_batch(session, data)
    # This round-trip is deliberate: no ORM identity or non-JSON object can cross
    # the unlocked lake-computation boundary.
    prepared = json.loads(json.dumps(prepared, ensure_ascii=False))
    with Session(engine) as session:
        touched = grouping.apply_prepared(session, prepared, projection or metrics(prepared))
        session.commit()
    return prepared, touched


def state(engine):
    with Session(engine) as session:
        topics = [row.model_dump() for row in session.exec(select(AttentionTopic).order_by(AttentionTopic.key))]
        items = [row.model_dump() for row in session.exec(select(AttentionItem).order_by(AttentionItem.key))]
        pending = [row.key for row in session.exec(select(GraphPending))]
    return topics, items, pending


def test_concrete_cross_platform_groups_do_not_become_a_person_subject(grouping_engine):
    prepared, touched = publish(grouping_engine, batch(source('zhihu', [TARIFF, CEASEFIRE]),
        source('weibo', [TARIFF]), source('cls', [TARIFF], kind='realtime'),
        source('xueqiu', ['英伟达'], kind='non_news')))
    topics, items, pending = state(grouping_engine)
    assert len(topics) == len(touched) == 2
    assert len(items) == 4
    tariff = next(row for row in topics if row['title'] == TARIFF)
    assert {row['title'] for row in items if row['topic'] == tariff['key']} == {TARIFF}
    assert len(prepared['topics'][tariff['key']]['item_keys']) == 3
    assert 'attention/' + tariff['key'] in pending
    assert all(len(row['normalized_hash']) == 64 for row in topics)
    assert all(len(row['canonical_hash']) == 64 for row in items)
    assert all(json.loads(row['snapshot_ref_json'])['archive_sha256'] == 'a'*64 for row in items)
    methods = {json.loads(row['grouping_json'])['method'] for row in items if row['topic'] == tariff['key']}
    assert methods == {'new_topic', 'same_title'}


def test_near_titles_require_action_context_and_preserve_numbers_denials_targets(grouping_engine):
    titles = [TARIFF, '特朗普对进口钢铝宣布加征25%关税',
              '特朗普否认对进口钢铝加征25%关税', '特朗普宣布对进口钢铝加征35%关税',
              '特朗普宣布对进口汽车加征25%关税', CEASEFIRE,
              '特朗普宣布对加拿大加征25%关税', '特朗普宣布对墨西哥加征25%关税']
    publish(grouping_engine, batch(*(source('p'+str(index), [title]) for index, title in enumerate(titles))))
    topics, items, _ = state(grouping_engine)
    by_title = {row['title']: row['topic'] for row in items}
    assert by_title[TARIFF] == by_title[titles[1]]
    assert len(topics) == 7
    assert len({by_title[title] for title in titles[2:]}) == 6


def test_url_normalization_is_strong_but_reused_urls_cannot_erase_negation(grouping_engine):
    publish(grouping_engine, batch(source('one', [TARIFF], url='https://wire.example/article/42?utm_source=a'),
        source('two', ['特朗普对进口钢铝加征25%关税'], url='https://wire.example/article/42#details'),
        source('three', ['特朗普否认对进口钢铝加征25%关税'], url='https://wire.example/article/42')))
    topics, items, _ = state(grouping_engine)
    assert len(topics) == 2
    positive = [row for row in items if '否认' not in row['title']]
    assert len({row['topic'] for row in positive}) == 1
    assert any(json.loads(row['grouping_json'])['method'] == 'same_url' for row in positive)


def test_exact_replay_is_a_noop_and_preserves_matcher_summary(grouping_engine):
    data = batch(source('zhihu', [TARIFF]), source('weibo', [TARIFF]))
    prepared, _ = publish(grouping_engine, data)
    key = next(iter(prepared['topics']))
    with Session(grouping_engine) as session:
        row = session.get(AttentionTopic, key)
        saved = json.loads(row.summary_json)
        saved['matching'] = dict(state='not_matched', reasons=['需要具体行为证据'])
        row.summary_json = json.dumps(saved, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        session.commit()
    before = state(grouping_engine)
    _, touched = publish(grouping_engine, data)
    assert touched == []
    assert state(grouping_engine) == before


def test_late_observations_expand_history_without_overwriting_current_snapshot(grouping_engine):
    recent = batch(source('zhihu', [TARIFF], observed='2026-11-14T08:00:00Z'))
    prepared, _ = publish(grouping_engine, recent)
    key = next(iter(prepared['topics']))
    old = batch(source('zhihu', [TARIFF], observed='2026-11-14T06:00:00Z'))
    publish(grouping_engine, old)
    topics, items, _ = state(grouping_engine)
    assert len(topics) == len(items) == 1
    assert topics[0]['key'] == key
    assert topics[0]['first_seen'] == '2026-11-14T06:00:00Z'
    assert topics[0]['last_seen'] == items[0]['last_seen'] == '2026-11-14T08:00:00Z'
    assert json.loads(items[0]['snapshot_ref_json'])['observed_at'] == '2026-11-14T08:00:00Z'
    assert json.loads(topics[0]['summary_json'])['observed_at'] == '2026-11-14T08:00:00Z'


def test_failed_or_empty_source_refreshes_unknown_not_zero(grouping_engine):
    prepared, _ = publish(grouping_engine, batch(source('zhihu', [TARIFF])))
    key = next(iter(prepared['topics']))
    with Session(grouping_engine) as session:
        prepared = grouping.prepare_batch(session, batch(source('zhihu', [], observed='2026-11-14T07:00:00Z', status='failed')))
    assert prepared['topics'][key]['item_keys']
    unknown = {key: dict(attention_score=None, rise_score=None, platform_count=0, item_count=0,
        baseline_state='insufficient', observed_at='2026-11-14T07:00:00Z',
        platforms=[dict(source_id='zhihu', status='failed', list_position=None)], series=[], notes=[])}
    with Session(grouping_engine) as session:
        assert grouping.apply_prepared(session, prepared, unknown) == [key]
        session.commit()
    topic = state(grouping_engine)[0][0]
    assert topic['attention_score'] is None and topic['rise_score'] is None
    assert topic['last_seen'] == NOW
    assert json.loads(topic['summary_json'])['platforms'][0]['status'] == 'failed'


def test_rollback_then_replay_does_not_multiply_members(grouping_engine):
    data = batch(source('zhihu', [TARIFF]), source('weibo', [TARIFF]))
    with Session(grouping_engine) as session:
        prepared = grouping.prepare_batch(session, data)
    with Session(grouping_engine) as session:
        grouping.apply_prepared(session, prepared, metrics(prepared))
        session.flush()
        session.rollback()
    assert state(grouping_engine) == ([], [], [])
    publish(grouping_engine, data)
    publish(grouping_engine, data)
    topics, items, _ = state(grouping_engine)
    assert len(topics) == 1 and len(items) == 2


def test_generation_fence_rejects_prepared_stale_results(grouping_engine):
    data = batch(source('zhihu', [TARIFF]))
    publish(grouping_engine, data)
    with Session(grouping_engine) as session:
        prepared = grouping.prepare_batch(session, data)
    changed = deepcopy(data)
    changed['observed_at'] = changed['sources'][0]['observed_at'] = '2026-11-14T07:00:00Z'
    changed['sources'][0]['items'][0]['observed_at'] = '2026-11-14T07:00:00Z'
    publish(grouping_engine, changed)
    with Session(grouping_engine) as session:
        with pytest.raises(grouping.PreparationConflict):
            grouping.apply_prepared(session, prepared, metrics(prepared))
        session.rollback()


def test_generic_headlines_never_glue_into_a_permanent_subject(grouping_engine):
    publish(grouping_engine, batch(source('one', ['特朗普', '突发重大消息'])))
    publish(grouping_engine, batch(source('two', ['特朗普', '突发重大消息'])))
    assert len(state(grouping_engine)[0]) == 4
    publish(grouping_engine, batch(source('one', ['特朗普', '突发重大消息'], observed='2026-11-16T06:00:00Z')))
    assert len(state(grouping_engine)[0]) == 6


def test_prepare_is_bounded_read_only_and_metric_members_are_explicitly_capped(grouping_engine):
    statements = []
    with Session(grouping_engine) as session:
        for index in range(205):
            topic = 'old-' + str(index)
            session.add(AttentionTopic(key=topic, title='不会匹配的旧议题' + str(index),
                normalized_title='不会匹配的旧议题' + str(index), first_seen='2020-01-01T00:00:00Z',
                last_seen='2020-01-01T00:00:00Z', generation='old'))
        session.commit()
    data = batch(*(source('p'+str(index), [TARIFF]) for index in range(205)))
    def record(_connection, _cursor, statement, *_args):
        statements.append(statement.lower())
    sql_event.listen(grouping_engine, 'before_cursor_execute', record)
    try:
        with Session(grouping_engine) as session:
            prepared = grouping.prepare_batch(session, data)
        assert all(sql.lstrip().startswith('select') for sql in statements)
        assert not any('count(' in sql for sql in statements)
        assert not any('attentionitem.*' in sql or 'attentiontopic.*' in sql for sql in statements)
        assert all('limit' in sql or ' in (' in sql for sql in statements)
    finally:
        sql_event.remove(grouping_engine, 'before_cursor_execute', record)
    assert len(prepared['topics']) == 1
    topic = next(iter(prepared['topics'].values()))
    assert len(topic['item_keys']) == 200 and topic['member_keys_truncated']


def test_multikilobyte_title_url_use_short_index_hashes(grouping_engine):
    title = '跨境供应链新政策' * 350
    url = 'https://wire.example/article/' + 'long-part-' * 650
    publish(grouping_engine, batch(source('one', [title], url=url), source('two', [title], url=url)))
    topics, items, _ = state(grouping_engine)
    assert len(topics) == 1 and len(items) == 2
    assert topics[0]['normalized_hash'] == _hash(grouping.normalize_title(title))
    assert len(items[0]['canonical_hash']) == 64


def test_english_actor_only_overlap_does_not_merge_different_actions(grouping_engine):
    publish(grouping_engine, batch(source('one', ['Trump announces 25% tariff on steel imports']),
        source('two', ['Trump denies 25% tariff on steel imports']),
        source('three', ['Trump discusses Ukraine ceasefire proposal'])))
    assert len(state(grouping_engine)[0]) == 3


def test_unknown_actor_and_relative_time_changes_remain_separate(grouping_engine):
    publish(grouping_engine, batch(source('one', ['张三宣布对进口钢铝加征25%关税']),
        source('two', ['李四宣布对进口钢铝加征25%关税']),
        source('three', ['Alice announces 25% tariff on steel imports']),
        source('four', ['Alise announces 25% tariff on steel imports']),
        source('five', ['特朗普今日宣布对进口钢铝加征25%关税']),
        source('six', ['特朗普昨日宣布对进口钢铝加征25%关税'])))
    assert len(state(grouping_engine)[0]) == 6


def test_active_membership_crossing_episode_boundary_does_not_duplicate_sql_item(grouping_engine):
    timestamp = datetime.fromisoformat(NOW.replace('Z', '+00:00')).timestamp()
    boundary = (int(timestamp // (72*3600))+1) * (72*3600)
    before = datetime.fromtimestamp(boundary-3600, timezone.utc).isoformat().replace('+00:00', 'Z')
    after = datetime.fromtimestamp(boundary+3600, timezone.utc).isoformat().replace('+00:00', 'Z')
    publish(grouping_engine, batch(source('zhihu', [TARIFF], observed=before)))
    initial = state(grouping_engine)
    publish(grouping_engine, batch(source('zhihu', [TARIFF], observed=after)))
    topics, items, _ = state(grouping_engine)
    assert len(topics) == len(items) == 1
    assert items[0]['key'] == initial[1][0]['key']
    assert items[0]['last_seen'] == after
