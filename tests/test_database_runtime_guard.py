"""Formal runtime configuration must never fall back to an old SQLite file."""
import hashlib

import pytest
from sqlalchemy import text

from news2douyin.storage.db import default_db_url, make_engine


def test_missing_production_url_fails_without_touching_legacy_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    legacy = tmp_path/'runs_v7'/'news2douyin_v7.db'
    legacy.parent.mkdir()
    legacy.write_bytes(b'untouched archived database fixture')
    before = hashlib.sha256(legacy.read_bytes()).hexdigest()
    monkeypatch.delenv('NEWS2DOUYIN_DATABASE_URL', raising=False)
    monkeypatch.setenv('NEWS2DOUYIN_DATABASE_REQUIRE_POSTGRES', '1')
    with pytest.raises(ValueError, match='must be a PostgreSQL URL'):
        make_engine()
    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == before
    assert not legacy.with_name(legacy.name+'-wal').exists()
    assert not legacy.with_name(legacy.name+'-shm').exists()


@pytest.mark.parametrize('configured', ['', 'sqlite:///old.db', 'mysql://private:secret@host/db',
                                         'this is not a URL'])
def test_guard_rejects_non_postgres_defaults_without_revealing_credentials(configured, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_DATABASE_REQUIRE_POSTGRES', 'true')
    monkeypatch.setenv('NEWS2DOUYIN_DATABASE_URL', configured)
    with pytest.raises(ValueError) as info:
        default_db_url()
    assert 'DATABASE_URL' in str(info.value)
    assert 'secret' not in str(info.value)
    assert configured not in str(info.value) if configured else True


@pytest.mark.parametrize('configured', ['postgres://user:secret@localhost/db',
                                         'postgresql://user:secret@localhost/db',
                                         'postgresql+psycopg://user:secret@localhost/db'])
def test_guard_accepts_postgres_without_opening_a_connection(configured, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_DATABASE_REQUIRE_POSTGRES', 'yes')
    monkeypatch.setenv('NEWS2DOUYIN_DATABASE_URL', configured)
    assert default_db_url() == configured
    engine = make_engine()
    try:
        assert engine.dialect.name == 'postgresql'
        assert 'secret' not in str(engine.url)
    finally:
        engine.dispose()


def test_local_development_default_and_explicit_offline_source_remain_available(tmp_path, monkeypatch):
    monkeypatch.delenv('NEWS2DOUYIN_DATABASE_REQUIRE_POSTGRES', raising=False)
    monkeypatch.delenv('NEWS2DOUYIN_DATABASE_URL', raising=False)
    assert default_db_url() == 'sqlite:///runs_v7/news2douyin_v7.db'
    # An explicit SQLite address is used by the offline migration tooling.
    monkeypatch.setenv('NEWS2DOUYIN_DATABASE_REQUIRE_POSTGRES', '1')
    engine = make_engine('sqlite:///'+str(tmp_path/'offline-source.db'))
    try:
        with engine.connect() as connection:
            assert connection.execute(text('SELECT 1')).scalar() == 1
    finally:
        engine.dispose()


def test_invalid_guard_setting_fails_clearly(monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_DATABASE_REQUIRE_POSTGRES', 'perhaps')
    with pytest.raises(ValueError, match='must be a boolean'):
        default_db_url()


def test_blank_guard_is_the_local_development_default(monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_DATABASE_REQUIRE_POSTGRES', ' ')
    monkeypatch.delenv('NEWS2DOUYIN_DATABASE_URL', raising=False)
    assert default_db_url() == 'sqlite:///runs_v7/news2douyin_v7.db'
