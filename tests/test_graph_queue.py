"""Pending-only outbox reads, safe background index maintenance and real rates."""
from concurrent.futures import ThreadPoolExecutor
import json
import os

import pytest
from sqlalchemy import event, inspect, text, update
from sqlmodel import Session, select

from news2douyin.desk.graph_queue import (
    INDEX_NAME, POSTGRES_INDEX_NAME, GraphQueueMetrics, QueueIndexError, ensure_queue_index, pick,
)
from news2douyin.desk.models import GraphPending
from news2douyin.storage.db import make_engine
from test_postgres_integration import postgres_url


@pytest.fixture(params=['sqlite', 'postgres'])
def queue_engine(request, tmp_path):
    url = request.getfixturevalue('postgres_url') if request.param == 'postgres' else 'sqlite:///'+str(tmp_path/'queue.db')
    engine = make_engine(url)
    GraphPending.__table__.create(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_pending_index_finds_tail_after_large_acknowledged_prefix(queue_engine):
    engine = queue_engine
    with engine.begin() as c:
        c.execute(GraphPending.__table__.insert(), [dict(key=f'event/{i:06}', token='done', written='done') for i in range(2000)] +
            [dict(key='event/z-last',token='new',written='old'), dict(key='watch/a-topic',token='topic',written='')])
    first = ensure_queue_index(engine)
    assert first['state'] == 'ready' and first['valid']
    assert not ensure_queue_index(engine)['created']
    with Session(engine) as s:
        assert pick(s).key == 'event/z-last'
        assert pick(s, prefer_watch=True).key == 'watch/a-topic'
    if engine.dialect.name == 'sqlite':
        with engine.connect() as c:
            plan = c.execute(text("EXPLAIN QUERY PLAN SELECT key,token,written FROM graphpending WHERE token <> written AND key >= 'event/' AND key < 'event0' ORDER BY key LIMIT 1")).all()
        assert INDEX_NAME in str(plan)
    else:
        with engine.connect() as c:
            c.execute(text('ANALYZE graphpending'))
            plan = c.execute(text('''EXPLAIN SELECT key,token,written FROM graphpending WHERE token <> written
                AND key COLLATE "C" >= 'event/' AND key COLLATE "C" < 'event0'
                ORDER BY key COLLATE "C" LIMIT 1''')).all()
        assert POSTGRES_INDEX_NAME in str(plan)
        assert 'Index Cond' in str(plan)


def test_watch_preference_and_empty_range_fallback_never_claim_or_modify(queue_engine):
    engine = queue_engine
    with Session(engine) as s:
        s.add_all([GraphPending(key='event/a',token='a'), GraphPending(key='watch/b',token='b'),
                   GraphPending(key='watch/already',token='done',written='done')])
        s.commit()
        assert pick(s, prefer_watch=True).key == 'watch/b'
        assert s.get(GraphPending, 'watch/b').written == ''
        row=s.get(GraphPending,'watch/b');row.written=row.token;s.add(row);s.commit()
        assert pick(s, prefer_watch=True).key == 'event/a'
        row=s.get(GraphPending,'event/a');row.written=row.token;s.add(row);s.commit()
        assert pick(s) is None


def test_pending_queue_pick_uses_small_read_only_named_queries(queue_engine):
    statements=[]
    def capture(_c,_cursor,sql,_args,context,_many):
        statements.append((sql,context.execution_options.get('n2d_query_name')))
    event.listen(queue_engine,'before_cursor_execute',capture)
    try:
        with Session(queue_engine) as s:
            assert pick(s,prefer_watch=True) is None
    finally:
        event.remove(queue_engine,'before_cursor_execute',capture)
    assert len(statements)==2
    assert all(sql.lstrip().upper().startswith('SELECT') and 'LIMIT' in sql.upper() for sql,_ in statements)
    assert [name for _,name in statements]==['desk.graph_queue.pick.watch','desk.graph_queue.pick.event']


def test_attempt_cursor_prevents_hot_first_key_from_starving_other_events(queue_engine):
    with Session(queue_engine) as s:
        s.add_all([GraphPending(key='event/'+key,token='initial') for key in ('a-hot','b-next','c-next')])
        s.add_all([GraphPending(key='watch/'+key,token='initial') for key in ('a-topic','b-topic')])
        s.commit()
    cursor=''; seen=[]; acknowledgements=[]
    for attempt in range(4):
        with Session(queue_engine) as s:
            row=pick(s,after_event=cursor)
            key,token=row.key,row.token
        if key=='event/a-hot':
            # New work arrives during graph I/O, so the old token cannot ack.
            with queue_engine.begin() as c:
                c.execute(update(GraphPending).where(GraphPending.key==key).values(token='changed-'+str(attempt)))
        with queue_engine.begin() as c:
            result=c.execute(update(GraphPending).where(GraphPending.key==key,
                GraphPending.token==token).values(written=token))
            acknowledgements.append(result.rowcount)
        cursor=key  # Advance on attempt, even when the token was invalidated.
        seen.append(key)
    assert seen==['event/a-hot','event/b-next','event/c-next','event/a-hot']
    assert acknowledgements==[0,1,1,0]
    with Session(queue_engine) as s:
        # Topic cursor is independent and wraps in its own key range.
        assert pick(s,prefer_watch=True,after_watch='watch/a-topic').key=='watch/b-topic'
        assert pick(s,prefer_watch=True,after_watch='watch/b-topic').key=='watch/a-topic'


def test_empty_queue_cursor_wrap_remains_bounded_and_read_only(queue_engine):
    statements=[]
    def capture(_c,_cursor,sql,_args,context,_many):
        statements.append(sql)
    event.listen(queue_engine,'before_cursor_execute',capture)
    try:
        with Session(queue_engine) as s:
            assert pick(s,after_event='event/z',after_watch='watch/z') is None
    finally:
        event.remove(queue_engine,'before_cursor_execute',capture)
    assert len(statements)==4
    assert all(sql.lstrip().upper().startswith('SELECT') and 'LIMIT' in sql.upper() for sql in statements)


def test_attention_range_is_opt_in_indexed_and_has_its_own_attempt_cursor(queue_engine):
    with Session(queue_engine) as s:
        s.add_all([GraphPending(key=key,token='new') for key in (
            'event/hot','watch/topic','attention/a-topic','attention/b-topic','attention/中文')])
        s.commit()
    assert ensure_queue_index(queue_engine)['state']=='ready'
    with Session(queue_engine) as s:
        assert pick(s).key=='event/hot'
        assert pick(s,include_attention=True,prefer_attention=True).key=='attention/a-topic'
        assert pick(s,include_attention=True,prefer_attention=True,
            after_attention='attention/a-topic').key=='attention/b-topic'
        assert pick(s,include_attention=True,prefer_attention=True,
            after_attention='attention/中文').key=='attention/a-topic'
    if queue_engine.dialect.name=='postgresql':
        with queue_engine.connect() as c:
            c.execute(text('SET enable_seqscan=off'))
            plan=c.execute(text('''EXPLAIN SELECT key,token,written FROM graphpending
                WHERE token<>written AND key COLLATE "C">='attention/' AND key COLLATE "C"<'attention0'
                ORDER BY key COLLATE "C" LIMIT 1''')).all()
        assert POSTGRES_INDEX_NAME in str(plan) and 'Index Cond' in str(plan)


def test_index_build_keeps_rows_and_refuses_different_existing_definition(queue_engine):
    engine=queue_engine
    name=ensure_queue_index(engine)['index']
    with engine.begin() as c:
        c.execute(text('DROP INDEX '+name))
        c.execute(text('CREATE INDEX '+name+' ON graphpending(key) WHERE token = written'))
    with pytest.raises(QueueIndexError,match='different definition'):
        ensure_queue_index(engine)
    assert name in {row['name'] for row in inspect(engine).get_indexes('graphpending')}
    with engine.begin() as c:
        c.execute(text('DROP INDEX '+name))
    assert ensure_queue_index(engine)['created']


def test_postgres_index_checks_collation_and_keeps_legacy_index(postgres_url):
    engine=make_engine(postgres_url)
    try:
        GraphPending.__table__.create(engine)
        with engine.begin() as c:
            # This matches v0.25.1's columns/predicate but has the wrong
            # collation identity, including on a C-locale database.
            c.execute(text('CREATE INDEX '+POSTGRES_INDEX_NAME+' ON graphpending(key) WHERE token <> written'))
        with pytest.raises(QueueIndexError,match='different definition'):
            ensure_queue_index(engine)
        with engine.begin() as c:
            c.execute(text('DROP INDEX '+POSTGRES_INDEX_NAME))
        assert ensure_queue_index(engine)['created']
        names={row['name'] for row in inspect(engine).get_indexes('graphpending')}
        assert {INDEX_NAME,POSTGRES_INDEX_NAME} <= names
    finally:
        engine.dispose()


def test_postgres_queue_and_cursor_use_c_order_with_database_locale(postgres_url):
    engine=make_engine(postgres_url)
    try:
        GraphPending.__table__.create(engine)
        ensure_queue_index(engine)
        with engine.connect() as c:
            locale=c.execute(text('SELECT datcollate FROM pg_database WHERE datname=current_database()')).scalar_one()
            expected=os.getenv('NEWS2DOUYIN_TEST_POSTGRES_LOCALE')
            if expected:
                assert locale==expected
            if locale=='en_US.utf8':
                # This exact v0.25.1 predicate excluded genuine work in the
                # field. Retain the regression under its actual DB locale.
                assert c.execute(text("SELECT 'event/evt_intake_a' < 'event0'")).scalar_one() is False
        event_keys=['event/evt_intake_a','event/a-z','event/a0','event/a_z','event/é','event/中文']
        watch_keys=['watch/a-z','watch/a0','watch/a_z']
        with Session(engine) as s:
            s.add_all([GraphPending(key=key,token='new') for key in event_keys+watch_keys]);s.commit()
            expected=sorted(event_keys)
            cursor=''
            seen=[]
            for _ in event_keys:
                row=pick(s,after_event=cursor)
                cursor=row.key;seen.append(cursor)
            assert seen==expected
            assert pick(s,after_event=cursor).key==expected[0]
            cursor=''
            seen=[]
            for _ in watch_keys:
                row=pick(s,prefer_watch=True,after_watch=cursor)
                cursor=row.key;seen.append(cursor)
            assert seen==sorted(watch_keys)
    finally:
        engine.dispose()


def test_postgres_rare_watch_is_an_index_range_not_a_filtered_pending_scan(postgres_url):
    engine=make_engine(postgres_url)
    try:
        GraphPending.__table__.create(engine)
        with engine.begin() as c:
            c.execute(GraphPending.__table__.insert(),
                [dict(key=f'event/evt_intake_{i:06}',token='new',written='') for i in range(20000)]+
                [dict(key='watch/only-topic',token='new',written='')])
        ensure_queue_index(engine)
        with engine.connect() as c:
            c.execute(text('ANALYZE graphpending'))
            plan=c.execute(text('''EXPLAIN (ANALYZE,FORMAT JSON)
                SELECT key,token,written FROM graphpending WHERE token <> written
                AND key COLLATE "C" >= 'watch/' AND key COLLATE "C" < 'watch0'
                ORDER BY key COLLATE "C" LIMIT 1''')).scalar_one()[0]['Plan']
        def nodes(node):
            yield node
            for child in node.get('Plans',[]):
                yield from nodes(child)
        scans=[node for node in nodes(plan) if node.get('Index Name')==POSTGRES_INDEX_NAME]
        assert len(scans)==1,plan
        condition=scans[0].get('Index Cond','')
        assert 'watch/' in condition and 'watch0' in condition,plan
        assert scans[0].get('Rows Removed by Filter',0)==0,plan
        assert scans[0]['Actual Rows']==1,plan
        with Session(engine) as s:
            assert pick(s,prefer_watch=True).key=='watch/only-topic'
    finally:
        engine.dispose()


def test_projection_metrics_separate_actual_acknowledgement_from_attempts():
    clock=[100.0]
    metrics=GraphQueueMetrics(clock=lambda:clock[0])
    metrics.record('event',30,acknowledged=True,graph_ms=20,sql_ms=10)
    metrics.record('event',40,stale=True)
    metrics.record('watch',50,failed=True)
    clock[0]+=10
    report=metrics.snapshot(pending=100)
    assert report['totals']['attempts']==3
    assert report['totals']['acknowledged']==1
    assert report['totals']['events']==1 and report['totals']['watches']==0
    assert report['totals']['stale']==report['totals']['failed']==1
    assert report['recent']['acknowledged']==1
    assert report['backlog']['eta_seconds'] is None
    with pytest.raises(ValueError):metrics.record('event',1,acknowledged=True,failed=True)


def test_event_and_topic_latency_distributions_remain_separate():
    clock=[0.0]
    metrics=GraphQueueMetrics(capacity=8,clock=lambda:clock[0])
    for _ in range(40):
        metrics.record('event',30,acknowledged=True,graph_ms=20,load_ms=3,ack_ms=2,graph_calls=4)
    for _ in range(4):
        metrics.record('watch',100000,stale=True,graph_ms=90000,load_ms=5000,ack_ms=50,graph_calls=1000)
    clock[0]=100
    report=metrics.snapshot()
    events=report['by_kind']['event']; watches=report['by_kind']['watch']
    assert events['totals']['attempts']==events['totals']['acknowledged']==40
    assert events['latency_ms']['mean']==events['latency_ms']['p95']==30
    assert events['retained_samples']==8
    assert watches['totals']['attempts']==watches['totals']['stale']==4
    assert watches['totals']['acknowledged']==0
    assert watches['latency_ms']['mean']==watches['latency_ms']['p95']==100000
    assert watches['totals']['graph_calls']==4000
    assert events['totals']['load_ms']==120 and events['totals']['ack_ms']==80
    assert events['totals']['other_ms']==200
    assert events['recent']['rate_per_second']==.4
    assert watches['recent']['rate_per_second']==0
    # Interleaving topic bursts never evicts this event-only latency buffer.
    for _ in range(20):metrics.record('watch',1000,partial=True)
    report=metrics.snapshot()
    assert report['by_kind']['event']['latency_ms']['mean']==30
    assert report['by_kind']['watch']['totals']['partial']==20
    assert report['totals']['acknowledged']==40
    with pytest.raises(ValueError):metrics.record('watch',1,partial=True,stale=True)


def test_per_kind_recent_window_expires_and_partial_is_not_completion():
    clock=[0.0]
    metrics=GraphQueueMetrics(window_seconds=5,clock=lambda:clock[0])
    metrics.record('event',10,acknowledged=True)
    metrics.record('watch',20,partial=True)
    clock[0]=2
    first=metrics.snapshot()
    assert first['by_kind']['event']['recent']['acknowledged']==1
    assert first['by_kind']['watch']['totals']['partial']==1
    assert first['by_kind']['watch']['recent']['acknowledged']==0
    clock[0]=10
    assert metrics.snapshot()['by_kind']['event']['recent']['acknowledged']==0


def test_backlog_eta_uses_net_decrease_and_stays_unknown_when_queue_grows():
    clock=[0.0]
    metrics=GraphQueueMetrics(clock=lambda:clock[0])
    assert metrics.snapshot(pending=100)['backlog']['eta_seconds'] is None
    for _ in range(50):metrics.record('event',1,acknowledged=True)
    clock[0]=10
    # Fifty acknowledgements do not imply net drain: new work increased backlog.
    report=metrics.snapshot(pending=110)
    assert report['backlog']['net_rate_per_second']==-1
    assert report['backlog']['eta_seconds'] is None
    clock[0]=20
    report=metrics.snapshot(pending=80)
    assert report['backlog']['net_rate_per_second']==1
    assert report['backlog']['eta_seconds']==80
    clock[0]=21
    assert metrics.snapshot(pending=0)['backlog']['eta_seconds']==0


def test_projection_telemetry_is_bounded_thread_safe_and_never_stores_identifiers():
    metrics=GraphQueueMetrics(capacity=8)
    def record(worker):
        for _ in range(20):
            metrics.record('event/private-password-or-key-'+str(worker),1,acknowledged=True)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(record,range(8)))
    report=metrics.snapshot()
    assert report['retained_samples']==8 and report['totals']['acknowledged']==160
    assert report['totals']['events']==report['totals']['watches']==0
    assert 'private-password' not in json.dumps(report)
