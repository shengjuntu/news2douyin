"""Real PostgreSQL checks; opt in with NEWS2DOUYIN_TEST_POSTGRES_URL."""
import json
import os
from uuid import uuid4
import pytest
from sqlalchemy import create_engine,event as sa_event,text,select as sa_select,literal
from sqlmodel import Session,select
from news2douyin.storage.db import create_schema
from news2douyin.storage.models import Article,ArticleVersion,Event,TaskRecord,CollectedObservation
from news2douyin.storage.articles import record_version
from news2douyin.desk.models import DeskDirty,DeskMeta,GraphPending,WatchReview
from news2douyin.research.models import ResearchQuestion
from news2douyin.extraction.models import VersionReading
from news2douyin.desk.service import initialize as init_desk
from news2douyin.extraction.service import initialize as init_extraction
from news2douyin.search.index import init_search,index_available
from news2douyin.search.service import article_page
from news2douyin.storage.sql import json_text,json_is_string,time_value


@pytest.fixture
def pg_engine():
    url=os.getenv('NEWS2DOUYIN_TEST_POSTGRES_URL')
    if not url:pytest.skip('real PostgreSQL URL not configured')
    schema='n2d_search_test_'+uuid4().hex
    admin=create_engine(url)
    with admin.begin() as c:c.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine=create_engine(url,connect_args={'options':f'-c search_path={schema},public'})
    create_schema(engine)
    init_search(engine)
    init_desk(engine,backfill=False)
    init_extraction(engine,backfill=False)
    try:yield engine
    finally:
        engine.dispose()
        with admin.begin() as c:c.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def test_pg_transactional_triggers_are_idempotent_and_skip_noop_updates(pg_engine):
    with Session(pg_engine) as s:
        s.add(Event(event_key='event1',event_title='Initial event'))
        article=Article(article_key='a',title='Initial report',content='Source evidence')
        s.add(article);record_version(s,article);s.commit()
        reading=s.exec(select(VersionReading)).one();reading.state='ready';s.add(reading)
        s.exec(text('DELETE FROM deskdirty'));s.commit()
    init_desk(pg_engine,backfill=False)
    init_extraction(pg_engine,backfill=False)
    with Session(pg_engine) as s:
        assert not s.get(DeskMeta,'initial_index')
        assert not s.exec(select(DeskDirty)).all()
        assert s.exec(select(VersionReading)).one().state=='ready'
        s.exec(text("UPDATE event SET event_title=event_title WHERE event_key='event1'"));s.commit()
        assert not s.exec(select(DeskDirty)).all()
        s.exec(text("UPDATE event SET event_title='Changed event' WHERE event_key='event1'"))
        assert s.get(DeskDirty,'event1')
        s.rollback()
        assert not s.get(DeskDirty,'event1')
        previous=s.exec(select(ArticleVersion)).one()
        s.add(ArticleVersion(article_key='a',revision=2,content_hash='another-version',payload_json=previous.payload_json))
        s.flush()
        assert len(s.exec(select(VersionReading)).all())==2
        s.rollback()
        assert len(s.exec(select(VersionReading)).all())==1


def test_pg_research_trigger_replays_changes_but_does_not_retokenize_noops(pg_engine):
    with Session(pg_engine) as s:
        s.add(WatchReview(key='ticket',watch='focus',event='',question='Context?',case_key='case'))
        s.add(ResearchQuestion(question_id='question',case_id='case',key='context',text='Context?'));s.commit()
        first=s.get(GraphPending,'watch/focus').token
        s.exec(text("UPDATE researchquestion SET text=text WHERE question_id='question'"));s.commit()
        assert s.get(GraphPending,'watch/focus').token==first
        s.exec(text("UPDATE researchquestion SET text='Changed?' WHERE question_id='question'"));s.commit()
        s.expire_all();second=s.get(GraphPending,'watch/focus').token
        assert second!=first
        s.exec(text("DELETE FROM researchquestion WHERE question_id='question'"));s.flush()
        s.expire_all();assert s.get(GraphPending,'watch/focus').token!=second
        s.rollback();s.expire_all();assert s.get(GraphPending,'watch/focus').token==second


def test_pg_safe_json_and_datetime_expressions_keep_invalid_legacy_rows_readable(pg_engine):
    with Session(pg_engine) as s:
        assert s.scalar(sa_select(json_text(literal('{"provider":"gkg"}'),'provider')))=='gkg'
        assert s.scalar(sa_select(json_text(literal('not json'),'provider'))) is None
        assert s.scalar(sa_select(json_is_string(literal('{"provider":42}'),'provider'))) is False
        assert s.scalar(sa_select(time_value('2026-11-11T01:00:00+02:00')==time_value('2026-11-10T23:00:00Z'))) is True
        assert s.scalar(sa_select(time_value('invalid date'))) is None
        assert s.scalar(sa_select(time_value('2026-11-11T01:00:00')==time_value('2026-11-11T01:00:00Z'))) is True
        s.add(Article(article_key='a',provider=''))
        s.add(TaskRecord(task_id='bad',profile_name='old',profile_json='not json',request_hash='bad'))
        s.add(CollectedObservation(observation_key='observation',scope_id='bad',input_index=0,article_key='a',revision=1,disposition='kept'));s.commit()
        from news2douyin.knowledge.radar import provider_articles
        assert s.exec(provider_articles('unknown')).all()==['a']


def test_pg_trigram_search_matches_literal_wildcards_chinese_and_offset_dates(pg_engine):
    with Session(pg_engine) as s:
        for key,title,when in [('a','Diplomacy 100% and 中文','2026-11-11T01:00:00+02:00'),
                               ('b','Diplomacy 1000 and 中文','2026-11-11T01:00:00Z'),
                               ('c','An invalid timestamp','not a date')]:
            s.add(Article(article_key=key,title=title,published_at=when))
        s.commit()
        capability=index_available(s)
        if not capability:pytest.skip('pg_trgm not installed in this PostgreSQL QA distribution')
        assert capability=='pg_trgm'
        assert [a.article_key for a in article_page(s,query='100%').items]==['a']
        assert article_page(s,query='中文').total==2
        dated=article_page(s,period='custom',date_from='2026-11-10',date_to='2026-11-10',timezone='UTC')
        assert [a.article_key for a in dated.items]==['a']
        assert dated.search_backend=='pg_trgm+literal'
        s.exec(text('SET LOCAL enable_seqscan=off'))
        plan=s.exec(text("EXPLAIN SELECT id FROM article WHERE lower(title) LIKE '%diplomacy%'" )).all()
        assert 'n2d_article_title_trgm' in '\n'.join(str(row) for row in plan)


def test_pg_search_explicitly_reports_safe_literal_fallback(pg_engine):
    with pg_engine.begin() as c:
        c.exec_driver_sql('DROP INDEX IF EXISTS n2d_article_title_trgm')
    with Session(pg_engine) as s:
        s.add(Article(article_key='fallback',title='外交背景'));s.commit()
        result=article_page(s,query='外交')
        assert result.total==1 and result.search_backend=='pg_sql_literal_degraded'
        assert result.envelope()['search_warning']


def test_pg_extension_permission_failure_keeps_search_and_transaction_usable(pg_engine):
    # A schema/table owner can legitimately lack CREATE on the database. This
    # checks a real permission error, not a mocked exception or a missing index.
    name='n2d_restricted_'+uuid4().hex
    schema=name+'_schema'
    url=os.environ['NEWS2DOUYIN_TEST_POSTGRES_URL']
    admin=create_engine(url)
    with admin.begin() as c:
        c.exec_driver_sql('DROP EXTENSION IF EXISTS pg_trgm CASCADE')
        c.exec_driver_sql(f'CREATE ROLE "{name}" NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT')
        c.exec_driver_sql(f'CREATE SCHEMA "{schema}" AUTHORIZATION "{name}"')
    limited=create_engine(url,connect_args={'options':f'-c search_path={schema},public'})
    @sa_event.listens_for(limited,'connect')
    def role(connection,_):
        with connection.cursor() as c:c.execute(f'SET ROLE "{name}"')
        connection.commit()
    try:
        create_schema(limited)
        init_search(limited)
        with Session(limited) as s:
            s.add(Article(article_key='limited',title='Restricted search works'));s.commit()
            result=article_page(s,query='restricted')
            assert result.total==1 and result.search_backend=='pg_sql_literal_degraded'
            assert '未能建立' in result.search_warning
    finally:
        limited.dispose()
        with admin.begin() as c:
            c.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
            c.exec_driver_sql(f'DROP ROLE "{name}"')
        admin.dispose()
