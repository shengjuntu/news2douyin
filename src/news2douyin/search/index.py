"""SQLite FTS5 trigram indexes, maintained by transactional DB triggers."""
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

TABLES = {'article': ('title', 'content', 'source_domain'),
          'event': ('event_title', 'summary', 'topic')}


def init_search(engine) -> None:
    if engine.dialect.name != 'sqlite':
        return
    with engine.begin() as conn:
        try:
            conn.exec_driver_sql("CREATE VIRTUAL TABLE IF NOT EXISTS news_fts_probe USING fts5(value, tokenize='trigram')")
        except OperationalError as exc:
            if 'no such module' in str(exc) or 'no such tokenizer' in str(exc):
                return  # Older SQLite remains functional with literal SQL search.
            raise
        conn.exec_driver_sql('DROP TABLE news_fts_probe')
        for table, columns in TABLES.items():
            index = f'{table}_fts'
            existed = conn.execute(text("SELECT 1 FROM sqlite_master WHERE name=:name"), {'name': index}).first()
            fields = ', '.join(columns)
            new = ', '.join(f'new.{c}' for c in columns)
            old = ', '.join(f'old.{c}' for c in columns)
            conn.exec_driver_sql(f"CREATE VIRTUAL TABLE IF NOT EXISTS {index} USING fts5({fields}, content='{table}', content_rowid='id', tokenize='trigram')")
            conn.exec_driver_sql(f"CREATE TRIGGER IF NOT EXISTS {index}_insert AFTER INSERT ON {table} BEGIN INSERT INTO {index}(rowid, {fields}) VALUES (new.id, {new}); END")
            conn.exec_driver_sql(f"CREATE TRIGGER IF NOT EXISTS {index}_delete AFTER DELETE ON {table} BEGIN INSERT INTO {index}({index}, rowid, {fields}) VALUES ('delete', old.id, {old}); END")
            conn.exec_driver_sql(f"CREATE TRIGGER IF NOT EXISTS {index}_update AFTER UPDATE ON {table} BEGIN INSERT INTO {index}({index}, rowid, {fields}) VALUES ('delete', old.id, {old}); INSERT INTO {index}(rowid, {fields}) VALUES (new.id, {new}); END")
            if not existed:
                conn.exec_driver_sql(f"INSERT INTO {index}({index}) VALUES ('rebuild')")


def index_available(session) -> bool:
    if session.bind.dialect.name != 'sqlite':
        return False
    return bool(session.execute(text("SELECT 1 FROM sqlite_master WHERE name='article_fts'")).first())
