"""Full-batch fixtures use real DuckDB; HTTP is deterministic and never live."""
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile
import pytest
from sqlmodel import Session,select
from sqlalchemy.exc import OperationalError
from news2douyin.intake import service
from news2douyin.intake.lake import Lake
from news2douyin.intake.models import FeedState,IntakeBatch
from news2douyin.intake.runtime import IntakeWorker

STAMP='20261104120000'
NEXT='20261104121500'


def line(number,title='Fixture report',url='https://media.example/repeated',translated=False,date=STAMP):
    cells=['']*27;cells[0]=STAMP+'-'+('T' if translated else '')+str(number);cells[1]=date;cells[2]='1';cells[3]='media.example';cells[4]=url
    cells[7]='DIPLOMACY';cells[9]='1#France#FR###';cells[11]='Alice';cells[13]='Organization'
    cells[26]='<PAGE_TITLE>'+title+'</PAGE_TITLE>' if title else ''
    return '\t'.join(cells)


def archive(rows,stamp=STAMP,translated=False):
    output=io.BytesIO()
    with ZipFile(output,'w') as z:z.writestr(stamp+('.translation' if translated else '')+'.gkg.csv','\n'.join(rows)+'\n')
    return output.getvalue()


def install_transport(monkeypatch,blob,latest=STAMP):
    def discover(feed):return dict(stamp=latest,bytes=len(blob),md5=hashlib.md5(blob).hexdigest(),url=service.url_for(feed,latest))
    def download(feed,stamp,destination,*args):
        p=Path(destination);p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(blob)
    monkeypatch.setattr(service,'discover',discover);monkeypatch.setattr(service,'download',download)


def test_complete_lake_over_250_no_title_duplicates_and_bad_rows_retained(tmp_path):
    lake=Lake(tmp_path)
    assert lake.search()=={'items':[],'total':0} and not lake.path.exists()
    rows=[line(i) for i in range(1,301)]+[line(301,title=''),line(302,url='citation:123'),'malformed\trow']
    blob=archive(rows);path=tmp_path/'fixture.zip';path.write_bytes(blob)
    result=lake.ingest_archive('gkg_english',STAMP,path,hashlib.md5(blob).hexdigest(),len(blob))
    assert (result['rows'],result['valid_rows'],result['invalid_rows'])==(303,302,1)
    material=lake.rows('gkg_english',STAMP,limit=1000)
    assert len(material)==303 and material[300]['title']==''
    assert material[301]['valid'] and not material[301]['metadata']['projection_eligible']
    assert material[302]['raw_line']=='malformed\trow' and material[302]['error']=='expected_27_columns'
    assert len({r['url'] for r in material[:300]})==1
    assert lake.rows('gkg_english',STAMP,after=302)[0]['row_index']==303
    assert lake.ingest_archive('gkg_english',STAMP,path)==result
    assert lake.search()['total']==303 and path.read_bytes()==blob


def test_translation_id_and_document_date_are_preserved_independently(tmp_path):
    blob=archive([line(1,translated=True,date='20261103110000')],translated=True)
    path=tmp_path/'translation.zip';path.write_bytes(blob)
    lake=Lake(tmp_path);lake.ingest_archive('gkg_translation',STAMP,path)
    row=lake.rows('gkg_translation',STAMP)[0]
    assert row['valid'] and row['record_id'].endswith('-T1')
    assert row['document_date']=='20261103110000' and row['stamp']==STAMP


def test_checksum_and_zip_failure_never_publish_partial_lake(tmp_path):
    blob=archive([line(1)]);path=tmp_path/'bad.zip';path.write_bytes(blob)
    lake=Lake(tmp_path)
    with pytest.raises(ValueError,match='md5'):lake.ingest_archive('gkg_english',STAMP,path,'0'*32)
    assert not lake.path.exists()
    with pytest.raises(ValueError):lake.ingest_archive('../escape',STAMP,path)
    damaged=io.BytesIO()
    with ZipFile(damaged,'w') as z:z.writestr('../'+STAMP+'.gkg.csv',line(1))
    path.write_bytes(damaged.getvalue())
    with pytest.raises(ValueError,match='member'):lake.ingest_archive('gkg_english',STAMP,path)
    assert lake.search()['total']==0


def test_two_independent_feeds_start_latest_then_replay_all_quarters(engine,tmp_path,monkeypatch):
    service.initialize(engine);blob=archive([line(1)]);install_transport(monkeypatch,blob)
    worker=IntakeWorker(engine,tmp_path)
    assert worker.step('gkg_english',now=1000)
    translation=archive([line(1,translated=True)],translated=True);install_transport(monkeypatch,translation)
    assert worker.step('gkg_translation',now=1001)
    with Session(engine) as s:
        assert s.get(FeedState,'gkg_english').last_complete==STAMP
        assert s.get(FeedState,'gkg_translation').last_complete==STAMP
    # No source keywords or 250-record cap exist on either receiver path.
    install_transport(monkeypatch,blob,latest='20261104123000')
    # Next download is the 12:15 slot; supply a valid member for that slot.
    def download(feed,stamp,destination,*args):
        path=Path(destination);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(archive([line(1)],stamp=stamp))
    monkeypatch.setattr(service,'download',download)
    assert worker.step('gkg_english',now=1100)
    with Session(engine) as s:
        assert s.get(FeedState,'gkg_english').last_complete==NEXT
        assert s.get(FeedState,'gkg_english').next_batch=='20261104123000'
        assert s.get(FeedState,'gkg_translation').next_batch==NEXT
    assert service.status(engine,tmp_path)['counters']['rows']==3


def test_429_retry_after_never_moves_checkpoint_or_allows_gap_reset(engine,tmp_path,monkeypatch):
    service.initialize(engine);install_transport(monkeypatch,archive([line(1)]))
    def limited(*args):raise service.IntakeHTTPError(429,120)
    monkeypatch.setattr(service,'download',limited)
    assert service.step(engine,tmp_path,'gkg_english',now=1000)
    with Session(engine) as s:
        feed=s.get(FeedState,'gkg_english')
        assert feed.next_batch==STAMP and feed.last_complete=='' and feed.retry_after==1120
        assert '429' in feed.last_error
    assert not service.step(engine,tmp_path,'gkg_english',now=1001)
    with pytest.raises(ValueError,match='游标'):service.configure(engine,'gkg_english',True,'20261104123000')
    service.configure(engine,'gkg_english',False,STAMP)
    assert not service.step(engine,tmp_path,'gkg_english',now=1200)
    service.retry(engine,'gkg_english');install_transport(monkeypatch,archive([line(1)]))
    assert service.step(engine,tmp_path,'gkg_english',now=1201)
    assert service.status(engine,tmp_path)['counters']['rows']==1
    with pytest.raises(service.IntakeHTTPError) as caught:service.response_error(SimpleNamespace(status_code=429,headers={'Retry-After':'140'}))
    assert caught.value.retry_after==140


def test_duck_commit_before_sql_ack_recovers_idempotently(engine,tmp_path,monkeypatch):
    service.initialize(engine);install_transport(monkeypatch,archive([line(1),line(2)]))
    original=service.next_stamp
    def crash(_):raise RuntimeError('simulated_ack_failure')
    monkeypatch.setattr(service,'next_stamp',crash)
    assert service.step(engine,tmp_path,'gkg_english',now=1000)
    assert Lake(tmp_path).search()['total']==2
    with Session(engine) as s:assert s.get(FeedState,'gkg_english').last_complete==''
    monkeypatch.setattr(service,'next_stamp',original)
    assert service.step(engine,tmp_path,'gkg_english',now=1100)
    assert Lake(tmp_path).search()['total']==2
    with Session(engine) as s:
        assert s.get(FeedState,'gkg_english').last_complete==STAMP
        assert s.get(IntakeBatch,'gkg_english_'+STAMP).state=='archived'


def test_discovery_uses_only_matching_trusted_gkg_manifest(monkeypatch):
    body=('2 '+'a'*32+' http://data.gdeltproject.org/gdeltv2/'+STAMP+'.gkg.csv.zip\n'+
          '3 '+'b'*32+' https://data.gdeltproject.org/gdeltv2/'+STAMP+'.translation.gkg.csv.zip\n'+
          '4 '+'c'*32+' https://evil.example/'+NEXT+'.gkg.csv.zip\n').encode()
    class Response:
        status_code=200;headers={}
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def iter_content(self,size):yield body
    monkeypatch.setattr(service.requests,'get',lambda *a,**k:Response())
    en=service.discover('gkg_english');tr=service.discover('gkg_translation')
    assert en['bytes']==2 and tr['bytes']==3
    assert en['url'].startswith('https://data.gdeltproject.org/')


def test_process_lock_refuses_second_receiver_and_restart_clears_dead_lease(engine,tmp_path,monkeypatch):
    import os,subprocess,sys
    service.initialize(engine);monkeypatch.setenv('NEWS2DOUYIN_GKG_STREAM_ENABLED','1')
    root=Path(__file__).resolve().parents[1]
    code="import sys;from news2douyin.intake.runtime import ReceiverLock;lock=ReceiverLock(sys.argv[1]).acquire();print('held',flush=True);sys.stdin.read(1);lock.release()"
    proc=subprocess.Popen([sys.executable,'-c',code,str(tmp_path)],env=dict(os.environ,PYTHONPATH=str(root/'src')),stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    first=IntakeWorker(engine,tmp_path)
    try:
        assert proc.stdout.readline().strip()=='held'
        with Session(engine) as s:
            f=s.get(FeedState,'gkg_english');f.next_batch=STAMP;f.lease='dead-process-token';f.lease_until=10**12;s.add(f);s.commit()
        assert not first.start() and '另一个接收进程' in first.status_error
        with Session(engine) as s:assert s.get(FeedState,'gkg_english').lease=='dead-process-token'
    finally:
        proc.terminate();proc.wait(timeout=5)
    # A real process exit released the OS lock; fresh receiver recovers immediately,
    # without advancing a cursor or waiting out the former 30-minute lease.
    monkeypatch.setattr(first,'step',lambda *args,**kwargs:False)
    assert first.start()
    try:
        with Session(engine) as s:
            f=s.get(FeedState,'gkg_english');assert f.lease=='' and f.lease_until==0 and f.next_batch==STAMP
        second=IntakeWorker(engine,tmp_path)
        assert not second.start()
    finally:first.stop()
    second=IntakeWorker(engine,tmp_path);monkeypatch.setattr(second,'step',lambda *args,**kwargs:False)
    assert second.start();second.stop()


def test_initialized_or_idle_receiver_does_not_acquire_a_write_lock(engine,tmp_path,monkeypatch):
    service.initialize(engine)
    with Session(engine) as s:
        for row in s.exec(select(FeedState)):
            row.enabled=False;s.add(row)
        s.commit()
    calls=[];original=service.lock_news
    def traced(s,*resources):calls.append(resources);return original(s,*resources)
    monkeypatch.setattr(service,'lock_news',traced)
    service.initialize(engine)
    assert not service.step(engine,tmp_path,now=1000)
    assert not service.recover_leases(engine)
    assert calls==[]


def test_database_failure_does_not_open_an_error_write_and_lease_is_recoverable(engine,tmp_path,monkeypatch):
    service.initialize(engine);install_transport(monkeypatch,archive([line(1)]))
    original=service.lock_news;calls=[]
    def conflict(s,*resources):
        calls.append(resources)
        if len(calls)==2:raise OperationalError('database locked',{},Exception('sensitive detail'))
        return original(s,*resources)
    with monkeypatch.context() as scoped:
        scoped.setattr(service,'lock_news',conflict)
        with pytest.raises(OperationalError):service.step(engine,tmp_path,'gkg_english',now=1000)
    assert len(calls)==2
    assert calls[0]==('intake:feed:gkg_english',)
    assert calls[1]==('intake:feed:gkg_english','intake:batch:gkg_english_'+STAMP)
    with Session(engine) as s:
        row=s.get(FeedState,'gkg_english')
        assert row.lease and not row.last_error and not row.last_complete
    # The process owning ReceiverLock may recover this actual abandoned lease
    # after backoff. No source record or acquisition checkpoint is skipped.
    assert service.recover_leases(engine)
    assert service.step(engine,tmp_path,'gkg_english',now=1001)
    assert service.status(engine,tmp_path)['counters']['rows']==1


def test_receiver_loop_backs_off_then_recovers_owned_lease_without_logging_to_sql(monkeypatch,tmp_path):
    class Stop:
        def __init__(self):self.delays=[]
        def is_set(self):return len(self.delays)>=3
        def wait(self,delay):self.delays.append(delay)
    class Ownership:
        released=False
        def release(self):self.released=True
    worker=IntakeWorker(None,tmp_path);worker.stop_event=Stop();ownership=Ownership();worker._ownership=ownership
    steps=[];recovered=[]
    def tick():
        steps.append(1)
        if len(steps)<=2:raise OperationalError('SQL',{},Exception('password=secret'))
        return True
    monkeypatch.setattr(worker,'step',tick)
    monkeypatch.setattr(service,'recover_leases',lambda engine:recovered.append(1))
    monkeypatch.setenv('NEWS2DOUYIN_GKG_STREAM_INTERVAL','0.01')
    worker.loop()
    assert worker.stop_event.delays==[1,2,1]
    assert recovered==[1,1] and ownership.released
    assert worker.last_error=='' and worker.failures==0
