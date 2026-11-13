from pathlib import Path
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from sqlmodel import Session
from news2douyin.storage.db import make_engine,init_db
from news2douyin.intake import service
from news2douyin.intake.models import FeedState,IntakeBatch
from news2douyin.intake.projection import IntakeProjectionReceipt
from news2douyin.server import intake


ROW=dict(feed='gkg_english',stamp='20261104101500',row_index=1,record_id='20261104101500-1',
    url='javascript:alert(1)',title='<script>not executable</script>',metadata={'persons':['Example']},
    valid=True,error='',raw_line='raw\tline',document_date='20261104101000',observed_at='2026-11-04T10:15:00Z')


@pytest.fixture
def api(tmp_path,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_INTAKE_TOKEN','fixture-intake-secret')
    engine=make_engine('sqlite:///'+str(tmp_path/'news.db'));init_db(engine);service.initialize(engine)
    class FixtureLake:
        def __init__(self,storage):self.storage=storage
        def search(self,**kwargs):return {'items':[dict(ROW)],'total':1}
        def rows(self,feed,stamp,after=0,limit=100):return [dict(ROW)] if feed==ROW['feed'] and stamp==ROW['stamp'] and after==0 else []
    monkeypatch.setattr(intake,'Lake',FixtureLake)
    app=FastAPI();app.state.intake=SimpleNamespace(running=False,last_error='receiver_already_owned');intake.register(app,engine,tmp_path/'runs',lambda template,**ctx:HTMLResponse('全量采集接收中心 '+template))
    yield TestClient(app),engine,tmp_path
    engine.dispose()


def test_status_get_and_post_archive_search_never_change_intake_scope(api):
    client,engine,root=api
    with Session(engine) as s:
        f=s.get(FeedState,'gkg_english');f.next_batch='20261104101500';s.add(f);s.commit()
    r=client.get('/api/intake/status');assert r.status_code==200
    assert set(r.json())>={'feeds','batches','counters','projection','extraction'}
    assert r.json()['receiver']==dict(running=False,last_error='receiver_already_owned')
    assert r.json()['database']==dict(dialect='sqlite',driver='pysqlite',label='SQLite',mode='lightweight')
    assert r.json()['database_ok']
    assert not (root/'runs').exists()
    data=client.get('/api/intake/records?q=anything&feed=gkg_english').json()
    assert data['total']==1 and data['items'][0]['title'].startswith('<script>')
    assert 'raw_line' not in data['items'][0] and 'metadata' not in data['items'][0]
    with Session(engine) as s:assert s.get(FeedState,'gkg_english').next_batch=='20261104101500'
    assert client.get('/api/intake/records?feed=not-known').status_code==404
    assert client.get('/api/intake/records?limit=1000').status_code==422


def test_status_preserves_memory_errors_when_transaction_database_is_down(api,monkeypatch):
    from sqlalchemy.exc import OperationalError
    client,_,_=api
    client.app.state.intake=SimpleNamespace(running=True,last_error='OperationalError')
    client.app.state.intake_projection=SimpleNamespace(running=True,last_error='OperationalError')
    def fail(*args,**kwargs):
        raise OperationalError('postgresql://secret:private@db',{},Exception('private-password'))
    monkeypatch.setattr(service,'status',fail)
    response=client.get('/api/intake/status')
    assert response.status_code==200
    data=response.json()
    assert not data['database_ok'] and data['counters']=={} and data['feeds']==[]
    assert data['receiver']==dict(running=True,last_error='OperationalError')
    assert data['projection']==dict(running=True,last_error='OperationalError')
    assert 'secret' not in response.text and 'private' not in response.text


def test_status_displays_projection_memory_error_without_a_persistent_error_row(api):
    client,_,_=api
    client.app.state.intake_projection=SimpleNamespace(running=True,last_error='OperationalError')
    data=client.get('/api/intake/status').json()
    assert data['database_ok'] and data['projection']['running']
    assert data['projection']['last_error']=='OperationalError'


def test_non_owner_can_read_sql_status_but_never_open_the_duckdb_lake(api,monkeypatch):
    client,_,_=api
    client.app.state.intake=SimpleNamespace(archive=SimpleNamespace(running=False,status_error='另一个接收进程正在使用此采集目录'))
    def forbidden(*args,**kwargs):raise AssertionError('non-owner opened the lake')
    monkeypatch.setattr(intake.Lake,'search',forbidden);monkeypatch.setattr(intake.Lake,'rows',forbidden)
    status=client.get('/api/intake/status')
    assert status.status_code==200 and not status.json()['receiver']['running']
    assert status.json()['receiver']['last_error']=='另一个接收进程正在使用此采集目录'
    assert client.get('/api/intake/records').status_code==503
    assert client.get('/api/intake/records/gkg_english/20261104101500/1').status_code==503


def test_raw_detail_keeps_origin_and_version_receipt(api):
    client,engine,_=api
    from news2douyin.intake.projection import _receipt_key
    with Session(engine) as s:
        s.add(IntakeProjectionReceipt(key=_receipt_key(ROW['feed'],ROW['stamp'],1),feed=ROW['feed'],stamp=ROW['stamp'],row_index=1,status='projected',article_key='a',revision=3));s.commit()
    result=client.get('/api/intake/records/gkg_english/20261104101500/1')
    assert result.status_code==200
    doc=result.json();assert doc['raw_line']=='raw\tline' and doc['projection']['revision']==3
    assert not doc['content_available'] and not doc['raw_line_truncated']
    assert client.get('/api/intake/records/gkg_english/20261104101500/2').status_code==404
    assert client.get('/api/intake/records/gkg_english/invalid/1').status_code==422


def test_pause_retry_preserve_cursor_and_guard_origin(api):
    client,engine,_=api
    with Session(engine) as s:
        row=s.get(FeedState,'gkg_english');row.next_batch='20261104101500';row.last_error='timeout';row.retry_after=9999999999;s.add(row);s.commit()
    result=client.post('/api/intake/feeds/gkg_english/enabled',json={'enabled':False});assert result.status_code==200
    with Session(engine) as s:
        row=s.get(FeedState,'gkg_english');assert not row.enabled and row.next_batch=='20261104101500'
    assert client.post('/api/intake/feeds/gkg_english/retry',json={},headers={'Origin':'https://evil.example'}).status_code==403
    assert client.post('/api/intake/feeds/gkg_english/retry',json={}).status_code==200
    with Session(engine) as s:
        row=s.get(FeedState,'gkg_english');assert row.enabled and row.retry_after==0 and row.next_batch=='20261104101500'
    assert client.post('/api/intake/feeds/fake/enabled',json={'enabled':True}).status_code==404


def test_initial_start_is_explicit_utc_and_cannot_rewrite_completed_progress(api):
    client,engine,_=api
    path='/api/intake/feeds/gkg_translation/enabled'
    response=client.post(path,json={'enabled':True,'start_batch':'2026-11-04T10:15'})
    assert response.status_code==200
    with Session(engine) as s:
        row=s.get(FeedState,'gkg_translation');assert row.next_batch=='20261104101500';row.last_complete='20261104101500';s.add(row);s.commit()
    assert client.post(path,json={'enabled':True,'start_batch':'2026-11-04T12:00'}).status_code==422
    assert client.post(path,json={'enabled':True,'start_batch':'2026-11-04T10:17'}).status_code==422
    with Session(engine) as s:assert s.get(FeedState,'gkg_translation').next_batch=='20261104101500'


def test_other_app_document_import_is_idempotent_and_keeps_body(api,monkeypatch):
    from sqlmodel import select
    from news2douyin.storage.models import Article,ArticleVersion
    client,engine,_=api
    payload=dict(provider='industry_feed',record_id='source-17',uri='https://industry.example/news/17',
        title='Industry report',content='A preserved source paragraph.',published_at='2026-11-04T10:00:00Z')
    headers={'Authorization':'Bearer fixture-intake-secret'}
    assert client.post('/api/intake/documents',json=payload).status_code==401
    assert client.post('/api/intake/documents',json=payload,headers={'Authorization':'Bearer wrong'}).status_code==401
    first=client.post('/api/intake/documents',json=payload,headers=headers)
    assert first.status_code==200 and not first.json()['reused']
    again=client.post('/api/intake/documents',json=payload,headers=headers)
    assert again.status_code==200 and again.json()['reused']
    assert again.json()['article_key']==first.json()['article_key']
    with Session(engine) as s:
        assert s.exec(select(Article)).one().content=='A preserved source paragraph.'
        assert len(s.exec(select(ArticleVersion)).all())==1
    assert client.post('/api/intake/documents',json=payload,headers=dict(headers,Origin='https://evil.example')).status_code==403
    assert client.post('/api/intake/documents',json=dict(payload,content_sha256='wrong'),headers=headers).status_code==422
    for reserved in ('gkg','GDELT','worldnewsapi','mock'):
        assert client.post('/api/intake/documents',json=dict(payload,provider=reserved),headers=headers).status_code==422
    changed=client.post('/api/intake/documents',json=dict(payload,content='A corrected source paragraph.'),headers=headers)
    assert changed.status_code==200 and changed.json()['revision']==2
    monkeypatch.delenv('NEWS2DOUYIN_INTAKE_TOKEN')
    unavailable=client.post('/api/intake/documents',json=payload,headers=headers)
    assert unavailable.status_code==503 and 'fixture-intake-secret' not in unavailable.text


@pytest.mark.parametrize('value',['2026-11-04T10:16','not-a-date','20261104101599','2026-11-04T10:15:01'])
def test_start_requires_a_valid_quarter_hour(value):
    with pytest.raises(ValueError):intake.stamp_value(value)
