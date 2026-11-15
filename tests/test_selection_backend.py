"""Source discovery contracts: real SQL backends, no model/network side effects."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json

import pytest
from sqlalchemy import event as sql_event, func
from sqlmodel import Session, select

from news2douyin.intake.official_models import OfficialEvent, OfficialMention
from news2douyin.selection import service
from news2douyin.selection.models import SelectionOrigin, SelectionFilter
from news2douyin.storage.db import init_db, make_engine
from news2douyin.storage.models import Article, Event
from news2douyin.workbench.models import ContentTask, ContentBinding
from news2douyin.topics.models import TopicWorkspace, TopicSource
from news2douyin.research.models import ResearchCase
from news2douyin.desk.models import Watch, RadarEntry
from news2douyin.tasks.control import TaskConflict
from test_postgres_integration import postgres_url


def _iso(value):
    return value.isoformat(timespec='seconds').replace('+00:00', 'Z')


@pytest.fixture(params=['sqlite', 'postgres'])
def selection_engine(request, tmp_path):
    url=request.getfixturevalue('postgres_url') if request.param=='postgres' else 'sqlite:///'+str(tmp_path/'selection.db')
    engine=make_engine(url);init_db(engine)
    now=datetime.now(timezone.utc)
    with Session(engine) as session:
        for key, actor, root, country, age in [
            ('101','DONALD TRUMP','07','ua',0), ('102','ALICE','04','cn',240),
            ('103','DONALD TRUMP','19','fr',0), ('104','100% INC','07','us',0)]:
            when=_iso(now-timedelta(hours=age))
            session.add(OfficialEvent(event_id=key,event_date='2020-01-01',added_at=when,
                first_seen=when,last_seen=when,actor1_name=actor,actor2_name='UKRAINE',
                actor1_country='USA',actor1_country_iso2='us',action_country_iso2=country,
                event_code=root+'0',event_base_code=root+'0',event_root_code=root,
                initial_sources=999 if key=='101' else 0,source_url='javascript:alert(1)' if key=='104' else 'https://wire.example/'+key,
                feed='events_english',stamp='20261114000000',row_index=int(key)))
        for key, event_id, domain in [('a','101','same.example'),('b','101','same.example'),
                                      ('c','103','one.example'),('d','103','two.example')]:
            session.add(OfficialMention(key=key,event_id=event_id,doc_identifier='https://'+domain+'/'+key,
                domain=domain,first_seen=_iso(now),last_seen=_iso(now),confidence=80,
                first_feed='mentions_english',first_stamp='20261114000000',first_row_index=1,
                last_feed='mentions_english',last_stamp='20261114000000',last_row_index=1))
        session.add(Watch(key='w_'+'a'*32,name='特朗普',kind='人物',scope='特朗普'))
        session.commit()
    yield engine
    engine.dispose()


def _ids(session, **filters):
    return [row['event_id'] for row in service.list_candidates(session,service.checked(**filters))['items']]


def test_filters_use_current_observations_and_separate_event_occurrence_time(selection_engine):
    with Session(selection_engine) as session:
        assert set(_ids(session))=={'101','103','104'}  # 2020 occurrence can have a new 2026 mention.
        assert set(_ids(session,period='all'))=={'101','102','103','104'}
        assert set(_ids(session,cameo='07'))=={'101','104'}
        assert set(_ids(session,country='UA'))=={'101'}
        assert set(_ids(session,country='us'))=={'101','103','104'}  # actor affiliation also counts.
        assert _ids(session,min_sources=2)==['103']  # 999 initial sources is not live evidence.
        assert service.detail(session,'101')['event']['observed_documents']==2
        assert service.detail(session,'101')['event']['observed_sources']==1
        unknown=service.detail(session,'104')['event']
        assert unknown['observed_sources'] is None and unknown['coverage_known'] is False
        assert unknown['source_url']==''


def test_alias_recall_and_literal_wildcards_have_no_membership_side_effect(selection_engine):
    with Session(selection_engine) as session:
        assert set(_ids(session,q='特朗普'))=={'101','103'}
        assert set(_ids(session,watch='w_'+'a'*32))=={'101','103'}
        assert _ids(session,q='100%')==['104']
        assert _ids(session,q='_')==[]
        for model in (Article,Event,RadarEntry,ContentTask):
            assert session.exec(select(func.count()).select_from(model)).one()==0


def test_page_queries_are_read_only_and_never_load_archive_payloads(selection_engine):
    statements=[]
    def record(_conn,_cursor,sql,*_):statements.append(sql.lower())
    sql_event.listen(selection_engine,'before_cursor_execute',record)
    try:
        with Session(selection_engine) as session:
            data=service.list_candidates(session,service.checked(period='all'),limit=2)
    finally:sql_event.remove(selection_engine,'before_cursor_execute',record)
    assert data['total'] is None and data['has_more'] and len(data['items'])==2
    assert all(statement.lstrip().startswith('select') for statement in statements)
    assert any('limit' in statement for statement in statements)
    assert not any('articleversion' in statement or 'raw_line' in statement or 'metadata_json' in statement for statement in statements)
    # Saved report content adds one bounded per-event recent-candidate read and fixed
    # PK-batched lookups; it must not become one query per card/source URL.
    assert len(statements)<=9, statements
    preview_queries=[sql for sql in statements if 'from officialmention' in sql and 'limit' in sql]
    assert len(preview_queries)==1
    assert 'row_number()' not in preview_queries[0]
    assert 'order by officialmention.last_seen desc' in preview_queries[0]
    assert sum('from articleidentity' in sql for sql in statements)==1
    assert sum('from sourcepreviewjob' in sql for sql in statements)==1
    assert not any('canonical_url =' in sql or 'article.url =' in sql for sql in statements)


def test_selection_is_idempotent_and_freezes_metadata_without_fabricating_local_event(selection_engine):
    first=service.select_candidate(selection_engine,'101','qa-select-source-101',angle='核对实际交付')
    with Session(selection_engine) as session:
        row=session.get(OfficialEvent,'101');row.actor1_name='Later source correction';session.add(row);session.commit()
    repeat=service.select_candidate(selection_engine,'101','qa-select-source-101',angle='核对实际交付')
    assert repeat==first
    with Session(selection_engine) as session:
        origin=session.get(SelectionOrigin,first['key'])
        source=session.exec(select(TopicSource)).one()
        frozen=json.loads(source.document_json)
        assert 'DONALD TRUMP' in frozen['title']
        assert frozen['source_kind']=='event_metadata' and frozen['requires_original_text']
        assert frozen['official_event_id']=='101' and frozen['source_ref']['row_index']==101
        assert session.exec(select(func.count()).select_from(ContentTask)).one()==1
        assert session.exec(select(func.count()).select_from(Article)).one()==0
        assert session.exec(select(func.count()).select_from(Event)).one()==0
        assert json.loads(origin.snapshot_json)['event']['actor1_name']=='DONALD TRUMP'
    with pytest.raises(TaskConflict):service.select_candidate(selection_engine,'103','qa-select-source-101',angle='核对实际交付')


def test_selection_insert_failure_rolls_back_workspace_case_and_bindings(selection_engine):
    def reject(_conn,_cursor,statement,*_):
        if statement.lower().lstrip().startswith('insert into selectionorigin'):
            raise RuntimeError('synthetic checkpoint failure')
    sql_event.listen(selection_engine,'before_cursor_execute',reject)
    try:
        with pytest.raises(RuntimeError,match='synthetic'):
            service.select_candidate(selection_engine,'101','qa-select-atomic-failure')
    finally:sql_event.remove(selection_engine,'before_cursor_execute',reject)
    with Session(selection_engine) as session:
        for model in (ContentTask,TopicWorkspace,TopicSource,ResearchCase,ContentBinding,SelectionOrigin):
            assert session.exec(select(func.count()).select_from(model)).one()==0,model
    assert service.select_candidate(selection_engine,'101','qa-select-atomic-failure')['key']


def test_concurrent_retries_publish_one_selected_topic(selection_engine):
    with ThreadPoolExecutor(max_workers=4) as pool:
        rows=list(pool.map(lambda _:service.select_candidate(selection_engine,'101','qa-concurrent-select-101'),range(4)))
    assert len({row['key'] for row in rows})==1
    with Session(selection_engine) as session:
        for model in (ContentTask,TopicSource,SelectionOrigin):
            assert session.exec(select(func.count()).select_from(model)).one()==1


@pytest.mark.parametrize('values',[{'cameo':'99'},{'country':'ZZ'},{'period':'forever'},
    {'min_sources':True},{'min_sources':1.5},{'min_sources':-1},{'watch':'raw/sql'},
    {'preset':'not-an-id'},{'order':'raw_sql'}])
def test_bad_filter_values_are_rejected(values):
    with pytest.raises(ValueError):service.checked(**values)


def test_detail_prefers_available_extraction_confidence_over_unknown(selection_engine):
    with Session(selection_engine) as session:
        when=datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
        session.add(OfficialMention(key='unknown-confidence',event_id='101',
            doc_identifier='https://unknown.example/report',domain='unknown.example',confidence=None,
            first_seen=when,last_seen=when,first_feed='mentions_english',first_stamp='20261114000000',
            first_row_index=90,last_feed='mentions_english',last_stamp='20261114000000',last_row_index=90))
        session.commit()
        result=service.detail(session,'101')
        assert result['mentions'][0]['confidence']==80
        assert result['mentions'][-1]['confidence'] is None


def test_country_names_and_short_codes_do_not_match_other_country_substrings(selection_engine):
    now=_iso(datetime.now(timezone.utc))
    with Session(selection_engine) as session:
        session.add(OfficialEvent(event_id='105',event_date='2026-11-14',added_at=now,first_seen=now,last_seen=now,
            actor1_name='RUSSIAN GOVERNMENT',actor1_country='RUS',actor1_country_iso2='ru',action_country_iso2='ru',
            event_code='040',event_base_code='040',event_root_code='04',feed='events_english',stamp='20261114000000',row_index=105))
        session.commit()
        assert set(_ids(session,q='美国'))=={'101','103','104'}
        assert set(_ids(session,q='US'))=={'101','103','104'}
        assert _ids(session,q='中国',period='all')==['102']
        assert _ids(session,q='CH',period='all')==[]  # ISO CH means Switzerland; source FIPS CH is not reused.
