"""Pending-only outbox reads, safe background index maintenance and real rates."""
from concurrent.futures import ThreadPoolExecutor
import json

import pytest
from sqlalchemy import event, inspect, text, update
from sqlmodel import Session, select

from news2douyin.desk.graph_queue import (
    INDEX_NAME, GraphQueueMetrics, QueueIndexError, ensure_queue_index, pick,
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
            plan = c.execute(text("EXPLAIN SELECT key,token,written FROM graphpending WHERE token <> written AND key >= 'event/' AND key < 'event0' ORDER BY key LIMIT 1")).all()
        assert INDEX_NAME in str(plan)


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


def test_index_build_keeps_rows_and_refuses_different_existing_definition(queue_engine):
    engine=queue_engine
    ensure_queue_index(engine)
    with engine.begin() as c:
        c.execute(text('DROP INDEX '+INDEX_NAME))
        c.execute(text('CREATE INDEX '+INDEX_NAME+' ON graphpending(key) WHERE token = written'))
    with pytest.raises(QueueIndexError,match='different definition'):
        ensure_queue_index(engine)
    assert INDEX_NAME in {row['name'] for row in inspect(engine).get_indexes('graphpending')}
    with engine.begin() as c:
        c.execute(text('DROP INDEX '+INDEX_NAME))
    assert ensure_queue_index(engine)['created']


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
