"""Streaming migration is exercised against a real disposable PostgreSQL database."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4
import pytest
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from sqlmodel import SQLModel,Session,select
from news2douyin.storage.db import init_db,load_models,make_engine
from news2douyin.desk.models import WatchEvent
from news2douyin.storage.models import Article,ArticleVersion,CollectProfile,ProfileState,TaskRecord,NewsWriteLock
from news2douyin.storage.migrate import MigrationError,migrate_sqlite,readonly_source,source_fingerprint


@pytest.fixture
def target():
    raw=os.getenv('NEWS2DOUYIN_TEST_POSTGRES_URL')
    if not raw:pytest.skip('requires NEWS2DOUYIN_TEST_POSTGRES_URL and real PostgreSQL')
    name='n2d_migration_'+uuid4().hex
    admin=create_engine(raw,isolation_level='AUTOCOMMIT')
    with admin.connect() as c:c.execute(text('CREATE DATABASE "'+name+'"'))
    url=make_url(raw).set(database=name).render_as_string(hide_password=False)
    try:yield url
    finally:
        with admin.connect() as c:c.execute(text('DROP DATABASE "'+name+'" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
def source(tmp_path):
    path=tmp_path/'source.sqlite';load_models()
    # Prepare an offline source, not the runtime WAL configuration.
    engine=create_engine('sqlite:///'+str(path));SQLModel.metadata.create_all(engine)
    # Emulate the v0.22 source which predates the new PostgreSQL graph lease table.
    with engine.begin() as c:c.execute(text('DROP TABLE IF EXISTS graphlease'))
    raw=' { "value" : "原文 Ω", "items": [1, 2] }\n'
    with Session(engine) as s:
        s.add(NewsWriteLock(name='collection'))
        for watch,event in [('W','é'),('W','A'),('W','中国'),('a','Z')]:s.add(WatchEvent(watch=watch,event=event))
        for i,title in enumerate(['Zebra','中国','éclair','a','Aa','z'],1):
            s.add(Article(id=i*10,article_key=title,title=title,content='frozen '+title,raw_json=raw))
            s.add(ArticleVersion(id=500+i,article_key=title,revision=3,content_hash='hash-'+title,payload_json=raw,origin='original'))
        s.add(CollectProfile(id=17,name='existing',provider='gkg',extra_json=raw))
        s.add(ProfileState(name='existing',enabled=False))
        s.add(TaskRecord(task_id='task_frozen',profile_name='existing',profile_json=raw,request_hash='fixed',status='queued',lease_until=0))
        s.commit()
    engine.dispose()
    return path


def test_migrate_preserves_all_rows_bool_json_versions_and_serial_ids(source,target):
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    report=migrate_sqlite(source,target,chunk_size=2,source_stopped=True)
    assert report['status']=='ready' and report['source_unchanged']
    tables={r['table']:r for r in report['tables']}
    assert tables['article']['source_rows']==6 and tables['articleversion']['target_rows']==6
    assert tables['watchevent']['source_rows']==4
    if 'graphlease' in tables:assert not tables['graphlease']['source_table_present'] and tables['graphlease']['target_rows']==0
    assert all(r['source_sha256']==r['target_sha256'] for r in report['tables'])
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before
    engine=make_engine(target)
    with Session(engine) as s:
        assert s.get(ProfileState,'existing').enabled is False
        assert s.exec(select(ArticleVersion).where(ArticleVersion.id==501)).one().revision==3
        assert s.exec(select(Article).where(Article.id==10)).one().raw_json==' { "value" : "原文 Ω", "items": [1, 2] }\n'
        new=Article(article_key='new',title='next');s.add(new);s.commit();s.refresh(new)
        assert new.id>60
    with engine.connect() as c:
        assert c.execute(text('SELECT phase FROM _n2d_sqlite_migration')).scalar()=='ready'
        # Search/desk triggers were installed only after verified copying.
        assert c.execute(text("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal")).scalar()>0
    engine.dispose()


def test_failure_after_committed_chunk_resumes_by_pk_without_duplicates(source,target):
    def interrupt(table,rows):
        if table=='article' and rows>=2:raise RuntimeError('simulated process interruption')
    with pytest.raises(MigrationError):migrate_sqlite(source,target,chunk_size=2,source_stopped=True,progress=interrupt)
    engine=make_engine(target)
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM article')).scalar()==2
        assert c.execute(text("SELECT rows_copied FROM _n2d_sqlite_migration_table WHERE name='article'")).scalar()==2
        assert c.execute(text('SELECT phase FROM _n2d_sqlite_migration')).scalar()=='failed'
    with pytest.raises(RuntimeError,match='migration is incomplete'):init_db(engine)
    engine.dispose()
    report=migrate_sqlite(source,target,chunk_size=1,source_stopped=True,resume=True)
    assert next(r for r in report['tables'] if r['table']=='article')['target_rows']==6
    engine=make_engine(target)
    init_db(engine)
    with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM article')).scalar()==6
    engine.dispose()


def test_refuses_existing_target_and_never_prints_password(source,target):
    engine=make_engine(target)
    with engine.begin() as c:c.execute(text('CREATE TABLE unrelated(value TEXT)'));c.execute(text("INSERT INTO unrelated VALUES ('keep')"))
    with pytest.raises(MigrationError,match='empty'):migrate_sqlite(source,target,source_stopped=True)
    with engine.connect() as c:assert c.execute(text('SELECT value FROM unrelated')).scalar()=='keep'
    engine.dispose()
    with pytest.raises(MigrationError) as caught:migrate_sqlite(source,'sqlite:///password-secret',source_stopped=True)
    assert 'password-secret' not in str(caught.value)


def test_stopped_confirmation_and_nonempty_wal_are_required(tmp_path):
    path=tmp_path/'source.sqlite';sqlite3.connect(path).close()
    with pytest.raises(MigrationError,match='source-stopped'):migrate_sqlite(path,'postgresql://unused')
    Path(str(path)+'-wal').write_bytes(b'not yet checkpointed')
    with pytest.raises(MigrationError,match='nonempty'):source_fingerprint(path)


def test_empty_sidecar_identity_is_stable_but_nonempty_changes_are_rejected(tmp_path):
    from news2douyin.storage.migrate import _assert_unchanged
    path=tmp_path/'source.sqlite'
    with sqlite3.connect(path) as c:c.execute('CREATE TABLE evidence (id INTEGER PRIMARY KEY)')
    before=source_fingerprint(path)
    with readonly_source(path) as c:
        version=c.execute('PRAGMA data_version').fetchone()[0]
        for suffix in ('-wal','-journal'):
            side=Path(str(path)+suffix);side.touch()
            assert source_fingerprint(path)==before
            _assert_unchanged(path,before,c,version)
            side.write_bytes(b'changed')
            with pytest.raises(MigrationError,match='changed'):_assert_unchanged(path,before,c,version)
            with pytest.raises(MigrationError,match='nonempty'):source_fingerprint(path)
            side.unlink()


def test_source_is_readonly_and_no_statement_can_modify_it(source):
    before=source.read_bytes()
    with readonly_source(source) as c:
        with pytest.raises(sqlite3.OperationalError):c.execute("UPDATE article SET title='wrong'")
    assert source.read_bytes()==before


def test_clean_runtime_wal_source_migrates_without_changing_source_bytes(tmp_path,target):
    from news2douyin.server.app import create_app
    from news2douyin.intake.projection import import_document
    path=tmp_path/'runtime-wal.sqlite'
    app=create_app(db_url='sqlite:///'+str(path),storage_root=str(tmp_path/'runs'))
    imported=import_document(app.state.engine,dict(provider='migration.fixture',record_id='1',
        uri='https://example.com/clean-wal-migration',title='WAL original',content='Immutable source text'))
    app.state.engine.dispose()
    assert not Path(str(path)+'-wal').exists()
    before=path.read_bytes();fingerprint=source_fingerprint(path)
    with readonly_source(path):
        # SQLite may create empty WAL / shared-memory bookkeeping even for a
        # read-only connection. Neither carries an uncheckpointed transaction.
        assert source_fingerprint(path)==fingerprint
    report=migrate_sqlite(path,target,chunk_size=1,source_stopped=True)
    assert report['status']=='ready' and report['source_unchanged']
    assert path.read_bytes()==before and source_fingerprint(path)==fingerprint
    destination=make_engine(target)
    with Session(destination) as s:
        article=s.exec(select(Article).where(Article.article_key==imported['article_key'])).one()
        assert article.content=='Immutable source text'
    destination.dispose()


def test_resume_rejects_changed_source_and_tampered_target(source,target):
    def interrupt(table,rows):
        if table=='article' and rows>=2:raise RuntimeError('fixture stop')
    with pytest.raises(MigrationError):migrate_sqlite(source,target,chunk_size=2,source_stopped=True,progress=interrupt)
    with sqlite3.connect(source) as c:c.execute("UPDATE article SET content='changed' WHERE id=10")
    with pytest.raises(MigrationError,match='identity'):migrate_sqlite(source,target,source_stopped=True,resume=True)


def test_verification_catches_target_row_edit_before_resume(source,target):
    def interrupt(table,rows):
        if table=='article' and rows>=2:raise RuntimeError('fixture stop')
    with pytest.raises(MigrationError):migrate_sqlite(source,target,chunk_size=2,source_stopped=True,progress=interrupt)
    engine=make_engine(target)
    with engine.begin() as c:c.execute(text("UPDATE article SET content='tampered' WHERE id=10"))
    with pytest.raises(MigrationError,match='verification'):migrate_sqlite(source,target,source_stopped=True,resume=True)
    with engine.connect() as c:assert c.execute(text('SELECT phase FROM _n2d_sqlite_migration')).scalar()=='failed'
    engine.dispose()


def test_transaction_failure_rolls_back_insert_and_checkpoint_together(source,target,monkeypatch):
    from news2douyin.storage import migrate
    original=migrate._save_progress;injected=[]
    def fail(connection,name,last_pk,count,*args,**kwargs):
        if name=='article' and count:
            injected.append(count)
            raise RuntimeError('checkpoint write interrupted')
        return original(connection,name,last_pk,count,*args,**kwargs)
    monkeypatch.setattr(migrate,'_save_progress',fail)
    # An unrelated setup/storage failure must not masquerade as the intended
    # checkpoint interruption and produce a misleading "article missing" error.
    with pytest.raises(MigrationError,match='failure type RuntimeError'):
        migrate_sqlite(source,target,chunk_size=2,source_stopped=True)
    assert injected==[2]
    engine=make_engine(target)
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM article')).scalar()==0
        assert c.execute(text("SELECT count(*) FROM _n2d_sqlite_migration_table WHERE name='article'")).scalar()==0
        assert c.execute(text('SELECT phase FROM _n2d_sqlite_migration')).scalar()=='failed'
    engine.dispose()
    monkeypatch.setattr(migrate,'_save_progress',original)
    assert migrate_sqlite(source,target,source_stopped=True,resume=True)['status']=='ready'


def test_derived_fts_is_excluded_but_unknown_business_table_is_never_dropped(source,target):
    with sqlite3.connect(source) as c:
        c.execute('CREATE VIRTUAL TABLE article_fts USING fts5(body)');c.execute("INSERT INTO article_fts VALUES ('derived cache')")
    report=migrate_sqlite(source,target,source_stopped=True)
    assert 'article_fts' in report['excluded_derived_tables'] and 'article_fts_data' in report['excluded_derived_tables']
    with sqlite3.connect(source) as c:c.execute('CREATE TABLE article_fts_custom_business(value TEXT)')
    with pytest.raises(MigrationError,match='outside loaded'):migrate_sqlite(source,target,source_stopped=True)


def test_connection_errors_are_redacted(source,monkeypatch):
    from news2douyin.storage import db
    def broken(*args,**kwargs):raise RuntimeError('postgresql://name:super-secret-password@private/db')
    monkeypatch.setattr(db,'make_engine',broken)
    with pytest.raises(MigrationError) as caught:migrate_sqlite(source,'postgresql://name:super-secret-password@private/db',source_stopped=True)
    assert 'super-secret' not in str(caught.value) and 'private' not in str(caught.value)


def test_cli_report_never_overwrites_the_source_file(source,capsys):
    from news2douyin.storage.migrate import main
    before=source.read_bytes()
    assert main(['--source',str(source),'--target-url','postgresql://unused',
                 '--source-stopped','--report-file',str(source)])==1
    assert source.read_bytes()==before
    assert 'must not overwrite' in capsys.readouterr().out


def test_target_with_business_data_in_another_schema_is_rejected(source,target):
    engine=make_engine(target)
    with engine.begin() as c:
        c.execute(text('CREATE SCHEMA tenant'));c.execute(text('CREATE TABLE tenant.existing(value TEXT)'))
        c.execute(text("INSERT INTO tenant.existing VALUES ('keep')"))
    with pytest.raises(MigrationError,match='dedicated empty'):migrate_sqlite(source,target,source_stopped=True)
    with engine.connect() as c:assert c.execute(text('SELECT value FROM tenant.existing')).scalar()=='keep'
    engine.dispose()


def test_autoincrement_highwater_is_preserved_even_after_deleted_ids(tmp_path,target):
    load_models();table=Article.__table__;old=table.dialect_options['sqlite']['autoincrement']
    table.dialect_options['sqlite']['autoincrement']=True
    source=tmp_path/'highwater.sqlite';engine=create_engine('sqlite:///'+str(source))
    try:table.create(engine)
    finally:table.dialect_options['sqlite']['autoincrement']=old
    with Session(engine) as s:
        removed=Article(id=100,article_key='deleted',title='deleted');s.add(removed);s.commit();s.delete(removed);s.commit()
        s.add(Article(id=1,article_key='kept',title='kept'));s.commit()
    engine.dispose()
    migrate_sqlite(source,target,source_stopped=True)
    destination=make_engine(target)
    with Session(destination) as s:
        added=Article(article_key='new',title='new');s.add(added);s.commit();s.refresh(added)
        assert added.id>100
    destination.dispose()
