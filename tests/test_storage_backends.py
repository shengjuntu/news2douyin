from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import sqlite3

import pytest
from sqlalchemy import event,text
from sqlalchemy.exc import OperationalError,TimeoutError
from sqlalchemy.pool import QueuePool,SingletonThreadPool

from news2douyin.storage import db
from news2douyin.storage.db import make_engine, normalize_db_url, is_lock_error


@pytest.fixture(autouse=True)
def connection_configuration(monkeypatch):
    for name in ('NEWS2DOUYIN_DB_POOL_SIZE','NEWS2DOUYIN_DB_MAX_OVERFLOW',
                 'NEWS2DOUYIN_DB_POOL_TIMEOUT','NEWS2DOUYIN_SQLITE_BUSY_TIMEOUT_MS',
                 'NEWS2DOUYIN_DB_LOCK_TIMEOUT_MS','NEWS2DOUYIN_DB_STATEMENT_TIMEOUT_MS'):
        monkeypatch.delenv(name,raising=False)


def test_postgres_configuration_uses_environment_without_connecting(monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_DATABASE_URL', 'postgresql://user:secret@127.0.0.1:1/db')
    engine = make_engine()
    try:
        assert engine.dialect.name == 'postgresql'
        assert engine.dialect.driver == 'psycopg'
        assert engine.pool.size() == 8
        assert engine.pool._max_overflow == 8
        assert engine.pool.timeout() == 10
        assert engine.hide_parameters
        assert 'secret' not in str(engine.url)
        assert normalize_db_url('postgres://user@localhost/db') == 'postgresql+psycopg://user@localhost/db'
    finally:
        engine.dispose()


def test_wal_reader_snapshot_does_not_block_committing_writer(tmp_path):
    path = tmp_path/'concurrent.db'
    engine = make_engine('sqlite:///'+str(path))
    with engine.begin() as c:
        c.execute(text('CREATE TABLE demo (id INTEGER PRIMARY KEY, value TEXT)'))
        c.execute(text("INSERT INTO demo VALUES (1, 'before')"))
    with engine.connect() as reader:
        assert reader.exec_driver_sql('PRAGMA journal_mode').scalar() == 'wal'
        assert reader.exec_driver_sql('PRAGMA busy_timeout').scalar() == 30000
        assert isinstance(engine.pool,QueuePool)
        assert engine.pool.size() == 5 and engine.pool._max_overflow == 10
        assert engine.pool.timeout() == 10
        reader.exec_driver_sql('BEGIN')
        assert reader.execute(text('SELECT value FROM demo')).scalar() == 'before'
        def write():
            with engine.begin() as writer:
                writer.execute(text("UPDATE demo SET value='after' WHERE id=1"))
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(write).result(timeout=3)
        assert reader.execute(text('SELECT value FROM demo')).scalar() == 'before'
        reader.rollback()
        assert reader.execute(text('SELECT value FROM demo')).scalar() == 'after'
    engine.dispose()


def test_busy_error_classification_and_invalid_config(tmp_path, monkeypatch):
    path = tmp_path/'busy.db'
    first = sqlite3.connect(path)
    second = sqlite3.connect(path, timeout=0)
    try:
        first.execute('CREATE TABLE demo (id INTEGER)')
        first.execute('INSERT INTO demo VALUES (1)')
        with pytest.raises(sqlite3.OperationalError) as info:
            second.execute('INSERT INTO demo VALUES (2)')
        assert is_lock_error(info.value)
        assert not is_lock_error(ValueError('not a database failure'))
    finally:
        first.close(); second.close()
    monkeypatch.setenv('NEWS2DOUYIN_SQLITE_BUSY_TIMEOUT_MS', '-1')
    with pytest.raises(ValueError, match='BUSY_TIMEOUT'):
        make_engine('sqlite:///'+str(path))


@pytest.mark.parametrize('database',['sqlite','postgresql'])
def test_pool_capacity_and_wait_are_environment_configuration(tmp_path,monkeypatch,database):
    monkeypatch.setenv('NEWS2DOUYIN_DB_POOL_SIZE','30')
    monkeypatch.setenv('NEWS2DOUYIN_DB_MAX_OVERFLOW','60')
    monkeypatch.setenv('NEWS2DOUYIN_DB_POOL_TIMEOUT','10')
    url='sqlite:///'+str(tmp_path/'capacity.db') if database=='sqlite' else 'postgresql://user:secret@127.0.0.1:1/db'
    engine=make_engine(url)
    try:
        assert isinstance(engine.pool,QueuePool)
        assert engine.pool.size()==30 and engine.pool._max_overflow==60
        assert engine.pool.timeout()==10 and engine.hide_parameters
    finally:engine.dispose()


def test_every_sqlite_connection_has_the_same_busy_timeout_and_wal_is_set_once(tmp_path,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_SQLITE_BUSY_TIMEOUT_MS','4321')
    monkeypatch.setenv('NEWS2DOUYIN_DB_POOL_SIZE','2')
    monkeypatch.setenv('NEWS2DOUYIN_DB_MAX_OVERFLOW','1')
    statements=[];original=db.create_engine
    def traced_engine(*args,**kwargs):
        engine=original(*args,**kwargs)
        @event.listens_for(engine,'connect')
        def trace(connection,_record):connection.set_trace_callback(statements.append)
        return engine
    monkeypatch.setattr(db,'create_engine',traced_engine)
    engine=make_engine('sqlite:///'+str(tmp_path/'connections.db'))
    try:
        with ExitStack() as stack:
            connections=[stack.enter_context(engine.connect()) for _ in range(3)]
            assert all(c.exec_driver_sql('PRAGMA busy_timeout').scalar()==4321 for c in connections)
            assert all(c.exec_driver_sql('PRAGMA journal_mode').scalar()=='wal' for c in connections)
        assert sum(sql.upper()=='PRAGMA JOURNAL_MODE=WAL' for sql in statements)==1
        assert sum(sql.upper()=='PRAGMA BUSY_TIMEOUT=4321' for sql in statements)==3
    finally:engine.dispose()


def test_pool_timeout_zero_fails_immediately_when_configured_capacity_is_used(tmp_path,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_DB_POOL_SIZE','1')
    monkeypatch.setenv('NEWS2DOUYIN_DB_MAX_OVERFLOW','0')
    monkeypatch.setenv('NEWS2DOUYIN_DB_POOL_TIMEOUT','0')
    engine=make_engine('sqlite:///'+str(tmp_path/'one-connection.db'))
    try:
        with engine.connect():
            with pytest.raises(TimeoutError):engine.connect()
    finally:engine.dispose()


@pytest.mark.parametrize('url',['sqlite://','sqlite:///:memory:'])
def test_memory_sqlite_keeps_its_existing_pool_semantics_with_pool_env_present(monkeypatch,url):
    monkeypatch.setenv('NEWS2DOUYIN_DB_POOL_SIZE','30')
    monkeypatch.setenv('NEWS2DOUYIN_DB_MAX_OVERFLOW','60')
    monkeypatch.setenv('NEWS2DOUYIN_DB_POOL_TIMEOUT','10')
    engine=make_engine(url)
    try:
        assert isinstance(engine.pool,SingletonThreadPool)
        with engine.begin() as connection:
            connection.exec_driver_sql('CREATE TABLE ephemeral (value TEXT)')
            connection.exec_driver_sql("INSERT INTO ephemeral VALUES ('retained')")
        with engine.connect() as connection:
            assert connection.exec_driver_sql('SELECT value FROM ephemeral').scalar()=='retained'
            assert connection.exec_driver_sql('PRAGMA busy_timeout').scalar()==30000
    finally:engine.dispose()


def test_empty_configuration_fields_use_database_specific_defaults(tmp_path,monkeypatch):
    for name in ('NEWS2DOUYIN_DB_POOL_SIZE','NEWS2DOUYIN_DB_MAX_OVERFLOW',
                 'NEWS2DOUYIN_DB_POOL_TIMEOUT','NEWS2DOUYIN_SQLITE_BUSY_TIMEOUT_MS'):
        monkeypatch.setenv(name,'  ')
    sqlite=make_engine('sqlite:///'+str(tmp_path/'defaults.db'))
    pg=make_engine('postgresql://user@127.0.0.1:1/db')
    try:
        assert sqlite.pool.size()==5 and sqlite.pool._max_overflow==10
        assert pg.pool.size()==8 and pg.pool._max_overflow==8
        assert sqlite.pool.timeout()==pg.pool.timeout()==10
        with sqlite.connect() as connection:assert connection.exec_driver_sql('PRAGMA busy_timeout').scalar()==30000
    finally:sqlite.dispose();pg.dispose()


def test_postgres_maintenance_keeps_migration_timeouts_without_connecting(monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_DB_LOCK_TIMEOUT_MS','1234')
    monkeypatch.setenv('NEWS2DOUYIN_DB_STATEMENT_TIMEOUT_MS','7890')
    captured=[];original=db.create_engine
    def capture(*args,**kwargs):captured.append(kwargs);return original(*args,**kwargs)
    monkeypatch.setattr(db,'create_engine',capture)
    online=make_engine('postgresql://user@127.0.0.1:1/db')
    maintenance=make_engine('postgresql://user@127.0.0.1:1/db',maintenance=True)
    try:
        assert captured[0]['connect_args']['options']=='-c lock_timeout=1234 -c statement_timeout=7890 -c idle_in_transaction_session_timeout=60000'
        assert captured[1]['connect_args']['options']=='-c lock_timeout=30000 -c statement_timeout=0 -c idle_in_transaction_session_timeout=0'
        assert all(config['pool_pre_ping'] and config['connect_args']['connect_timeout']==10 for config in captured)
    finally:online.dispose();maintenance.dispose()


@pytest.mark.parametrize('name,value',[
    ('NEWS2DOUYIN_DB_POOL_SIZE','0'),('NEWS2DOUYIN_DB_POOL_SIZE','65'),
    ('NEWS2DOUYIN_DB_MAX_OVERFLOW','-1'),('NEWS2DOUYIN_DB_MAX_OVERFLOW','65'),
    ('NEWS2DOUYIN_DB_POOL_TIMEOUT','-1'),('NEWS2DOUYIN_DB_POOL_TIMEOUT','301'),
    ('NEWS2DOUYIN_DB_POOL_TIMEOUT','invalid'),('NEWS2DOUYIN_SQLITE_BUSY_TIMEOUT_MS','60001'),
])
def test_invalid_pool_and_busy_configuration_is_rejected_without_echoing_values(tmp_path,monkeypatch,name,value):
    monkeypatch.setenv(name,value)
    with pytest.raises(ValueError,match=name):make_engine('sqlite:///'+str(tmp_path/'invalid.db'))


def test_sqlite_error_messages_hide_bound_parameters():
    engine=make_engine('sqlite:///:memory:')
    try:
        with engine.connect() as connection:
            with pytest.raises(OperationalError) as info:
                connection.execute(text('SELECT :private + missing_column'),{'private':'private-source-content'})
        assert 'private-source-content' not in str(info.value)
    finally:engine.dispose()
