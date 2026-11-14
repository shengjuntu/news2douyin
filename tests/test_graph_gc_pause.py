"""Operator pause is durable control, never a fabricated successful scan."""
import time

import pytest
from sqlmodel import Session, select

from news2douyin.desk import graph_gc
from news2douyin.desk.graph_maintenance_models import GraphGcControl, GraphGcState
from test_graph_gc import MemoryGraph, gc_engine, state_for


def checkpoint(engine):
    with Session(engine) as session:
        return {row.key: row.model_dump() for row in session.exec(select(GraphGcState)).all()}


def test_pause_resume_preserves_progress_across_worker_restart(gc_engine):
    mirror = MemoryGraph(gc_engine, {1:('Article','first'),2:('Article','second'),3:('Article','tail')})
    assert graph_gc.step(gc_engine, mirror)
    before = checkpoint(gc_engine)
    article = state_for(gc_engine,'Article')
    assert article.status == 'sweeping' and article.cursor == 3
    assert graph_gc.set_paused(gc_engine, mirror)['paused'] is True
    assert checkpoint(gc_engine) == before
    restarted = MemoryGraph(gc_engine, mirror.nodes, mirror.edges)
    assert graph_gc.summary(gc_engine, restarted)['state'] == 'paused'
    assert graph_gc.summary(gc_engine, restarted)['complete_labels'] == 0
    for _ in range(3):
        assert graph_gc.step(gc_engine, restarted) is False
    assert restarted.calls == [] and checkpoint(gc_engine) == before
    assert graph_gc.set_paused(gc_engine, restarted, False)['paused'] is False
    assert checkpoint(gc_engine) == before
    assert graph_gc.step(gc_engine, restarted)
    assert state_for(gc_engine,'Article').cycle == article.cycle
    assert not any('max(id(n))' in query for query, _ in restarted.calls)
    assert state_for(gc_engine,'Article').deleted == 3


def test_disable_flag_stops_without_false_completion_or_graph_io(gc_engine, monkeypatch):
    mirror = MemoryGraph(gc_engine, {1:('Article','first')})
    monkeypatch.setenv('NEWS2DOUYIN_GRAPH_GC_ENABLED','0')
    assert graph_gc.step(gc_engine, mirror) is False
    status = graph_gc.summary(gc_engine, mirror)
    assert status['state'] == 'disabled' and status['enabled'] is False
    assert status['complete_labels'] == 0 and mirror.calls == []
    assert checkpoint(gc_engine) == {}
    with Session(gc_engine) as session:
        assert session.exec(select(GraphGcControl)).all() == []
    monkeypatch.setenv('NEWS2DOUYIN_GRAPH_GC_ENABLED','1')
    assert graph_gc.step(gc_engine,mirror)
    assert state_for(gc_engine,'Article').deleted == 1


def test_timed_pause_expiry_and_namespace_isolation(gc_engine, monkeypatch):
    monkeypatch.setattr(graph_gc.time,'time',lambda:1000.)
    first = MemoryGraph(gc_engine)
    second = MemoryGraph(gc_engine)
    second.prefix = 'different_namespace'
    graph_gc.set_paused(gc_engine,first,True,resume_at=1100.)
    assert graph_gc.control(gc_engine,first) == dict(enabled=True,paused=True,resume_at=1100.)
    assert graph_gc.control(gc_engine,second)['paused'] is False
    monkeypatch.setattr(graph_gc.time,'time',lambda:1101.)
    assert graph_gc.control(gc_engine,first) == dict(enabled=True,paused=False,resume_at=0.)
    assert checkpoint(gc_engine) == {}


def test_pause_keeps_prior_error_and_backoff(gc_engine):
    mirror = MemoryGraph(gc_engine,{1:('Article','first')})
    mirror.fail_batch = True
    assert graph_gc.step(gc_engine,mirror) is False
    before = checkpoint(gc_engine)
    graph_gc.set_paused(gc_engine,mirror)
    status = graph_gc.summary(gc_engine,mirror)
    assert status['state'] == 'paused' and status['error']
    graph_gc.set_paused(gc_engine,mirror,False)
    assert graph_gc.summary(gc_engine,mirror)['state'] == 'error'
    assert checkpoint(gc_engine) == before


def test_summary_diagnostics_distinguish_candidate_read_timeout(gc_engine):
    mirror = MemoryGraph(gc_engine,{1:('Article','first')})
    mirror.fail_batch = True
    assert graph_gc.step(gc_engine,mirror) is False
    status = graph_gc.summary(gc_engine,mirror)
    diagnostic = status['diagnostics']
    assert diagnostic['phases']['highwater']['calls'] == 1
    assert diagnostic['phases']['read']['timeouts'] == 1
    assert diagnostic['last_failure']['phase'] == 'read'
    assert diagnostic['last_failure']['error'] == 'TimeoutError'
    assert diagnostic['last_failure']['timeout'] is True
    assert 'secret' not in str(diagnostic)
    assert '候选读取超时' in status['error']
    assert state_for(gc_engine,'Article').phase == 'batch'
    graph_gc.retry(gc_engine,mirror)
    assert graph_gc.step(gc_engine,mirror)
    assert graph_gc.summary(gc_engine,mirror)['diagnostics']['phases']['delete']['calls'] == 1


def test_plan_failure_after_restart_keeps_existing_highwater(gc_engine):
    mirror = MemoryGraph(gc_engine,{1:('Article','first'),2:('Article','second'),3:('Article','tail')})
    assert graph_gc.step(gc_engine,mirror)
    before = state_for(gc_engine,'Article')
    restarted = MemoryGraph(gc_engine,mirror.nodes,mirror.edges)
    restarted.plan = 'Node By Label Scan'
    assert graph_gc.step(gc_engine,restarted) is False
    failed = state_for(gc_engine,'Article')
    assert failed.phase == 'batch' and (failed.cursor,failed.through_key,failed.cycle) == (before.cursor,before.through_key,before.cycle)
    assert graph_gc.summary(gc_engine,restarted)['diagnostics']['last_failure']['phase'] == 'plan'
    restarted.plan = 'Node By Id Seek'
    graph_gc.retry(gc_engine,restarted)
    assert graph_gc.step(gc_engine,restarted)
    assert not any('max(id(n))' in query for query, _ in restarted.calls)


def test_diagnostics_are_bounded_and_do_not_persist_driver_messages(gc_engine):
    mirror = MemoryGraph(gc_engine)
    for number in range(140):
        with graph_gc._timed(mirror,'focus_'+str(number),'Article','read'):
            pass
    assert len(mirror._gc_diagnostics) == 128
    with pytest.raises(TimeoutError):
        with graph_gc._timed(mirror,'active_global','Article','read'):
            raise TimeoutError('redis://password:credential@private.example secret query')
    diagnostic = graph_gc.diagnostics(mirror)
    assert 'private.example' not in str(diagnostic) and 'credential' not in str(diagnostic)
    assert diagnostic['last']['timeout'] is True


@pytest.mark.parametrize('paused,resume_at',[(1,0),(True,-1),(True,float('nan')),(True,float('inf')),(False,100),(True,True)])
def test_invalid_pause_input_does_not_write_control(gc_engine,paused,resume_at):
    with pytest.raises(ValueError):
        graph_gc.set_paused(gc_engine,MemoryGraph(gc_engine),paused,resume_at=resume_at)
    with Session(gc_engine) as session:
        assert session.exec(select(GraphGcControl)).all() == []


def test_additive_control_table_upgrade_keeps_existing_gc_checkpoint(gc_engine):
    from news2douyin.storage.db import create_schema
    mirror = MemoryGraph(gc_engine,{1:('Article','first'),2:('Article','second'),3:('Article','tail')})
    assert graph_gc.step(gc_engine,mirror)
    before = checkpoint(gc_engine)
    GraphGcControl.__table__.drop(gc_engine)
    # Emulate an existing v0.25.1 database that has progress but no pause table.
    create_schema(gc_engine)
    assert checkpoint(gc_engine) == before
    assert graph_gc.control(gc_engine,mirror) == dict(enabled=True,paused=False,resume_at=0.)


@pytest.mark.parametrize('stage',['highwater','delete'])
def test_timeout_diagnostics_identify_other_graph_stages(gc_engine,stage):
    mirror = MemoryGraph(gc_engine,{1:('Article','first')})
    if stage == 'highwater':
        original = mirror.read
        def read(graph,query,**params):
            if 'max(id(n))' in query:
                raise TimeoutError('private highwater endpoint')
            return original(graph,query,**params)
        mirror.read = read
    else:
        def query(graph,query,**params):
            raise TimeoutError('private delete endpoint')
        mirror.query = query
    assert graph_gc.step(gc_engine,mirror) is False
    status = graph_gc.summary(gc_engine,mirror)
    assert status['diagnostics']['last_failure']['phase'] == stage
    assert status['diagnostics']['phases'][stage]['timeouts'] == 1
    assert 'private' not in status['error']


@pytest.mark.parametrize('environment_off',[False,True])
def test_mirror_maintenance_step_checks_control_before_graph_selection_and_lease(gc_engine,monkeypatch,environment_off):
    mirror = MemoryGraph(gc_engine)
    if environment_off:
        monkeypatch.setenv('NEWS2DOUYIN_GRAPH_GC_ENABLED','0')
    else:
        graph_gc.set_paused(gc_engine,mirror)
    def unexpected(*args,**kwargs):
        raise AssertionError('disabled or paused maintenance acquired the graph lease')
    monkeypatch.setattr(mirror,'exclusive',unexpected)
    cursor,global_turn=mirror._gc_graph_cursor,mirror._gc_global_turn
    assert mirror.maintenance_step(gc_engine) is False
    assert (mirror._gc_graph_cursor,mirror._gc_global_turn)==(cursor,global_turn)
    assert mirror.calls==[]
