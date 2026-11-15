"""Catalog-only legacy migration and complete long text on real PostgreSQL."""
import random
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event as sql_event, inspect, text
from sqlalchemy.exc import SQLAlchemyError
from sqlmodel import Session, select

from news2douyin.storage.db import create_schema, init_db, make_engine
from news2douyin.storage.index_safety import IndexSafetyError, ensure_safe_text_indexes
from news2douyin.storage.models import Article, Event
from test_postgres_integration import postgres_url


@pytest.fixture(params=['sqlite', 'postgres'])
def indexed_engine(request,tmp_path):
    url=request.getfixturevalue('postgres_url') if request.param=='postgres' else 'sqlite:///'+str(tmp_path/'indexes.db')
    engine=make_engine(url)
    create_schema(engine)
    try:yield engine
    finally:engine.dispose()


def names(engine,table):
    if engine.dialect.name=='sqlite':
        # SQLAlchemy intentionally omits expression indexes from SQLite
        # reflection. Check the actual catalog when asserting preservation.
        with engine.connect() as c:
            return set(c.execute(text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=:table"),dict(table=table)).scalars())
    return {row['name'] for row in inspect(engine).get_indexes(table)}


def old_indexes(engine):
    with engine.begin() as c:
        c.exec_driver_sql('CREATE INDEX ix_event_event_title ON event (event_title)')
        c.exec_driver_sql('CREATE INDEX ix_article_source_domain ON article (source_domain)')


def long_text(length=2800):
    # Repeated characters compress well in PostgreSQL and do not reproduce the
    # legacy B-tree failure. Deterministic high entropy tests the actual limit.
    rng=random.Random(291)
    return ''.join(chr(0x4e00+rng.randrange(20000)) for _ in range(length))


def test_new_schema_accepts_full_long_headlines_and_source_text(indexed_engine):
    engine=indexed_engine
    init_db(engine)
    title=long_text()
    assert len(title.encode())>7000
    with Session(engine) as s:
        s.add(Event(event_key='long-index-fixture',event_title=title))
        s.add(Article(article_key='long-source-fixture',title=title,
            source_domain=title,canonical_url='https://safe.example/report'))
        s.commit()
    with Session(engine) as s:
        assert s.exec(select(Event.event_title).where(Event.event_key=='long-index-fixture')).one()==title
        assert s.exec(select(Article.source_domain).where(Article.article_key=='long-source-fixture')).one()==title
    assert 'ix_event_event_title' not in names(engine,'event')
    assert 'ix_article_source_domain' not in names(engine,'article')
    # This release deliberately preserves existing legacy equality lookup.
    assert 'ix_article_canonical_url' in names(engine,'article')


def test_legacy_indexes_are_removed_without_reading_business_rows_and_never_recreated(indexed_engine):
    engine=indexed_engine
    old_indexes(engine)
    recorded=[]
    def audit(_connection,_cursor,statement,*_args):recorded.append(statement.lower())
    sql_event.listen(engine,'before_cursor_execute',audit)
    try:
        result=ensure_safe_text_indexes(engine)
    finally:sql_event.remove(engine,'before_cursor_execute',audit)
    assert set(result['removed'])=={'ix_event_event_title','ix_article_source_domain'}
    assert result['source_text_preserved'] and not result['scans_or_rebuilds']
    assert not any(' from event' in sql or ' from article' in sql for sql in recorded)
    assert not any(sql.lstrip().startswith(('update ','insert ','delete ','create index')) for sql in recorded)
    assert ensure_safe_text_indexes(engine)['removed']==[]
    init_db(engine)
    init_db(engine)
    assert 'ix_event_event_title' not in names(engine,'event')
    assert 'ix_article_source_domain' not in names(engine,'article')
    assert 'ix_article_canonical_url' in names(engine,'article')


@pytest.mark.parametrize('definition',[
    'CREATE UNIQUE INDEX ix_article_source_domain ON article (source_domain)',
    'CREATE INDEX ix_article_source_domain ON article (title)',
    'CREATE INDEX ix_article_source_domain ON article (source_domain) WHERE id>1',
    'CREATE INDEX ix_article_source_domain ON article (lower(source_domain))',
    'CREATE INDEX ix_article_source_domain ON article (source_domain DESC)',
])
def test_definition_drift_stops_without_removing_any_index(indexed_engine,definition):
    engine=indexed_engine
    with engine.begin() as c:
        c.exec_driver_sql('CREATE INDEX ix_event_event_title ON event (event_title)')
        c.exec_driver_sql(definition)
    with pytest.raises(IndexSafetyError,match='ix_article_source_domain.*不一致'):
        ensure_safe_text_indexes(engine)
    assert 'ix_event_event_title' in names(engine,'event')
    assert 'ix_article_source_domain' in names(engine,'article')


def test_ddl_failure_rolls_back_both_index_drops(indexed_engine):
    engine=indexed_engine
    old_indexes(engine)
    def interrupt(_connection,_cursor,statement,*_args):
        if statement.upper().startswith('DROP INDEX') and 'ix_article_source_domain' in statement:
            raise RuntimeError('QA simulated DDL interruption')
    sql_event.listen(engine,'before_cursor_execute',interrupt)
    try:
        with pytest.raises(RuntimeError,match='DDL interruption'):ensure_safe_text_indexes(engine)
    finally:sql_event.remove(engine,'before_cursor_execute',interrupt)
    assert 'ix_event_event_title' in names(engine,'event')
    assert 'ix_article_source_domain' in names(engine,'article')
    assert len(ensure_safe_text_indexes(engine)['removed'])==2


def test_same_name_on_other_table_is_never_dropped(indexed_engine):
    engine=indexed_engine
    with engine.begin() as c:c.exec_driver_sql('CREATE INDEX ix_event_event_title ON article (title)')
    with pytest.raises(IndexSafetyError,match='ix_event_event_title.*不一致'):ensure_safe_text_indexes(engine)
    assert 'ix_event_event_title' in names(engine,'article')


def test_parallel_startups_migrate_the_old_indexes_once(indexed_engine):
    engine=indexed_engine
    old_indexes(engine)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _number:ensure_safe_text_indexes(engine),range(2)))
    assert sorted(len(row['removed']) for row in results)==[0,2]


def test_postgresql_legacy_constraint_and_hash_index_are_preserved(postgres_url):
    engine=make_engine(postgres_url);create_schema(engine)
    try:
        with engine.begin() as c:
            c.exec_driver_sql('ALTER TABLE event ADD CONSTRAINT ix_event_event_title UNIQUE(event_title)')
        with pytest.raises(IndexSafetyError,match='不一致'):ensure_safe_text_indexes(engine)
        with engine.begin() as c:
            assert c.execute(text("SELECT 1 FROM pg_constraint WHERE conname='ix_event_event_title'")).scalar_one()==1
            c.exec_driver_sql('ALTER TABLE event DROP CONSTRAINT ix_event_event_title')
            c.exec_driver_sql('CREATE INDEX ix_event_event_title ON event USING hash (event_title)')
        with pytest.raises(IndexSafetyError,match='不一致'):ensure_safe_text_indexes(engine)
        assert 'ix_event_event_title' in names(engine,'event')
    finally:engine.dispose()


def test_postgresql_migration_is_limited_to_current_schema(postgres_url):
    engine=make_engine(postgres_url);create_schema(engine)
    try:
        old_indexes(engine)
        with engine.begin() as c:
            c.exec_driver_sql('CREATE SCHEMA other_indexes')
            c.exec_driver_sql('CREATE TABLE other_indexes.event (event_title text)')
            c.exec_driver_sql('CREATE INDEX ix_event_event_title ON other_indexes.event (event_title)')
        ensure_safe_text_indexes(engine)
        with engine.connect() as c:
            assert c.execute(text("SELECT to_regclass('other_indexes.ix_event_event_title')")).scalar_one()
            assert c.execute(text("SELECT to_regclass('public.ix_event_event_title')")).scalar_one() is None
    finally:engine.dispose()


def test_postgresql_original_index_failure_is_fixed_by_the_upgrade(postgres_url):
    engine=make_engine(postgres_url);create_schema(engine)
    try:
        old_indexes(engine)
        with Session(engine) as s:
            s.add(Event(event_key='legacy',event_title='short'));s.commit()
        title=long_text()
        with pytest.raises(SQLAlchemyError) as failure:
            with engine.begin() as c:c.execute(text("UPDATE event SET event_title=:title WHERE event_key='legacy'"),dict(title=title))
        assert getattr(failure.value.orig,'sqlstate',None)=='54000'
        init_db(engine)
        with engine.begin() as c:
            c.execute(text("UPDATE event SET event_title=:title WHERE event_key='legacy'"),dict(title=title))
        with engine.connect() as c:
            assert c.execute(text("SELECT event_title FROM event WHERE event_key='legacy'")).scalar_one()==title
        init_db(engine)
        assert 'ix_event_event_title' not in names(engine,'event')
    finally:engine.dispose()


def test_postgresql_busy_old_writer_causes_bounded_atomic_failure(postgres_url):
    engine=make_engine(postgres_url);create_schema(engine)
    try:
        old_indexes(engine)
        with engine.begin() as holder:
            holder.exec_driver_sql('LOCK TABLE event IN ACCESS SHARE MODE')
            started=time.monotonic()
            with pytest.raises(IndexSafetyError,match='停止旧写进程') as caught:
                ensure_safe_text_indexes(engine)
            assert getattr(caught.value.__cause__.orig,'sqlstate',None)=='55P03'
            assert time.monotonic()-started<6
            assert 'ix_event_event_title' in names(engine,'event')
            assert 'ix_article_source_domain' in names(engine,'article')
        assert len(ensure_safe_text_indexes(engine)['removed'])==2
    finally:engine.dispose()
