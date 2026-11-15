"""Remove only the two obsolete, code-owned whole-text B-tree indexes.

No source text changes, table scans, replacements or background rebuilds are
performed. Existing equality identity lookup on canonical_url is deliberately
outside this migration. The complete definition is checked before any DDL;
schema drift fails startup instead of silently deleting an operator's index.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

log = logging.getLogger(__name__)


class IndexSafetyError(RuntimeError):
    pass


@dataclass(frozen=True)
class LegacyIndex:
    name: str
    table: str
    column: str


LEGACY_INDEXES = (
    LegacyIndex('ix_event_event_title', 'event', 'event_title'),
    LegacyIndex('ix_article_source_domain', 'article', 'source_domain'),
)


def _mismatch(spec):
    return IndexSafetyError(
        f'文本索引安全迁移已停止：{spec.name} 与旧版定义不一致；'
        '请核对索引和约束定义后再启动，未自动删除自定义索引。')


def _pg_definitions(connection, schema):
    names = [spec.name for spec in LEGACY_INDEXES]
    rows = connection.execute(text('''
        SELECT x.relname AS name, x.relkind AS relation_kind,
               t.relname AS table_name, t.relkind AS table_kind, tn.nspname AS table_schema,
               a.attname AS column_name, am.amname AS method,
               i.indisunique, i.indisprimary, i.indisexclusion,
               i.indnatts, i.indnkeyatts, i.indisvalid, i.indisready,
               i.indislive, i.indisreplident, i.indisclustered,
               i.indexprs IS NULL AS no_expression,
               i.indpred IS NULL AS no_predicate,
               i.indoption[0] AS column_options,
               i.indcollation[0] = a.attcollation AS default_collation,
               opc.opcdefault AS default_operator_class,
               x.reloptions IS NULL AS default_storage_options,
               NOT EXISTS (SELECT 1 FROM pg_constraint c
                   WHERE c.conindid = x.oid) AS no_constraint
        FROM pg_class x
        JOIN pg_namespace n ON n.oid=x.relnamespace
        LEFT JOIN pg_index i ON i.indexrelid=x.oid
        LEFT JOIN pg_class t ON t.oid=i.indrelid
        LEFT JOIN pg_namespace tn ON tn.oid=t.relnamespace
        LEFT JOIN pg_am am ON am.oid=x.relam
        LEFT JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=i.indkey[0]
        LEFT JOIN pg_opclass opc ON opc.oid=i.indclass[0]
        WHERE n.nspname=:schema AND x.relname IN (:first,:second)
    '''), dict(schema=schema, first=names[0], second=names[1])).mappings().all()
    return {row['name']: row for row in rows}


def _check_pg(spec, row, schema):
    required_true = ('indisvalid', 'indisready', 'indislive', 'no_expression',
                     'no_predicate', 'default_collation', 'default_operator_class',
                     'default_storage_options', 'no_constraint')
    if (row['relation_kind'] != 'i' or row['table_kind'] != 'r' or row['table_name'] != spec.table
            or row['table_schema'] != schema or row['column_name'] != spec.column
            or row['method'] != 'btree' or row['indnatts'] != 1 or row['indnkeyatts'] != 1
            or row['column_options'] != 0
            or any(row[name] for name in ('indisunique', 'indisprimary', 'indisexclusion', 'indisreplident', 'indisclustered'))
            or not all(row[name] is True for name in required_true)):
        raise _mismatch(spec)


def _postgresql(connection):
    # Startup repair is catalog/DDL work, never an unbounded maintenance job.
    # SET LOCAL restores the connection's configured timeout on transaction end.
    connection.execute(text("SELECT set_config('lock_timeout','2000ms',true)"))
    schema = connection.execute(text('SELECT current_schema()')).scalar_one()
    if not schema:
        raise IndexSafetyError('文本索引安全迁移无法确定当前数据库schema')
    # Serialize application startups; the local lock timeout bounds both this
    # acquisition and table-lock acquisition, without an automatic retry loop.
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtext('news2douyin:text-index-safety:v1'))"))
    definitions = _pg_definitions(connection, schema)
    for spec in LEGACY_INDEXES:
        if spec.name in definitions:
            _check_pg(spec, definitions[spec.name], schema)
    selected = [spec for spec in LEGACY_INDEXES if spec.name in definitions]
    if not selected:
        return schema, []
    quote = connection.dialect.identifier_preparer.quote_identifier
    # No table data is read. Lock only tables with a verified obsolete index,
    # then revalidate the catalogs so check/drop cannot race other schema DDL.
    targets = ', '.join(quote(schema)+'.'+quote(name) for name in sorted({spec.table for spec in selected}))
    connection.exec_driver_sql('LOCK TABLE '+targets+' IN ACCESS EXCLUSIVE MODE')
    definitions = _pg_definitions(connection, schema)
    removed = []
    for spec in selected:
        if spec.name not in definitions:
            continue
        _check_pg(spec, definitions[spec.name], schema)
        connection.exec_driver_sql('DROP INDEX '+quote(schema)+'.'+quote(spec.name))
        removed.append(spec.name)
    return schema, removed


def _sqlite(connection):
    # SQLite DDL is transactional only after an explicit database transaction.
    # This also prevents another writer from changing the checked definition.
    connection.exec_driver_sql('BEGIN IMMEDIATE')
    definitions = {row['name']: row for row in connection.execute(text('''
        SELECT name,type,tbl_name FROM sqlite_master WHERE name IN (:first,:second)
    '''), dict(first=LEGACY_INDEXES[0].name,second=LEGACY_INDEXES[1].name)).mappings()}
    selected = []
    quote = connection.dialect.identifier_preparer.quote_identifier
    for spec in LEGACY_INDEXES:
        row = definitions.get(spec.name)
        if not row:
            continue
        indexes = {item['name']: item for item in connection.exec_driver_sql(
            'PRAGMA index_list('+quote(spec.table)+')').mappings()}
        index = indexes.get(spec.name)
        columns = list(connection.exec_driver_sql('PRAGMA index_xinfo('+quote(spec.name)+')').mappings())
        keys = [column for column in columns if column['key']]
        if (row['type'] != 'index' or row['tbl_name'] != spec.table or not index
                or index['unique'] or index['partial'] or index['origin'] != 'c'
                or len(keys) != 1 or keys[0]['name'] != spec.column
                or keys[0]['cid'] < 0 or keys[0]['desc'] or keys[0]['coll'] != 'BINARY'):
            raise _mismatch(spec)
        selected.append(spec)
    for spec in selected:
        connection.exec_driver_sql('DROP INDEX '+quote(spec.name))
    return 'main', [spec.name for spec in selected]


def ensure_safe_text_indexes(engine):
    """Idempotent catalog-only migration, called before runtime workers start."""
    dialect = engine.dialect.name
    if dialect not in ('postgresql', 'sqlite'):
        raise IndexSafetyError('文本索引安全迁移仅支持PostgreSQL或SQLite')
    try:
        with engine.begin() as connection:
            schema, removed = (_postgresql(connection) if dialect == 'postgresql' else _sqlite(connection))
    except IndexSafetyError:
        raise
    except SQLAlchemyError as exc:
        # Database exceptions may carry SQL parameter values. Keep the public
        # startup failure actionable without leaking configured credentials.
        raise IndexSafetyError('文本索引安全迁移未完成；请停止旧写进程并检查数据库锁或DDL权限后重试。') from exc
    result = dict(schema=schema, checked=[spec.name for spec in LEGACY_INDEXES], removed=removed,
                  source_text_preserved=True, scans_or_rebuilds=False)
    engine._n2d_text_index_safety = result
    if removed:
        log.info('Removed obsolete text indexes before workers: %s', ', '.join(removed))
    return result
