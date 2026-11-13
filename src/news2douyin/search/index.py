"""Transactional SQLite FTS5 and PostgreSQL literal-compatible trigram indexes."""
import logging
import os
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from ..storage.sql import install_sql_functions

log=logging.getLogger(__name__)
TABLES={'article':('title','content','source_domain'), 'event':('event_title','summary','topic')}
PG_INDEXES=tuple(f'n2d_{table}_{column}_trgm' for table,columns in TABLES.items() for column in columns)


def init_search(engine,*,build_indexes=True):
    with engine.begin() as conn:
        install_sql_functions(conn)
        if engine.dialect.name=='postgresql':
            auto_create=build_indexes and os.getenv('NEWS2DOUYIN_PG_TRGM_AUTO_CREATE','1')!='0'
            engine._n2d_search_warning=''
            if auto_create:
                try:
                    # A permissions error must not poison the surrounding schema transaction.
                    with conn.begin_nested():
                        conn.exec_driver_sql('CREATE EXTENSION IF NOT EXISTS pg_trgm')
                        for table,columns in TABLES.items():
                            for column in columns:
                                conn.exec_driver_sql(f'CREATE INDEX IF NOT EXISTS n2d_{table}_{column}_trgm ON "{table}" USING gin (lower("{column}") gin_trgm_ops)')
                except SQLAlchemyError as exc:
                    engine._n2d_search_warning='PostgreSQL pg_trgm 索引未能建立，当前使用字面文本扫描 · '+type(exc).__name__
                    log.warning('%s',engine._n2d_search_warning)
            else:
                engine._n2d_search_warning='PostgreSQL pg_trgm 自动建索引已关闭；已有索引仍可使用。'
            return
        if engine.dialect.name!='sqlite':raise ValueError('Unsupported search database')
        try:
            conn.exec_driver_sql("CREATE VIRTUAL TABLE IF NOT EXISTS news_fts_probe USING fts5(value, tokenize='trigram')")
        except OperationalError as exc:
            if 'no such module' in str(exc) or 'no such tokenizer' in str(exc):return
            raise
        conn.exec_driver_sql('DROP TABLE news_fts_probe')
        for table,columns in TABLES.items():
            index=f'{table}_fts'
            existed=conn.execute(text("SELECT 1 FROM sqlite_master WHERE name=:name"),{'name':index}).first()
            fields=', '.join(columns);new=', '.join(f'new.{c}' for c in columns);old=', '.join(f'old.{c}' for c in columns)
            conn.exec_driver_sql(f"CREATE VIRTUAL TABLE IF NOT EXISTS {index} USING fts5({fields}, content='{table}', content_rowid='id', tokenize='trigram')")
            conn.exec_driver_sql(f"CREATE TRIGGER IF NOT EXISTS {index}_insert AFTER INSERT ON {table} BEGIN INSERT INTO {index}(rowid,{fields}) VALUES(new.id,{new}); END")
            conn.exec_driver_sql(f"CREATE TRIGGER IF NOT EXISTS {index}_delete AFTER DELETE ON {table} BEGIN INSERT INTO {index}({index},rowid,{fields}) VALUES('delete',old.id,{old}); END")
            changed=' OR '.join(f'new.{c} IS NOT old.{c}' for c in ('id',*columns))
            conn.exec_driver_sql(f'DROP TRIGGER IF EXISTS {index}_update')
            conn.exec_driver_sql(f"CREATE TRIGGER {index}_update AFTER UPDATE ON {table} WHEN {changed} BEGIN INSERT INTO {index}({index},rowid,{fields}) VALUES('delete',old.id,{old}); INSERT INTO {index}(rowid,{fields}) VALUES(new.id,{new}); END")
            if not existed:conn.exec_driver_sql(f"INSERT INTO {index}({index}) VALUES('rebuild')")


def index_available(session):
    """True for SQLite FTS5, 'pg_trgm' for ready PG indexes, False for scans."""
    if session.bind.dialect.name=='postgresql':
        # Verify usable indexes, rather than claiming pg_trgm solely because an
        # extension exists. Queries remain semantically identical on fallback.
        names=','.join("'"+name+"'" for name in PG_INDEXES)
        available=session.execute(text(f"""SELECT count(*) FROM pg_index i
            JOIN pg_class idx ON idx.oid=i.indexrelid JOIN pg_class tbl ON tbl.oid=i.indrelid
            JOIN pg_namespace n ON n.oid=tbl.relnamespace
            WHERE n.nspname=current_schema() AND idx.relname IN ({names})
            AND i.indisvalid AND i.indisready""")).scalar_one()
        return 'pg_trgm' if available==len(PG_INDEXES) else False
    if session.bind.dialect.name!='sqlite':return False
    return bool(session.execute(text("SELECT 1 FROM sqlite_master WHERE name='article_fts'")).first())
