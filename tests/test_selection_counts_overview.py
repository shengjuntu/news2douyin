"""Totals remain opt-in, narrow, time bounded, and honest about index gaps."""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event as sql_event
from sqlmodel import Session

from news2douyin.intake.models import FeedState, IntakeBatch
from news2douyin.selection import service
from news2douyin.selection.counts import CandidateCounter
from news2douyin.selection.overview import overview
from news2douyin.server.app import create_app
from test_selection_backend import selection_engine
from test_postgres_integration import postgres_url


def test_opt_in_count_respects_filters_and_reuses_only_short_lived_totals(selection_engine):
    counter=CandidateCounter(selection_engine)
    filters=service.checked(period='all')
    first=counter.count(filters,limit=3)
    assert first['total']==4 and first['pages']==2 and not first['cached']
    again=counter.count(filters,limit=2)
    assert again['total']==4 and again['pages']==2 and again['cached']
    assert again['computed_at']==first['computed_at'] and again['cache_seconds']==30
    assert counter.count(service.checked(period='all',min_sources=2))['total']==1
    assert counter.count(service.checked(period='all',q='特朗普'))['total']==2
    empty=counter.count(service.checked(period='all',q='nothing-matches'))
    assert empty['total']==0 and empty['pages']==0


def test_count_selects_count_only_and_never_article_or_archive_payload(selection_engine):
    sql=[]
    def record(_conn,_cursor,statement,*_):sql.append(statement.lower())
    sql_event.listen(selection_engine,'before_cursor_execute',record)
    try:CandidateCounter(selection_engine).count(service.checked(period='all'))
    finally:sql_event.remove(selection_engine,'before_cursor_execute',record)
    count=[query for query in sql if 'count(' in query]
    assert len(count)==1 and 'officialevent' in count[0]
    assert 'actor1_name' not in count[0] and 'articleversion' not in count[0]
    assert all('raw_line' not in query and 'metadata_json' not in query for query in sql)


def test_parallel_same_filter_reports_busy_instead_of_duplicate_count(selection_engine):
    import json
    counter=CandidateCounter(selection_engine)
    filters=service.checked(period='all')
    key=json.dumps({k:v for k,v in filters.items() if k!='preset'},sort_keys=True,ensure_ascii=False)
    counter.running.add(key)
    result=counter.count(filters)
    assert result['status']=='busy' and result['total'] is None and result['pages'] is None


def test_count_timeout_does_not_poison_reused_sqlite_connection(tmp_path):
    from news2douyin.storage.db import make_engine,init_db
    from news2douyin.intake.official_models import OfficialEvent
    engine=make_engine('sqlite:///'+str(tmp_path/'count-budget.db'));init_db(engine)
    now=datetime.now(timezone.utc).isoformat().replace('+00:00','Z')
    with Session(engine) as session:
        session.add_all([OfficialEvent(event_id=str(i),event_date='2026-11-14',last_seen=now,
            actor1_name='Searchable source '+str(i),feed='events_english',stamp='20261114000000',row_index=i+1)
            for i in range(500)])
        session.commit()
    try:
        filters=service.checked(period='all',q='Searchable')
        result=CandidateCounter(engine,budget_ms=0).count(filters)
        assert result['status']=='timeout' and result['total'] is None
        assert CandidateCounter(engine).count(filters)['total']==500
        with engine.connect() as connection:
            assert connection.exec_driver_sql('PRAGMA busy_timeout').scalar()==30000
    finally:engine.dispose()


def test_overview_never_crosses_archived_index_gap_or_queries_raw_lake(selection_engine):
    with Session(selection_engine) as session:
        session.merge(FeedState(key='events_english',last_complete='20261114123000'))
        missing=session.get(FeedState,'mentions_english')
        if missing:session.delete(missing)
        for stamp,rows,done in [('20261114120000',2,2),('20261114121500',3,1),('20261114123000',4,4)]:
            session.add(IntakeBatch(key='events_english_'+stamp,feed='events_english',stamp=stamp,
                state='archived',rows=rows,projected_offset=done))
        session.commit()
        data=overview(session)
        assert len(data['feeds'])==6
        feed=next(row for row in data['feeds'] if row['feed']=='events_english')
        assert feed['archived_at']=='2026-11-14T12:30:00Z'
        assert feed['indexed_at']=='2026-11-14T12:00:00Z'
        assert feed['pending_batches']==1 and feed['pending_rows']==2
        absent=next(row for row in data['feeds'] if row['feed']=='mentions_english')
        assert absent['enabled'] is None and absent['indexed_at']==''
        session.add(IntakeBatch(key='events_english_20261114114500',feed='events_english',
            stamp='20261114114500',state='error',last_error='private upstream error'))
        session.commit()
        feed=next(row for row in overview(session)['feeds'] if row['feed']=='events_english')
        assert feed['indexed_at']==''


def test_static_count_route_is_not_misread_as_event_id(tmp_path,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_GKG_STREAM_ENABLED','0')
    monkeypatch.setenv('NEWS2DOUYIN_GKG_PROJECTION_ENABLED','0')
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL','')
    app=create_app(db_url='sqlite:///'+str(tmp_path/'http.db'),storage_root=str(tmp_path/'runs'))
    try:
        with TestClient(app) as client:
            result=client.get('/api/gdelt/events/count?period=all&limit=20')
            assert result.status_code==200 and result.json()['total']==0
            assert client.get('/api/gdelt/overview').status_code==200
            assert client.get('/radar?source=gdelt&event=12345').status_code==200
            assert client.get('/radar?source=gdelt&event=javascript:bad').status_code==422
    finally:app.state.engine.dispose()
