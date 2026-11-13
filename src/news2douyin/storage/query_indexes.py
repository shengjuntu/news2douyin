"""Explicit maintenance for existing databases; startup never rebuilds old tables."""
import argparse
import json

from sqlalchemy import inspect, text

NAME = 'ix_articleversion_article_latest_id'
COLUMNS = ['article_key', 'id']
READ_INDEXES = (
    ('articleversion', NAME, COLUMNS),
    ('article', 'ix_article_read_provider', ['article_key', 'provider']),
    ('articleeventlink', 'ix_articleeventlink_read_sources', ['event_key', 'article_key']),
    ('radarentry', 'ix_radarentry_read_scope', ['key', 'seen', 'priority', 'band']),
)


def ensure_latest_index(engine):
    return _ensure(engine, 'articleversion', NAME, COLUMNS)


def _ensure(engine, table, name, columns):
    before = {row['name']: row for row in inspect(engine).get_indexes(table)}
    if name in before:
        row = before[name]
        options = row.get('dialect_options', {})
        different = (row['column_names'] != columns or bool(row.get('unique')) or
            any(options.get(key) is not None for key in ('sqlite_where', 'postgresql_where')) or
            options.get('postgresql_using') not in (None, 'btree') or
            bool(options.get('postgresql_include')))
        if different:
            raise RuntimeError('Query index name is occupied by a different definition; review it manually')
    created = name not in before
    if created:
        concurrent = ' CONCURRENTLY' if engine.dialect.name == 'postgresql' else ''
        # PG concurrent index builds must be outside a transaction; failure can
        # leave an invalid index. We report that state and never silently drop it.
        with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as c:
            fields = ', '.join('"' + column + '"' for column in columns)
            c.execute(text(f'CREATE INDEX{concurrent} IF NOT EXISTS "{name}" ON "{table}" ({fields})'))
    if engine.dialect.name == 'postgresql':
        with engine.connect() as c:
            valid = c.execute(text('''SELECT i.indisvalid AND i.indisready FROM pg_index i
                JOIN pg_class x ON x.oid=i.indexrelid JOIN pg_namespace n ON n.oid=x.relnamespace
                WHERE n.nspname=current_schema() AND x.relname=:name'''), {'name': name}).scalar_one()
            if not valid:
                raise RuntimeError('Query index is invalid; review the failed build before retrying')
    return dict(index=name, table=table, columns=columns, created=created, valid=True)


def ensure_read_indexes(engine, *, analyze=False):
    indexes = [_ensure(engine, *spec) for spec in READ_INDEXES]
    analyzed = []
    if analyze:
        # Explicit maintenance only: SQLite scans just these small indexes;
        # PostgreSQL samples the corresponding tables for its planner statistics.
        targets = [name for _, name, _ in READ_INDEXES] if engine.dialect.name == 'sqlite' else list(dict.fromkeys(table for table, _, _ in READ_INDEXES))
        with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as c:
            for target in targets:
                c.execute(text('ANALYZE "' + target + '"'))
                analyzed.append(target)
    return dict(indexes=indexes, analyzed=analyzed)


def main(argv=None):
    from ..config import load_environment
    from .db import make_engine, default_db_url
    load_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db-url', default=None)
    parser.add_argument('--apply', action='store_true', help='Create the query index; SQLite needs a writer maintenance window')
    parser.add_argument('--analyze', action='store_true', help='With --apply, update statistics for the managed indexes/tables')
    args = parser.parse_args(argv)
    if not args.apply:
        print(json.dumps(dict(status='plan', indexes=[dict(table=t,index=n,columns=c) for t,n,c in READ_INDEXES],analyze=args.analyze,
            note='No database connection opened. Use --apply during an appropriate maintenance window.')))
        return 0
    engine = None
    try:
        engine = make_engine(args.db_url or default_db_url(), maintenance=True)
        result = ensure_read_indexes(engine, analyze=args.analyze)
        print(json.dumps(dict(status='ready', **result)))
        return 0
    except Exception as exc:
        print(json.dumps(dict(status='failed', error=type(exc).__name__,
            note='Review the index and database state; no data or existing index was deleted.')))
        return 1
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
