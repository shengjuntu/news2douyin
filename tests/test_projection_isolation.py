"""Persistent failure holds and honest, bounded projection counters."""
from datetime import datetime,timezone
import json
import random
import string

import pytest
from sqlalchemy import event as sql_event,func
from sqlalchemy.exc import OperationalError
from sqlmodel import Session,select

from news2douyin.intake import projection
from news2douyin.intake.models import IntakeBatch
from news2douyin.storage.db import init_db,make_engine
from news2douyin.storage.models import Article,ArticleVersion
from test_intake_projection import archive,line
from test_postgres_integration import postgres_url


@pytest.fixture(params=['sqlite','postgres'])
def isolated(request,tmp_path,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_GKG_AUTO_EVENTS','0')
    url=request.getfixturevalue('postgres_url') if request.param=='postgres' else 'sqlite:///'+str(tmp_path/'isolated.db')
    engine=make_engine(url);init_db(engine)
    try:yield engine,tmp_path/'runs'
    finally:engine.dispose()


def test_old_receipts_do_not_get_scanned_or_reported_as_zero(isolated):
    engine,_=isolated
    with Session(engine) as s:
        s.add(projection.IntakeProjectionReceipt(key='old',feed='gkg_english',stamp='20261104120000',row_index=1,status='projected'))
        s.add(IntakeBatch(key='old_batch',feed='gkg_english',stamp='20261104120000',state='archived',rows=10,projected_offset=3))
        s.commit()
    payload=dict(provider='fixture',record_id='new',title='A genuine new external lead')
    projection.import_document(engine,payload)
    assert projection.import_document(engine,payload)['reused']
    statements=[]
    def record(_c,_cu,sql,*_args):statements.append(sql.lower())
    sql_event.listen(engine,'before_cursor_execute',record)
    try:data=projection.projection_status(engine)
    finally:sql_event.remove(engine,'before_cursor_execute',record)
    assert data['projected'] is None and data['skipped'] is None and data['external_imported'] is None
    assert not data['counts_known'] and data['processed_rows']==3 and data['waiting']==7
    assert data['since_upgrade']==dict(projected=0,skipped=0,external_imported=1)
    assert not any('intakeprojectionreceipt' in sql and ('count(' in sql or 'group by' in sql) for sql in statements)


def test_observer_cannot_fail_already_committed_work(isolated):
    engine,root=isolated;stamp='20261104120000'
    key=archive(engine,root,[line(stamp,1,'A preserved source title')])
    def broken(_chunk):raise RuntimeError('telemetry is not work')
    assert projection.project_step(engine,root,observer=broken)
    with Session(engine) as s:
        assert s.get(IntakeBatch,key).projected_offset==1
        assert s.get(projection.IntakeProjectionFailure,key) is None
    assert projection.projection_status(engine)['projected']==1


def test_prefetch_failure_does_not_blame_the_first_requested_row(isolated,monkeypatch):
    from news2douyin.intake.lake import Lake
    engine,root=isolated;stamp='20261104120000'
    key=archive(engine,root,[line(stamp,i,'A preserved source title') for i in range(1,5)])
    original=Lake.rows
    def failed(*args,**kwargs):raise ValueError('damaged row later in the prefetched block')
    with monkeypatch.context() as scoped:
        scoped.setattr(Lake,'rows',failed)
        with pytest.raises(ValueError):projection.project_step(engine,root,limit=4,now=100)
    with Session(engine) as s:
        failure=s.get(projection.IntakeProjectionFailure,key)
        assert failure.row_index is None and failure.phase=='lake_read'
        assert json.loads(failure.error_context_json)==dict(read_start=1,read_end=4,row_is_exact=False)
        assert s.get(IntakeBatch,key).projected_offset==0
    assert not projection.project_step(engine,root,now=101)
    projection.retry_projection(engine,key)
    assert projection.project_step(engine,root,now=101)
    assert projection.projection_status(engine)['blocked_batches']==0


def test_partial_retry_does_not_resolve_a_failure_it_has_not_reached(isolated,monkeypatch):
    from types import SimpleNamespace
    engine,root=isolated;stamp='20261104120000'
    key=archive(engine,root,[line(stamp,i,'A preserved source title') for i in range(1,4)])
    original=projection._upsert;calls=[]
    def failed(*args,**kwargs):
        calls.append(1)
        if len(calls)==2:raise ValueError('second source row fails')
        return original(*args,**kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(projection,'_upsert',failed)
        with pytest.raises(ValueError):projection.project_step(engine,root,limit=3,time_budget_ms=60000)
    projection.retry_projection(engine,key)
    ticks=iter([0.,1.])
    with monkeypatch.context() as scoped:
        scoped.setattr(projection,'time',SimpleNamespace(monotonic=lambda:next(ticks)))
        assert projection.project_step(engine,root,limit=3,time_budget_ms=1)
    with Session(engine) as s:
        assert s.get(IntakeBatch,key).projected_offset==1
        failure=s.get(projection.IntakeProjectionFailure,key)
        assert failure.state=='blocked' and failure.row_index==2 and not failure.resolved_at
    assert projection.project_step(engine,root,limit=3,time_budget_ms=60000)
    with Session(engine) as s:
        assert s.get(IntakeBatch,key).projected_offset==3
        assert s.get(projection.IntakeProjectionFailure,key).state=='resolved'
    assert projection.projection_status(engine)['projected']==3


def test_chunk_uses_one_narrow_receipt_query_and_replay_does_not_recount(isolated):
    engine,root=isolated;stamp='20261104120000'
    # Initialize the global meter through a real write. Its one-time indexed
    # history-existence probe is separate from steady-state chunk lookup.
    projection.import_document(engine,dict(provider='fixture',record_id='meter-warmup',title='Warmup source'))
    key=archive(engine,root,[line(stamp,i,'A preserved source title') for i in range(1,26)]+['malformed source row\n'])
    receipt_reads=[]
    def record(_connection,_cursor,statement,_parameters,context,_executemany):
        sql=statement.lower().lstrip()
        if sql.startswith('select') and 'from intakeprojectionreceipt' in sql:
            receipt_reads.append((sql,context.execution_options.get('n2d_query_name')))
    sql_event.listen(engine,'before_cursor_execute',record)
    try:
        assert projection.project_step(engine,root,limit=26,time_budget_ms=60000)
    finally:sql_event.remove(engine,'before_cursor_execute',record)
    assert len(receipt_reads)==1
    sql,name=receipt_reads[0]
    assert name=='intake.projection.receipts.chunk'
    assert sql.split('from',1)[0].strip().replace('"','')=='select intakeprojectionreceipt.key'
    assert ' in (' in sql and 'count(' not in sql
    first=projection.projection_status(engine)
    assert first['projected']==25 and first['skipped']==1
    with Session(engine) as s:
        article_count=s.exec(select(func.count()).select_from(Article)).one()
        version_count=s.exec(select(func.count()).select_from(ArticleVersion)).one()
        row=s.get(IntakeBatch,key);row.projected_offset=0;s.add(row);s.commit()
    receipt_reads.clear();sql_event.listen(engine,'before_cursor_execute',record)
    try:
        assert projection.project_step(engine,root,limit=26,time_budget_ms=60000)
    finally:sql_event.remove(engine,'before_cursor_execute',record)
    assert len(receipt_reads)==1 and receipt_reads[0][1]=='intake.projection.receipts.chunk'
    after=projection.projection_status(engine)
    assert after['projected']==25 and after['skipped']==1 and after['processed_rows']==26
    assert next(row for row in after['per_feed'] if row['feed']=='gkg_english')['last_chunk']['replayed']==26
    with Session(engine) as s:
        assert s.exec(select(func.count()).select_from(Article)).one()==article_count
        assert s.exec(select(func.count()).select_from(ArticleVersion)).one()==version_count


@pytest.mark.parametrize('state,expected',[('54000',True),('22001',True),('23502',True),('23514',True),
    ('23505',False),('08006',False),('55P03',False),('40P01',False),('40001',False),('42P01',False),('42501',False)])
def test_only_whitelisted_data_errors_create_batch_holds(state,expected):
    class DriverError(Exception):sqlstate=state
    assert projection._isolatable(OperationalError('do not expose SQL',{},DriverError())) is expected


def test_actual_postgres_long_url_rolls_back_and_yields_instead_of_pin(postgres_url,tmp_path,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_GKG_AUTO_EVENTS','0')
    engine=make_engine(postgres_url);init_db(engine);root=tmp_path/'runs'
    stamp='20261104120000';rng=random.Random(46)
    suffix=''.join(rng.choices(string.ascii_letters+string.digits,k=6500))
    try:
        bad=archive(engine,root,[line(stamp,1,'First good row'),line(stamp,2,'Long locator',url='https://media.example/'+suffix)])
        good=archive(engine,root,[line('20261104121500',3,'Another batch keeps going')],stamp='20261104121500')
        with pytest.raises(Exception) as failed:
            projection.project_step(engine,root,limit=2,time_budget_ms=60000,now=100)
        assert getattr(getattr(failed.value,'orig',None),'sqlstate',None)=='54000'
        with Session(engine) as s:
            error=s.get(projection.IntakeProjectionFailure,bad)
            assert error and error.row_index==2 and error.phase=='sql_row'
            assert s.get(IntakeBatch,bad).projected_offset==0
            assert s.exec(select(func.count()).select_from(Article)).one()==0
        assert projection.project_step(engine,root,now=101)
        with Session(engine) as s:
            assert s.get(IntakeBatch,good).projected_offset==1
            assert s.get(IntakeBatch,bad).projected_offset==0
            assert s.exec(select(func.count()).select_from(ArticleVersion)).one()==1
        data=projection.projection_status(engine)
        assert data['waiting']==2 and data['blocked_batches']==1 and data['projected']==1
    finally:engine.dispose()
