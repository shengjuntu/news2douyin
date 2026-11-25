import json
from pathlib import Path
from zipfile import ZipFile
import pytest
from sqlmodel import Session, select
from sqlalchemy import func,event
from sqlalchemy.exc import OperationalError
from news2douyin.storage.db import make_engine,init_db
from news2douyin.storage.models import Article,ArticleVersion,Event
from news2douyin.extraction.models import VersionReading
from news2douyin.intake.models import IntakeBatch
from news2douyin.intake.lake import Lake
from news2douyin.intake.projection import (IntakeProjectionReceipt,IntakeDocument,
    project_step,projection_status,lookup_receipt,import_document)


@pytest.fixture
def setup(tmp_path,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_GKG_STREAM_ENABLED','0')
    # This suite verifies the retained legacy per-document event pipeline.
    # Default discovery-only behavior has a separate explicit regression.
    monkeypatch.setenv('NEWS2DOUYIN_GKG_AUTO_EVENTS','1')
    engine=make_engine('sqlite:///'+str(tmp_path/'news.db'));init_db(engine)
    yield engine,tmp_path/'runs'
    engine.dispose()


def line(stamp,index,title=None,url=None):
    columns=['']*27
    columns[0]=stamp+'-'+str(index);columns[1]=stamp;columns[2]='1';columns[3]='media.example'
    columns[4]=url or 'https://media.example/article/'+str(index)
    columns[7]='DIPLOMACY';columns[11]='Alice';columns[13]='Example Organization'
    columns[26]='<PAGE_TITLE>'+title+'</PAGE_TITLE>' if title is not None else ''
    return '\t'.join(columns)+'\n'


def archive(engine,storage,lines,stamp='20261104120000',feed='gkg_english'):
    folder=Path(storage);folder.mkdir(parents=True,exist_ok=True);path=folder/(feed+stamp+'.zip')
    member=stamp+('.translation' if feed=='gkg_translation' else '')+'.gkg.csv'
    with ZipFile(path,'w') as z:z.writestr(member,''.join(lines))
    details=Lake(storage).ingest_archive(feed,stamp,path)
    with Session(engine) as s:
        s.add(IntakeBatch(key=feed+'_'+stamp,feed=feed,stamp=stamp,state='archived',
            rows=details['rows'],valid_rows=details['valid_rows'],invalid_rows=details['invalid_rows']))
        s.commit()
    return feed+'_'+stamp


def test_full_projection_keeps_more_than_250_records_and_untitled_metadata(setup):
    engine,storage=setup;stamp='20261104120000'
    batch=archive(engine,storage,[line(stamp,n,'Alice met Bob yesterday.' if n!=17 else None) for n in range(300)]+['malformed raw row\n'])
    count=0
    while project_step(engine,storage,limit=80,time_budget_ms=60000):count+=1
    assert count==4
    status=projection_status(engine)
    expected=dict(projected=300,skipped=1,waiting=0,unresolved_batches=0,last_error='',external_imported=0)
    assert {name:status[name] for name in expected}==expected
    assert status['counts_known'] and status['processed_rows']==301
    with Session(engine) as s:
        assert s.exec(select(func.count()).select_from(Article)).one()==300
        assert s.exec(select(func.count()).select_from(Event)).one()==299
        assert s.exec(select(func.count()).select_from(ArticleVersion)).one()==300
        blank=s.exec(select(Article).where(Article.url=='https://media.example/article/17')).one()
        assert blank.title=='' and blank.content==''
        version=s.exec(select(ArticleVersion).where(ArticleVersion.article_key==blank.article_key)).one()
        assert s.get(VersionReading,version.id).state=='metadata_only'
        assert lookup_receipt(s,'gkg_english',stamp,301)['status']=='skipped'
        assert s.get(IntakeBatch,batch).projected_offset==301


def test_rewound_projection_reuses_receipts_and_does_not_duplicate_versions(setup):
    engine,storage=setup;stamp='20261104120000'
    batch=archive(engine,storage,[line(stamp,1,'Original'),line(stamp,2,'Original',url='https://media.example/article/1')])
    assert project_step(engine,storage)
    with Session(engine) as s:
        assert s.exec(select(func.count()).select_from(Article)).one()==1
        assert s.exec(select(func.count()).select_from(ArticleVersion)).one()==1
        row=s.get(IntakeBatch,batch);row.projected_offset=0;s.add(row);s.commit()
    assert project_step(engine,storage)
    with Session(engine) as s:
        assert s.exec(select(func.count()).select_from(ArticleVersion)).one()==1
        assert s.exec(select(func.count()).select_from(IntakeProjectionReceipt)).one()==2


def test_discovery_preserves_existing_full_article_and_older_headline(setup):
    engine,storage=setup;stamp='20261104120000'
    payload=dict(provider='industry_site',record_id='post-1',uri='https://media.example/article/1',
        title='Verified source title',content='An original full article body.',published_at='2026-11-01T00:00:00Z')
    first=import_document(engine,payload)
    assert import_document(engine,payload)['reused']
    archive(engine,storage,[line(stamp,1,'GKG headline must not replace full article')])
    assert project_step(engine,storage)
    with Session(engine) as s:
        a=s.exec(select(Article)).one()
        assert a.article_key==first['article_key'] and a.content==payload['content']
        assert a.title==payload['title'] and a.provider=='industry_site'
        assert s.exec(select(func.count()).select_from(ArticleVersion)).one()==1
    later='20261104123000'
    archive(engine,storage,[line(later,2,'Newest headline')],stamp=later)
    assert project_step(engine,storage)
    earlier='20261104121500'
    archive(engine,storage,[line(earlier,2,'Older delayed headline')],stamp=earlier,feed='gkg_translation')
    assert project_step(engine,storage)
    with Session(engine) as s:
        a=s.exec(select(Article).where(Article.url=='https://media.example/article/2')).one()
        assert a.title=='Newest headline'


def test_external_connector_versions_and_checksum_guard(setup):
    engine,_=setup
    payload=dict(provider='industry_site',record_id='local-7',title='One title',content='One body',
        metadata={'source_app':'industry-monitor'})
    original=import_document(engine,payload)
    assert import_document(engine,payload)['reused']
    changed=import_document(engine,dict(payload,title='Revised title',content='Revised body'))
    assert changed['article_key']==original['article_key'] and changed['revision']==2
    with Session(engine) as s:
        versions=s.exec(select(ArticleVersion).order_by(ArticleVersion.revision)).all()
        assert json.loads(versions[0].payload_json)['content']=='One body'
        assert json.loads(versions[1].payload_json)['content']=='Revised body'
    with pytest.raises(ValueError):IntakeDocument(**dict(payload,content_sha256='wrong'))
    with pytest.raises(ValueError):IntakeDocument(**dict(payload,uri='file:///etc/passwd'))


def test_metadata_observations_survive_without_artificial_body_revisions(setup):
    engine,_=setup
    first=dict(provider='industry_site',record_id='record',uri='https://media.example/a',
        title='Actual title',content='Actual immutable body',metadata={'edition':'one','source':'publisher'})
    one=import_document(engine,first)
    assert import_document(engine,dict(first,metadata={'source':'publisher','edition':'one'}))['reused']
    two=import_document(engine,dict(first,metadata={'edition':'two'}))
    assert one['article_key']==two['article_key'] and one['revision']==two['revision']==1
    assert 'envelope_json' not in one and 'envelope_json' not in two
    with Session(engine) as s:
        old=s.get(IntakeProjectionReceipt,one['key']);new=s.get(IntakeProjectionReceipt,two['key'])
        assert json.loads(old.envelope_json)['metadata']=={'edition':'one','source':'publisher'}
        assert json.loads(new.envelope_json)['metadata']=={'edition':'two'}
        assert 'content' not in json.loads(old.envelope_json)
        assert s.exec(select(func.count()).select_from(ArticleVersion)).one()==1


def test_non_web_reference_remains_in_archive_with_explicit_skipped_receipt(setup):
    engine,storage=setup;stamp='20261104120000'
    archive(engine,storage,[line(stamp,1,'Citation record',url='citation:some-source')])
    assert project_step(engine,storage)
    assert projection_status(engine)['skipped']==1
    assert Lake(storage).search()['total']==1
    with Session(engine) as s:assert not s.exec(select(Article)).all()


def test_time_budget_acknowledges_a_prefix_and_replays_every_remaining_row(setup,monkeypatch):
    from types import SimpleNamespace
    from news2douyin.intake import projection
    engine,storage=setup;stamp='20261104120000'
    key=archive(engine,storage,[line(stamp,n,'Actual headline') for n in range(5)])
    ticks=iter([0.0,1.0])
    with monkeypatch.context() as scoped:
        scoped.setattr(projection,'time',SimpleNamespace(monotonic=lambda:next(ticks)))
        assert project_step(engine,storage,limit=5,time_budget_ms=1)
    with Session(engine) as s:
        assert s.get(IntakeBatch,key).projected_offset==1
        assert s.exec(select(func.count()).select_from(IntakeProjectionReceipt)).one()==1
    while project_step(engine,storage,limit=2,time_budget_ms=60000):pass
    assert projection_status(engine)['projected']==5
    assert projection_status(engine)['waiting']==0


def test_observation_only_import_does_not_update_article_or_create_body_revision(setup):
    engine,_=setup
    payload=dict(provider='industry_site',record_id='same',uri='https://media.example/a',
        title='Stable headline',content='Stable full article.',observed_at='2026-11-01T00:00:00Z',metadata={'edition':1})
    one=import_document(engine,payload);updates=[]
    def trace(conn,cursor,statement,parameters,context,executemany):
        if statement.lower().lstrip().startswith('update article '):updates.append(statement)
    event.listen(engine,'before_cursor_execute',trace)
    try:
        two=import_document(engine,dict(payload,observed_at='2026-11-02T00:00:00Z',metadata={'edition':2}))
    finally:event.remove(engine,'before_cursor_execute',trace)
    assert one['article_key']==two['article_key'] and one['revision']==two['revision']==1
    assert not updates
    with Session(engine) as s:
        article=s.exec(select(Article)).one()
        assert article.fetched_at==payload['observed_at']
        assert s.exec(select(func.count()).select_from(IntakeProjectionReceipt)).one()==2


def test_projection_failure_rolls_back_receipts_cursor_and_articles_together(setup,monkeypatch):
    from news2douyin.intake import projection
    engine,storage=setup;stamp='20261104120000'
    key=archive(engine,storage,[line(stamp,n,'Actual headline') for n in range(3)])
    original=projection._upsert;calls=[]
    def fail_second(*args,**kwargs):
        calls.append(1)
        if len(calls)==2:raise RuntimeError('simulated_projection_failure')
        return original(*args,**kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(projection,'_upsert',fail_second)
        with pytest.raises(RuntimeError):project_step(engine,storage,limit=3,time_budget_ms=60000)
    with Session(engine) as s:
        assert s.get(IntakeBatch,key).projected_offset==0
        assert s.exec(select(func.count()).select_from(IntakeProjectionReceipt)).one()==0
        assert s.exec(select(func.count()).select_from(Article)).one()==0
    # A failed chunk is held for review/backoff; retry releases only that hold.
    assert not project_step(engine,storage,limit=3,time_budget_ms=60000)
    projection.retry_projection(engine,key)
    assert project_step(engine,storage,limit=3,time_budget_ms=60000)
    assert projection_status(engine)['projected']==3


def test_projection_loop_keeps_errors_in_memory_and_backs_off_without_status_writes(monkeypatch):
    from news2douyin.intake import projection
    class Stop:
        def __init__(self):self.delays=[]
        def is_set(self):return len(self.delays)>=3
        def wait(self,delay):self.delays.append(delay)
    worker=projection.ProjectionWorker(None,None);worker.stop_event=Stop()
    def failed():raise OperationalError('sensitive SQL',{},Exception('password=secret'))
    def forbidden(*args,**kwargs):raise AssertionError('failure reporting opened a SQL session')
    monkeypatch.setattr(worker,'step',failed);monkeypatch.setattr(projection,'Session',forbidden)
    worker.loop()
    assert worker.stop_event.delays==[1,2,4]
    assert worker.last_error=='OperationalError' and worker.failures==3


def test_projection_work_interval_accepts_configured_short_delay(monkeypatch):
    from news2douyin.intake.projection import ProjectionWorker
    class Stop:
        def __init__(self):self.delays=[]
        def is_set(self):return bool(self.delays)
        def wait(self,delay):self.delays.append(delay)
    worker=ProjectionWorker(None,None);worker.stop_event=Stop()
    monkeypatch.delenv('NEWS2DOUYIN_GDELT_PROJECTION_INTERVAL',raising=False)
    monkeypatch.setenv('NEWS2DOUYIN_GKG_PROJECTION_INTERVAL','0.01')
    monkeypatch.setattr(worker,'step',lambda:True)
    worker.loop()
    assert worker.stop_event.delays==[.01]


def test_projection_interval_defaults_aliases_and_invalid_values(monkeypatch):
    from types import SimpleNamespace
    from news2douyin.intake.projection import projection_interval
    pg=SimpleNamespace(dialect=SimpleNamespace(name='postgresql'))
    sqlite=SimpleNamespace(dialect=SimpleNamespace(name='sqlite'))
    monkeypatch.delenv('NEWS2DOUYIN_GDELT_PROJECTION_INTERVAL',raising=False)
    monkeypatch.delenv('NEWS2DOUYIN_GKG_PROJECTION_INTERVAL',raising=False)
    assert projection_interval(pg)==.05
    assert projection_interval(sqlite)==1
    monkeypatch.setenv('NEWS2DOUYIN_GKG_PROJECTION_INTERVAL','1')
    assert projection_interval(pg)==1  # Existing explicit production settings remain.
    monkeypatch.setenv('NEWS2DOUYIN_GDELT_PROJECTION_INTERVAL','0.05')
    assert projection_interval(pg)==projection_interval(sqlite)==.05
    monkeypatch.setenv('NEWS2DOUYIN_GDELT_PROJECTION_INTERVAL',' ')
    assert projection_interval(pg)==1
    monkeypatch.setenv('NEWS2DOUYIN_GDELT_PROJECTION_INTERVAL','0.001')
    assert projection_interval(pg)==.01
    for value in ('NaN','inf','invalid'):
        monkeypatch.setenv('NEWS2DOUYIN_GDELT_PROJECTION_INTERVAL',value)
        assert projection_interval(pg)==.05
        assert projection_interval(sqlite)==1


def test_short_busy_delay_does_not_change_idle_or_failure_backoff(monkeypatch):
    from news2douyin.intake.projection import ProjectionWorker
    class Stop:
        def __init__(self):self.delays=[]
        def is_set(self):return len(self.delays)>=2
        def wait(self,delay):self.delays.append(delay)
    worker=ProjectionWorker(None,None);worker.stop_event=Stop()
    outcomes=iter([True,False])
    monkeypatch.setenv('NEWS2DOUYIN_GDELT_PROJECTION_INTERVAL','0.05')
    monkeypatch.delenv('NEWS2DOUYIN_GKG_PROJECTION_IDLE_INTERVAL',raising=False)
    monkeypatch.setattr(worker,'step',lambda:next(outcomes))
    worker.loop()
    assert worker.stop_event.delays==[.05,3]


def test_recent_gkg_gets_three_turns_then_historical_repair_can_advance(setup):
    from datetime import datetime, timezone

    engine, storage = setup
    now = datetime(2026, 11, 24, tzinfo=timezone.utc).timestamp()
    old, recent = '20220223043000', '20261123120000'
    old_key = archive(engine, storage, [line(old, i) for i in range(2)], stamp=old)
    recent_key = archive(engine, storage, [line(recent, i) for i in range(10, 15)], stamp=recent)
    progress = []
    for _ in range(5):
        assert project_step(engine, storage, limit=1, now=now, observer=progress.append)
    assert [item['key'] for item in progress] == [recent_key] * 3 + [old_key, recent_key]
    with Session(engine) as session:
        assert session.get(IntakeBatch, old_key).projected_offset == 1
        assert session.get(IntakeBatch, recent_key).projected_offset == 4
