from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select
from sqlalchemy import event

from news2douyin.digest import backfill
from news2douyin.digest.models import DailyDigestIssue
from news2douyin.intake.models import DailyRepairJob, DailyRepairPart, FeedState, IntakeBatch
from news2douyin.server.app import create_app
from test_postgres_integration import postgres_url

DAY = date(2025, 3, 16)
URL = '/admin/daily-radar'
API = '/api/admin/daily-radar'
STAMP = '20250315160000' # midnight Beijing


@pytest.fixture(params=['sqlite', 'postgres'])
def instance(tmp_path, monkeypatch, request):
    monkeypatch.setenv('NEWS2DOUYIN_GDELT_STREAM_ENABLED','1')
    monkeypatch.setenv('NEWS2DOUYIN_DIGEST_ENABLED','1')
    url = request.getfixturevalue('postgres_url') if request.param=='postgres' else 'sqlite:///'+str(tmp_path/'local.db')
    app = create_app(db_url=url,storage_root=str(tmp_path/'archive'))
    with Session(app.state.engine) as session:
        for feed in session.exec(select(FeedState)):
            feed.enabled = feed.key=='gkg_english'
            session.add(feed)
        session.commit()
    # Start no network or workers. These pages only submit durable tasks.
    client = TestClient(app)
    yield client,app.state.engine
    client.close()
    app.state.engine.dispose()


def test_coverage_and_week_view_read_no_issue_bodies_and_never_queue(instance):
    client,engine=instance
    with Session(engine) as s:
        s.add(IntakeBatch(key='test',feed='gkg_english',stamp=STAMP,state='archived',rows=4))
        s.add(DailyDigestIssue(day=DAY.isoformat(),status='ready',generated_at='2025-03-17T00:00:00Z',
                               items_json='invalid JSON: management must not open cards',stats_json='{"selected":3}'))
        s.commit()
    selects=[]
    def capture(_conn,_cursor,sql,_params,_ctx,_many):
        selects.append(sql)
    event.listen(engine,'before_cursor_execute',capture)
    try:
        response=client.get(API,params={'day':DAY.isoformat()})
        html=client.get(URL,params={'day':DAY.isoformat()})
    finally:
        event.remove(engine,'before_cursor_execute',capture)
    assert response.status_code==html.status_code==200
    data=response.json()
    assert (data['detail']['stored'],data['detail']['missing'],data['detail']['expected'])==(1,95,96)
    assert data['detail']['issue']['selected']==3 and data['jobs']==[]
    assert len(data['days'])==7 and data['days'][0]['day']==str(DAY)
    assert '03-16 00:15' in html.text and '按已有数据重算' in html.text and '补缺并重算' in html.text
    assert '第 1 版' in html.text and '前七天' in html.text
    assert not any('items_json' in sql.lower() for sql in selects)
    assert not any(sql.lstrip().upper().startswith(('INSERT','UPDATE','DELETE')) for sql in selects)
    assert client.get(API,params={'end':str(DAY)}).json()['selected']==str(DAY)
    assert client.get(API,params={'day':'2025-03-20','end':'2025-03-19'}).status_code==422
    assert client.get(URL,params={'day':'9999-12-31'}).status_code==422


def test_forms_enqueue_once_show_progress_and_reject_cross_origin(instance):
    client,engine=instance
    request=URL+'/'+str(DAY)+'/backfill'
    assert client.post(request,headers={'origin':'https://other.example'}).status_code==403
    response=client.post(request,follow_redirects=False)
    assert response.status_code==303 and response.headers['location'].endswith('#day-detail')
    again=client.post(API+'/'+str(DAY)+'/backfill')
    assert again.status_code==202
    with Session(engine) as s:
        jobs=list(s.exec(select(DailyRepairJob)))
        assert len(jobs)==1 and jobs[0].id==again.json()['job']['id']
    html=client.get(response.headers['location']).text
    assert '已排队' in html and '最近补采任务' in html
    assert 'data-running="1"' in html
    assert client.get('/research-assets/daily-management.css').status_code==200
    assert client.get('/research-assets/daily-management.js').status_code==200
    assert client.post(API+'/'+str(DAY)+'/nonsense').status_code==404


def test_partial_recovery_is_explicit_and_keeps_old_edition(instance):
    client,engine=instance
    with Session(engine) as s:
        s.add(DailyRepairJob(day=str(DAY),id='repair-01',status='partial',feeds_json='["gkg_english"]',
                             last_error='一个批次 HTTP 404'))
        s.add(DailyRepairPart(key='failed',day=str(DAY),feed='gkg_english',stamp='20250315161500',
                              state='failed',error='HTTP 404 <script>broken</script>'))
        s.add(DailyRepairPart(key='good',day=str(DAY),feed='gkg_english',stamp=STAMP,state='done',
                              completed_at='2026-01-01T00:00:00Z'))
        s.add(DailyDigestIssue(day=str(DAY),status='ready',generated_at='2025-03-17T00:00:00Z',
                               stats_json='{"selected":3}'))
        s.commit()
    page=client.get(URL,params={'day':str(DAY)})
    assert page.status_code==200
    assert '重试补采' in page.text and '用已补齐的批次重算' in page.text
    assert 'HTTP 404' in page.text and '<script>broken</script>' not in page.text
    response=client.post(API+'/'+str(DAY)+'/publish-partial')
    assert response.status_code==202 and response.json()['job']['status']=='submit_digest'
    with Session(engine) as s:
        assert s.get(DailyDigestIssue,str(DAY)).generated_at=='2025-03-17T00:00:00Z'


def test_date_picker_and_disabled_receiver(instance, monkeypatch):
    client,engine=instance
    before=client.get(API).json()
    today=before['today']
    page=client.get(URL,params={'day':today}).text
    assert '今天' in page and '用已有数据生成' in page and '补缺并重算' not in page
    assert client.post(API+'/'+today+'/backfill').status_code==422
    monkeypatch.setattr(backfill.intake,'runtime_enabled',lambda:False)
    html=client.post(URL+'/'+str(DAY)+'/backfill')
    assert html.status_code==409 and '接收器已停用' in html.text
    response=client.post(API+'/'+today+'/rebuild')
    assert response.status_code==202 and response.json()['build_status']=='pending'
