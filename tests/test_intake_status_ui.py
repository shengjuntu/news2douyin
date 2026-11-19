"""Read-only intake polling and explicit, source-scoped operator actions."""
from pathlib import Path

import pytest
from jinja2 import Environment,FileSystemLoader,select_autoescape
from sqlalchemy import event as sql_event
from sqlmodel import Session

from test_intake_api import api
from news2douyin.intake import projection,service
from news2douyin.intake.models import FeedState
from sql_readonly_audit import is_read_only_execution


def test_intake_status_poll_does_not_count_article_versions_or_open_the_lake(api,monkeypatch):
    client,engine,_=api
    def forbidden(*args,**kwargs):
        raise AssertionError('A status GET tried to access an archive/network')
    monkeypatch.setattr(service.requests,'get',forbidden)
    from news2douyin.server import intake
    monkeypatch.setattr(intake.Lake,'search',forbidden)
    monkeypatch.setattr(intake.Lake,'rows',forbidden)
    executions=[]
    def audit(_connection,_cursor,statement,_parameters,context,_executemany):
        executions.append((statement,is_read_only_execution(statement,context)))
    sql_event.listen(engine,'before_cursor_execute',audit)
    try:
        for path in ('/intake','/api/intake/status'):
            response=client.get(path)
            assert response.status_code==200,response.text
        status=client.get('/api/intake/status')
        assert status.headers['cache-control']=='no-store'
        assert status.json()['extraction']=={} and status.json()['extraction_known'] is False
        assert executions and all(read for _,read in executions)
        assert not any('versionreading' in sql.casefold() for sql,_ in executions)
        ledger=[sql.casefold() for sql,_ in executions if 'intakeprojectionreceipt' in sql.casefold()]
        # An indexed LIMIT-one existence check can distinguish an untouched
        # database from legacy history without counting the history itself.
        assert all('limit' in sql and 'count(' not in sql.replace(' ','') and 'group by' not in sql for sql in ledger)
    finally:sql_event.remove(engine,'before_cursor_execute',audit)


def test_probe_is_an_explicit_same_origin_action_without_changing_feed_cursor(api,monkeypatch):
    client,engine,_=api
    with Session(engine) as session:
        row=session.get(FeedState,'events_english');row.next_batch='20261114101500'
        session.add(row);session.commit()
    calls=[]
    def probe(_engine,feed):
        calls.append(feed)
        return dict(feed=feed,target_stamp='20261114101500',cursor_unchanged=True,message='Fixture only')
    monkeypatch.setattr(service,'probe',probe)
    path='/api/intake/feeds/events_english/probe'
    assert client.get(path).status_code==405 and calls==[]
    assert client.post(path,json={},headers={'Origin':'https://foreign.example'}).status_code==403
    assert calls==[]
    response=client.post(path,json={})
    assert response.status_code==200 and response.json()['cursor_unchanged']
    assert calls==['events_english']
    assert client.post('/api/intake/feeds/unknown/probe',json={}).status_code==404
    with Session(engine) as session:assert session.get(FeedState,'events_english').next_batch=='20261114101500'


def test_projection_retry_route_uses_same_origin_and_the_batch_service(api,monkeypatch):
    client,engine,_=api
    calls=[]
    def retry(_engine,key):calls.append(key);return dict(queued=True,key=key)
    monkeypatch.setattr(projection,'retry_projection',retry)
    key='events_english_20261114101500'
    path='/api/intake/projection/'+key+'/retry'
    assert client.post(path,json={},headers={'Origin':'https://foreign.example'}).status_code==403
    assert calls==[]
    assert client.post(path,json={}).json()==dict(queued=True,key=key)
    assert calls==[key]


def test_intake_page_explains_offsets_unknown_history_and_explicit_archive_search():
    template_root=Path(__file__).parents[1]/'src/news2douyin/server/templates'
    environment=Environment(loader=FileSystemLoader(template_root),autoescape=select_autoescape(['html']))
    page=environment.get_template('intake.html').render(active='intake',feed_labels={'events_english':'Events English'})
    assert '待处理和失败分开统计' in page
    assert '包含成功投影与保留原档的跳过记录' in page
    assert '重试只解除等待' in page
    assert '尚未查询，页面刷新只读取接收进度' in page
    assert '/knowledge' in page and 'id="intake-failures-section"' in page
