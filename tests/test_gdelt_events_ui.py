"""Source-event discovery remains read-only until explicit topic selection."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func
from sqlmodel import Session, select

from news2douyin.server.app import create_app
from news2douyin.intake.official_models import OfficialEvent, OfficialMention
from news2douyin.storage.models import Article, Event
from news2douyin.desk.models import RadarEntry
from news2douyin.workbench.models import ContentTask


@pytest.fixture
def gdelt_ui(tmp_path,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL','')
    monkeypatch.setenv('NEWS2DOUYIN_GKG_STREAM_ENABLED','0')
    app=create_app(db_url='sqlite:///'+str(tmp_path/'ui.db'),storage_root=str(tmp_path/'runs'))
    now=datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
    stamp=datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')
    with Session(app.state.engine) as s:
        s.add(OfficialEvent(event_id='123456',event_date=now[:10],added_at=now,first_seen=now,last_seen=now,
            actor1_name='<script>alert(1)</script>',actor2_name='乙国',event_code='072',event_base_code='072',
            event_root_code='07',action_country='CH',action_country_iso2='cn',action_place='某地',
            initial_mentions=100,initial_sources=50,feed='events_english',stamp=stamp,row_index=1))
        for index,domain in enumerate(('one.example','two.example'),1):
            s.add(OfficialMention(key='mention-'+str(index),event_id='123456',doc_identifier='https://'+domain+'/report',
                domain=domain,first_seen=now,last_seen=now,confidence=80,
                first_feed='mentions_english',first_stamp=stamp,first_row_index=index,
                last_feed='mentions_english',last_stamp=stamp,last_row_index=index))
        s.commit()
    yield TestClient(app),app
    app.state.engine.dispose()


def test_official_radar_has_simple_filters_escaped_sources_and_no_local_mutation(gdelt_ui):
    client,app=gdelt_ui
    response=client.get('/radar?source=gdelt&period=all')
    assert response.status_code==200,response.text
    assert '全球事件线索' in response.text and '已整理事件' in response.text
    assert 'CAMEO' not in response.text or '行为' in response.text
    assert 'data-gdelt-event="123456"' in response.text
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in response.text
    assert '<script>alert(1)</script>' not in response.text
    assert '不改变全量采集范围' in response.text
    assert '焦点、来源与保存筛选' in response.text
    assert '<details class="full gdelt-advanced" >' in response.text
    assert client.get('/api/gdelt/events?period=all').status_code==200
    detail=client.get('/api/gdelt/events/123456')
    assert detail.status_code==200 and len(detail.json()['mentions'])==2
    with Session(app.state.engine) as s:
        for model in (Article,Event,RadarEntry,ContentTask):
            assert s.exec(select(func.count()).select_from(model)).one()==0


def test_candidate_filters_validate_inputs_and_persist_without_changing_intake(gdelt_ui):
    client,app=gdelt_ui
    assert client.get('/api/gdelt/events?min_sources=-1').status_code==422
    assert client.get('/api/gdelt/events?period=forever').status_code==422
    assert client.get('/radar?source=unknown').status_code==422
    saved=client.post('/api/gdelt/filters',json={'name':'援助线索','filters':{'cameo':'07','period':'all'}})
    assert saved.status_code==200,saved.text
    listed=client.get('/api/gdelt/filters').json()['items']
    assert any(row['name']=='援助线索' for row in listed)
    assert client.post('/api/gdelt/filters',json={'name':'bad','filters':{'unsafe_unknown':True}}).status_code==422
    assert client.post('/api/gdelt/filters',json={'name':'bad','filters':{}},headers={'Origin':'https://evil.example'}).status_code==403
    assert client.get('/api/gdelt/events/123456/select').status_code==405
    key=next(row['key'] for row in listed if row['name']=='援助线索')
    assert client.delete('/api/gdelt/filters/'+key).status_code==200


def test_explicit_selection_enters_existing_workbench_and_retries_idempotently(gdelt_ui):
    client,app=gdelt_ui
    payload={'request_id':'gdelt-ui-select-1','title':'援助发生了什么变化','angle':'先核对交付事实，再查历史背景'}
    refused=client.post('/api/gdelt/events/123456/select',json=payload,headers={'Origin':'https://evil.example'})
    assert refused.status_code==403
    first=client.post('/api/gdelt/events/123456/select',json=payload)
    assert first.status_code==200,first.text
    second=client.post('/api/gdelt/events/123456/select',json=payload)
    assert second.status_code==200 and second.json()['key']==first.json()['key']
    assert client.get(first.json()['url']).status_code==200
    with Session(app.state.engine) as s:
        assert s.exec(select(func.count()).select_from(ContentTask)).one()==1


def test_candidate_http_reads_do_not_write_or_contact_graph(gdelt_ui,monkeypatch):
    from sqlalchemy import event as sql_event
    client,app=gdelt_ui
    statements=[]
    def capture(_connection,_cursor,statement,*_rest):statements.append(statement.lstrip().upper())
    def forbidden(*args,**kwargs):raise AssertionError('Candidate HTTP read contacted graph')
    mirror=app.state.desk.graph
    monkeypatch.setattr(mirror,'query',forbidden);monkeypatch.setattr(mirror,'read',forbidden)
    sql_event.listen(app.state.engine,'before_cursor_execute',capture)
    try:
        for path in ('/radar?source=gdelt&period=all','/api/gdelt/events?period=all',
                     '/api/gdelt/events/123456','/api/gdelt/filters'):
            response=client.get(path)
            assert response.status_code==200,(path,response.text)
    finally:
        sql_event.remove(app.state.engine,'before_cursor_execute',capture)
    assert statements and all(statement.startswith(('SELECT','PRAGMA')) for statement in statements),statements


def test_official_candidates_and_legacy_local_bookmarks(gdelt_ui):
    client,_=gdelt_ui
    official=client.get('/radar?source=gdelt')
    assert official.status_code==200 and '<h1>全球事件线索</h1>' in official.text
    local=client.get('/radar?source=local')
    assert local.status_code==200 and 'data-radar data-facet-state=' in local.text
    assert '<h1>已整理事件</h1>' in local.text
    bookmark=client.get('/radar?q=existing-search')
    assert bookmark.status_code==200 and 'data-radar data-facet-state=' in bookmark.text
    assert 'name="source" value="local"' in local.text
    assert 'href="/radar?source=local"' in official.text


def test_radar_without_official_rows_shows_daily_landing(tmp_path,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL','')
    app=create_app(db_url='sqlite:///'+str(tmp_path/'empty.db'),storage_root=str(tmp_path/'runs'))
    try:
        response=TestClient(app).get('/radar')
        assert response.status_code==200 and 'data-daily-edition=' in response.text
        assert '<h1>全球事件线索</h1>' not in response.text
    finally:app.state.engine.dispose()


def test_selected_card_prioritizes_continuation_and_keeps_other_angle_secondary(gdelt_ui):
    client,_=gdelt_ui
    selected=client.post('/api/gdelt/events/123456/select',json={
        'request_id':'ui-continuation-0270','title':'核对援助线索','angle':'先读取原文'})
    assert selected.status_code==200,selected.text
    page=client.get('/radar?source=gdelt&period=all')
    assert page.status_code==200
    assert '继续这项选题 →' in page.text
    assert '另起一个角度' in page.text
    assert 'gdelt-other-angle' in page.text
    assert selected.json()['url'] in page.text
    assert '已形成选题' in page.text


def test_source_detail_link_can_open_outside_current_filter_and_keeps_non_js_evidence(gdelt_ui):
    client,_=gdelt_ui
    page=client.get('/radar?source=gdelt&country=us&period=all&event=123456')
    assert page.status_code==200,page.text
    assert '当前筛选没有命中的事件线索' in page.text
    assert 'id="gdelt-detail-dialog"' in page.text
    detail=client.get('/api/gdelt/events/123456')
    assert detail.status_code==200 and detail.json()['event']['event_id']=='123456'
    matching=client.get('/radar?source=gdelt&period=all').text
    assert 'href="/api/gdelt/events/123456"' in matching
    assert '查看已保存的结构化详情' in matching
    assert '发生日期</span><time datetime=' in matching
    assert '报道语言未知' in matching or '报道语种未知' in matching


def test_radar_assets_are_packaged_locally_and_current_page_is_visible(gdelt_ui):
    client,_=gdelt_ui
    page=client.get('/radar?source=gdelt&period=all&limit=1')
    assert page.status_code==200
    assert '第 1 页' in page.text
    assert '查看总条数与页数' in page.text
    assert '焦点关键词' in page.text
    assert client.get('/research-assets/gdelt-events.css').status_code==200
    assert client.get('/research-assets/gdelt-events.js').status_code==200


def test_deleted_topic_filter_is_explicitly_recoverable_instead_of_widened(gdelt_ui):
    client,_=gdelt_ui
    missing='w_'+'a'*32
    response=client.get('/radar?source=gdelt&period=all&watch='+missing)
    assert response.status_code==200,response.text
    assert '焦点筛选暂不可用' in response.text
    assert '当前未执行事件筛选' in response.text
    assert '移除不可用的焦点条件' in response.text
    assert 'value="'+missing+'" selected' in response.text
    assert 'data-gdelt-event="123456"' not in response.text
    assert client.get('/api/gdelt/events?period=all&watch='+missing).status_code==404
