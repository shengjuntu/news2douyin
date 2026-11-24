"""Durable, bounded topic initialization and stale-result publication guards."""
import json
import time

import pytest
from sqlmodel import Session, select

from news2douyin.storage.db import make_engine, create_schema
from news2douyin.storage.models import ArticleVersion
from news2douyin.desk import focus_jobs as jobs, intent
from news2douyin.desk.models import Watch, WatchEvent, RadarEntry
from news2douyin.desk.focus_models import FocusDefinition, FocusBuild, FocusBackfill, FocusCandidate


@pytest.fixture
def engine(tmp_path):
    engine = make_engine('sqlite:///'+str(tmp_path/'focus.db'))
    create_schema(engine)
    yield engine
    engine.dispose()


def spec():
    return dict(name='军援政策跟踪', kind='长期事件', entities=[dict(name='特朗普', type='person', aliases=['Trump'])],
        keywords=['军援', 'military aid'], relations=[], include=['政策行动'], exclude=[],
        questions=['军援政策发生了什么变化？'], geography=[], time_start='', time_end='')


def watch(engine, monkeypatch):
    monkeypatch.setattr(intent, 'interpret', lambda description: dict(spec=spec(), method='model', message=''))
    with Session(engine) as s:
        draft = jobs.submit_build(s, '关注特朗普的军援政策')['key']
    assert jobs.step(engine)
    with Session(engine) as s:
        assert jobs.get_build(s, draft)['status'] == 'ready'
        w = jobs.create_from_build(s, draft)
        key = w.key
        assert jobs.create_from_build(s, draft).key == key
    return key


def card(engine, n=0):
    title='Trump announces military aid '+str(n)
    source='article_'+str(n)
    event='event_'+str(n).zfill(3)
    with Session(engine) as s:
        s.add(ArticleVersion(article_key=source, revision=1, content_hash='hash'+str(n),
            payload_json=json.dumps(dict(title=title, content=title+'. A policy statement.'))))
        c=RadarEntry(key=event,title=title,headings=title.lower(),material=title.lower(),seen='2026-11-12T00:00:00Z',
            priority=2,band='优先看',reason='',evidence='',geography='[]',
            sources=json.dumps([dict(key=source,title=title,revision=1)]))
        s.add(c);s.commit()
    return event


def candidate(engine, key, event):
    with Session(engine) as s:
        w=s.get(Watch,key)
        row=jobs.queue_event(s,w,s.get(RadarEntry,event))
        s.commit()
        return row.key if row else None


def test_build_and_judge_release_all_sql_connections_during_model_io(engine,monkeypatch):
    def interpret(description):
        assert engine.pool.checkedout()==0
        return dict(spec=spec(),method='model',message='')
    monkeypatch.setattr(intent,'interpret',interpret)
    with Session(engine) as s:draft=jobs.submit_build(s,'跟踪军援政策')['key']
    assert jobs.step(engine)
    with Session(engine) as s:key=jobs.create_from_build(s,draft).key
    event=card(engine)
    def judge(scope,snapshot,route):
        assert engine.pool.checkedout()==0
        source=snapshot.sources[0]
        assert source['revision']==1 and source['content'].endswith('statement.')
        return dict(decision='core',method='model',reason='政策变化',
            evidence=[dict(article=source['key'],revision=1,quote=source['title'])],questions=scope.questions)
    monkeypatch.setattr(intent,'judge',judge)
    assert jobs.step(engine)
    with Session(engine) as s:
        assert s.get(WatchEvent,(key,event)).relevance==2
        assert jobs.status(s,key)['counts']['core']==1


def test_backfill_is_bounded_and_resume_uses_keyset(engine,monkeypatch):
    # Legacy watches use the same bounded job; no LLM or definition required.
    with Session(engine) as s:
        w=Watch(key='legacy',name='Trump',kind='人物',scope='Trump')
        s.add(w);jobs.queue_backfill(s,w);s.commit()
    for n in range(27):card(engine,n)
    assert jobs.step(engine)
    with Session(engine) as s:
        job=s.get(FocusBackfill,'legacy')
        assert job.checked==25 and job.status=='running'
        saved=job.cursor
    assert jobs.step(engine)
    with Session(engine) as s:
        job=s.get(FocusBackfill,'legacy')
        assert job.checked==27 and job.status=='complete' and job.cursor>saved
        assert len(s.exec(select(WatchEvent)).all())==27
    assert jobs.step(engine) is False


def test_manual_exclusion_is_not_overwritten_while_judge_runs(engine,monkeypatch):
    key=watch(engine,monkeypatch);event=card(engine);candidate(engine,key,event)
    def judge(*args):
        with Session(engine) as s:
            s.merge(WatchEvent(watch=key,event=event,choice='exclude',reason='人工排除'));s.commit()
        return dict(decision='core',method='model',reason='属于政策',evidence=[],questions=[])
    monkeypatch.setattr(intent,'judge',judge)
    jobs.step(engine)
    with Session(engine) as s:
        assert s.get(WatchEvent,(key,event)).choice=='exclude'
        row=s.exec(select(FocusCandidate)).one()
        assert row.decision=='manual'


def test_scope_edit_during_judge_discards_old_result(engine,monkeypatch):
    key=watch(engine,monkeypatch);event=card(engine);candidate(engine,key,event)
    def judge(*args):
        with Session(engine) as s:
            w=s.get(Watch,key);w.version+=1;s.add(w);s.commit()
        return dict(decision='core',method='model',reason='旧判断',evidence=[],questions=[])
    monkeypatch.setattr(intent,'judge',judge)
    jobs.step(engine)
    with Session(engine) as s:
        assert s.get(WatchEvent,(key,event)) is None
        assert s.exec(select(FocusCandidate)).one().status=='superseded'


def test_new_source_revision_during_judge_discards_result(engine,monkeypatch):
    key=watch(engine,monkeypatch);event=card(engine);candidate(engine,key,event)
    def judge(*args):
        with Session(engine) as s:
            s.add(ArticleVersion(article_key='article_0',revision=2,content_hash='new',
                payload_json=json.dumps(dict(title='Updated',content='New material'))));s.commit()
        return dict(decision='core',method='model',reason='旧原文判断',evidence=[],questions=[])
    monkeypatch.setattr(intent,'judge',judge)
    jobs.step(engine)
    with Session(engine) as s:
        assert s.get(WatchEvent,(key,event)) is None
        assert s.exec(select(FocusCandidate)).one().status=='superseded'


def test_unavailable_model_retains_pending_with_backoff(engine,monkeypatch):
    key=watch(engine,monkeypatch);event=card(engine);candidate(engine,key,event)
    monkeypatch.setattr(intent,'judge',lambda *args:dict(decision='pending',method='unavailable',
        reason='模型暂不可用',evidence=[],questions=[]))
    jobs.step(engine)
    with Session(engine) as s:
        row=s.exec(select(FocusCandidate)).one()
        assert row.decision=='pending' and row.status=='pending' and row.next_attempt>time.time()
        assert s.get(WatchEvent,(key,event)) is None
        state=jobs.status(s,key)
        assert state['counts']['pending']==1
        assert state['pending'][0]['reason']=='模型暂不可用'
    assert jobs.step(engine) is False
    with Session(engine) as s:jobs.retry(s,key)
    assert jobs.step(engine)


def test_graph_route_can_queue_without_lexical_hit(engine,monkeypatch):
    key=watch(engine,monkeypatch);event=card(engine)
    with Session(engine) as s:
        w=s.get(Watch,key);c=s.get(RadarEntry,event)
        c.headings=c.material=c.title='Unfamiliar negotiation envoy';c.sources='[]'
        s.add(c);s.commit()
        row=jobs.queue_event(s,w,c,recall_extra=dict(channels=['graph'],paths=[{'source':'seed','target':event}],reason='有出处的共同参与者'))
        assert row and 'graph' in json.loads(row.recall_json)['channels']


def test_creating_watch_never_scans_history_in_request(engine,monkeypatch):
    from sqlalchemy import event as sql_event
    monkeypatch.setattr(intent,'interpret',lambda description:dict(spec=spec(),method='rules',message='离线生成'))
    with Session(engine) as s:draft=jobs.submit_build(s,'军援政策')['key']
    jobs.step(engine)
    statements=[]
    def record(conn,cursor,statement,*args):statements.append(statement.lower())
    sql_event.listen(engine,'before_cursor_execute',record)
    try:
        with Session(engine) as s:key=jobs.create_from_build(s,draft).key
    finally:sql_event.remove(engine,'before_cursor_execute',record)
    assert not any('from radarentry' in q for q in statements)
    with Session(engine) as s:
        assert s.get(FocusBackfill,key).status=='pending'
        assert s.get(FocusDefinition,key).method=='rules'


def test_stale_backfill_does_not_starve_another_watch(engine,monkeypatch):
    with Session(engine) as s:
        for key in ('a_stale','b_current'):
            w=Watch(key=key,name=key,kind='长期事件',scope='Trump')
            s.add(w);jobs.queue_backfill(s,w)
        s.commit()
        old=s.get(Watch,'a_stale');old.version=2;s.add(old);s.commit()
    card(engine)
    assert jobs.scan_step(engine)
    with Session(engine) as s:
        assert s.get(FocusBackfill,'a_stale').status=='superseded'
        assert s.get(FocusBackfill,'b_current').checked==1


def test_force_restore_rejudges_prior_manual_decision(engine,monkeypatch):
    key=watch(engine,monkeypatch);event=card(engine);candidate_id=candidate(engine,key,event)
    with Session(engine) as s:
        row=s.get(FocusCandidate,candidate_id);row.status='done';row.decision='manual';s.add(row)
        # link_event(auto) has already removed the manual WatchEvent here.
        jobs.queue_event(s,s.get(Watch,key),s.get(RadarEntry,event),force=True);s.commit()
    with Session(engine) as s:
        row=s.get(FocusCandidate,candidate_id)
        assert row.status=='pending' and not row.decision


def test_new_candidate_withdraws_stale_auto_material(engine,monkeypatch):
    key=watch(engine,monkeypatch);event=card(engine);candidate(engine,key,event)
    with Session(engine) as s:
        s.add(WatchEvent(watch=key,event=event,choice='auto',relevance=2,reason='旧判断'))
        c=s.get(RadarEntry,event);c.material+=' updated';s.add(c);s.commit()
        new=jobs.queue_event(s,s.get(Watch,key),c);s.commit()
        assert new
    with Session(engine) as s:
        assert s.get(WatchEvent,(key,event)) is None
        rows=s.exec(select(FocusCandidate)).all()
        assert len(rows)==2 and sorted(r.status for r in rows)==['pending','superseded']


def test_range_edit_keeps_parent_with_manual_children_and_research_history(engine,monkeypatch):
    from news2douyin.extraction.models import FocusPoint,PointReview
    key=watch(engine,monkeypatch)
    with Session(engine) as s:
        old=s.exec(select(FocusPoint).where(FocusPoint.watch==key)).one()
        s.add(FocusPoint(key='manualchild',watch=key,parent=old.key,text='人工补充的问题',created='2026-11-12T00:00:00Z'))
        s.add(PointReview(ticket='historical_ticket',point=old.key));s.commit()
        updated=spec();updated['questions']=['制裁政策有什么变化？']
        jobs.update_definition(s,key,1,'改为关注制裁',updated)
        assert s.get(FocusPoint,old.key) is not None
        assert s.get(FocusPoint,'manualchild').parent==old.key


def test_scope_edit_immediately_hides_old_auto_count(engine,monkeypatch):
    key=watch(engine,monkeypatch);event=card(engine);candidate(engine,key,event)
    monkeypatch.setattr(intent,'judge',lambda *args:dict(decision='core',method='model',reason='相关',evidence=[],questions=[]))
    jobs.step(engine)
    with Session(engine) as s:
        assert jobs.status(s,key)['counts']['core']==1
        jobs.update_definition(s,key,1,'关注制裁政策',spec())
        assert jobs.status(s,key)['counts']['core']==0
        # The old judgement is retained for review; no mass history DELETE.
        assert s.exec(select(FocusCandidate)).one().decision=='core'


def test_model_admission_records_change_and_live_core_schedules_review(engine,monkeypatch):
    from news2douyin.knowledge.models import FocusChange,PointMaterial
    from news2douyin.desk.models import WatchReview
    key=watch(engine,monkeypatch);event=card(engine);candidate(engine,key,event)
    with Session(engine) as s:
        w=s.get(Watch,key);w.auto_research=True;w.created='2026-11-01T00:00:00Z';s.add(w);s.commit()
    monkeypatch.setattr(intent,'judge',lambda scope,*args:dict(decision='core',method='model',reason='相关',evidence=[],questions=scope.questions))
    jobs.step(engine)
    with Session(engine) as s:
        changes=s.exec(select(FocusChange).where(FocusChange.watch==key)).all()
        assert len(changes)==1 and changes[0].kind=='added'
        review=s.exec(select(WatchReview).where(WatchReview.watch==key)).one()
        assert review.automatic and review.event==event
        material=s.exec(select(PointMaterial)).one()
        assert '不表示事实' in material.reason


def test_force_restore_queues_graph_only_event_without_lexical_match(engine,monkeypatch):
    key=watch(engine,monkeypatch);event=card(engine)
    with Session(engine) as s:
        c=s.get(RadarEntry,event)
        c.title=c.headings=c.material='An unfamiliar envoy arrives';c.sources='[]'
        s.add(c);s.commit()
        row=jobs.queue_event(s,s.get(Watch,key),c,force=True)
        assert row and json.loads(row.recall_json)['channels']==['recheck']
        assert '人工恢复' in json.loads(row.recall_json)['reason']


def test_old_topic_history_backfill_does_not_trigger_live_signal_review(engine,monkeypatch):
    from news2douyin.desk.models import WatchReview
    key=watch(engine,monkeypatch);event=card(engine)
    with Session(engine) as s:
        w=s.get(Watch,key);w.created='2025-01-01T00:00:00Z';w.auto_research=True;s.add(w);s.commit()
        jobs.update_definition(s,key,1,'重新关注军援政策',spec())
        c=s.get(RadarEntry,event);c.seen='2026-10-01T00:00:00Z';s.add(c);s.commit()
    monkeypatch.setattr(intent,'judge',lambda scope,*args:dict(decision='core',method='model',reason='历史背景相关',evidence=[],questions=scope.questions))
    # Only the history scanner enqueues this row, after the old topic is edited.
    jobs.step(engine)
    with Session(engine) as s:
        row=s.exec(select(FocusCandidate)).one()
        assert json.loads(row.recall_json)['historical'] is True
        assert s.get(WatchEvent,(key,event)) is not None
        assert not s.exec(select(WatchReview)).all()


def test_history_origin_does_not_broaden_recall_or_replace_direct_origin(engine,monkeypatch):
    key=watch(engine,monkeypatch);event=card(engine);candidate_id=candidate(engine,key,event)
    with Session(engine) as s:
        w=s.get(Watch,key);c=s.get(RadarEntry,event)
        same=jobs.queue_event(s,w,c,recall_extra={'historical':True})
        assert same.key==candidate_id and not json.loads(same.recall_json).get('historical')
        c.title=c.headings=c.material='An unrelated local festival';c.sources='[]';s.add(c);s.commit()
        assert jobs.queue_event(s,w,c,recall_extra={'historical':True}) is None


def test_restored_event_fingerprint_reactivates_its_prior_automatic_candidate(engine, monkeypatch):
    key = watch(engine, monkeypatch)
    event = card(engine)
    first = candidate(engine, key, event)
    monkeypatch.setattr(intent, 'judge', lambda *args: dict(
        decision='core', method='model', reason='材料支持', evidence=[], questions=[]))
    assert jobs._judge_step(engine)
    with Session(engine) as s:
        w = s.get(Watch, key)
        current = s.get(RadarEntry, event)
        title = current.title
        assert s.get(WatchEvent, (key, event)).choice == 'auto'
        current.title = 'Trump revises the military aid policy'
        s.add(current)
        newer = jobs.queue_event(s, w, current).key
        s.commit()
        assert s.get(WatchEvent, (key, event)) is None
        current.title = title
        s.add(current)
        returned = jobs.queue_event(s, w, current)
        assert returned.key == first and returned.status == 'pending'
        assert returned.attempts == 0 and returned.decision == ''
        s.commit()
        assert s.get(FocusCandidate, newer).status == 'superseded'
    assert jobs._judge_step(engine)
    with Session(engine) as s:
        assert s.get(FocusCandidate, first).decision == 'core'
        assert s.get(WatchEvent, (key, event)).choice == 'auto'
