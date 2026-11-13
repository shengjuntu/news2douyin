"""Stop-window, read-only SQLite -> empty PostgreSQL migration with resumable keysets.

Operational data only: external DuckDB lakes and ZIP archives remain in the
existing storage-root. No source checkpoint, VACUUM, trigger or write is run.
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from datetime import date,datetime,time as datetime_time,timezone
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3

from sqlalchemy import BigInteger,Boolean,Column,Integer,MetaData,String,Table,Text,func,inspect,select,text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import make_url
from sqlalchemy.types import TypeDecorator
from sqlmodel import SQLModel

CHECKPOINT_SCHEMA=MetaData()
RUN=Table('_n2d_sqlite_migration',CHECKPOINT_SCHEMA,
    Column('id',Integer,primary_key=True),Column('source_fingerprint',Text,nullable=False),
    Column('schema_hash',String(64),nullable=False),Column('phase',String(30),nullable=False),
    Column('created_at',Text,nullable=False),Column('updated_at',Text,nullable=False),Column('failure_type',Text,nullable=False,default=''))
PROGRESS=Table('_n2d_sqlite_migration_table',CHECKPOINT_SCHEMA,
    Column('name',String(200),primary_key=True),Column('last_pk_json',Text,nullable=False),
    Column('rows_copied',BigInteger,nullable=False),Column('chain_hash',String(64),nullable=False),
    Column('state',String(20),nullable=False),Column('verified_rows',BigInteger),Column('verified_hash',String(64)))
ZERO='00'*32
LOCK_ID=527834016235189


class MigrationError(RuntimeError):
    """Messages never include database URLs, SQL parameters or source row text."""


def _now():return datetime.now(timezone.utc).isoformat(timespec='seconds')
def _json(value):return json.dumps(value,ensure_ascii=False,separators=(',',':'),sort_keys=True)
def _quote(name):return '"'+name.replace('"','""')+'"'


def _file_signature(path):
    if not path.exists():return None
    stat=path.stat()
    with path.open('rb') as stream:
        head=stream.read(1024*1024)
        stream.seek(max(0,stat.st_size-1024*1024));tail=stream.read(1024*1024)
    return dict(size=stat.st_size,mtime_ns=stat.st_mtime_ns,edge_sha256=hashlib.sha256(head+tail).hexdigest())


def source_fingerprint(path):
    path=Path(path).resolve()
    if not path.is_file():raise MigrationError('SQLite source file does not exist')
    for suffix in ('-wal','-journal'):
        side=Path(str(path)+suffix)
        if side.exists() and side.stat().st_size:
            raise MigrationError('Source has a nonempty '+suffix+' file; stop writers and prepare a clean SQLite snapshot before migration')
    # SQLite can create empty WAL bookkeeping while opening a clean WAL-mode
    # database read-only. Empty and absent sidecars represent the same source
    # snapshot; shared memory likewise is not database content. Never normalize
    # a nonempty WAL/journal: the checks above intentionally reject those.
    return dict(path=str(path),main=_file_signature(path),wal=None,journal=None)


@contextmanager
def readonly_source(path):
    # Never use immutable=1: it would incorrectly ignore an uncheckpointed WAL.
    uri=Path(path).resolve().as_uri()+'?mode=ro'
    connection=sqlite3.connect(uri,uri=True,timeout=1)
    try:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        # Establish a single stable read snapshot before inspecting schema/data.
        connection.execute('SELECT count(*) FROM sqlite_master').fetchone()
        yield connection
    finally:
        connection.rollback();connection.close()


def _assert_unchanged(path,fingerprint,source,version):
    current=dict(path=str(Path(path).resolve()))
    for name,suffix in [('main',''),('wal','-wal'),('journal','-journal')]:
        item=Path(str(path)+suffix)
        if item.exists():
            stat=item.stat()
            current[name]=None if suffix and stat.st_size==0 else dict(size=stat.st_size,mtime_ns=stat.st_mtime_ns)
        else:current[name]=None
    expected={name:({k:v for k,v in fingerprint[name].items() if k!='edge_sha256'} if fingerprint[name] is not None else None) for name in ('main','wal','journal')}
    expected['path']=fingerprint['path']
    if current!=expected or source.execute('PRAGMA data_version').fetchone()[0]!=version:
        raise MigrationError('SQLite source changed during migration; do not resume against a changing source')


def _source_tables(source):
    rows=source.execute("SELECT name,sql FROM sqlite_master WHERE type='table'").fetchall()
    virtual={name for name,sql in rows if sql and 'CREATE VIRTUAL TABLE' in sql.upper() and 'USING FTS' in sql.upper()}
    derived=set(virtual)
    for name,_ in rows:
        if name.startswith('sqlite_') or any(name==v+suffix for v in virtual for suffix in ('_data','_idx','_content','_docsize','_config','_segments','_segdir','_stat')):derived.add(name)
    return {name for name,_ in rows if name not in derived},sorted(derived)


def _plans(source):
    from .db import load_models
    load_models()
    tables={table.name:table for table in SQLModel.metadata.sorted_tables}
    present,derived=_source_tables(source)
    unknown=present-set(tables)
    if unknown:raise MigrationError('Source contains tables outside loaded application metadata: '+', '.join(sorted(unknown)))
    plans=[]
    for table in tables.values():
        primary=[c.name for c in table.primary_key.columns]
        if not primary:raise MigrationError('Application table has no primary key: '+table.name)
        if table.name in present:
            info=source.execute('PRAGMA table_info('+_quote(table.name)+')').fetchall()
            original={r[1] for r in info}
            if original-set(table.columns.keys()):raise MigrationError('Source contains unknown columns in table '+table.name)
            columns=[c.name for c in table.columns if c.name in original]
            if not set(primary)<=set(columns):raise MigrationError('Source lacks primary key columns in table '+table.name)
        else:columns=[c.name for c in table.columns]
        plans.append(dict(table=table,columns=columns,primary=primary,present=table.name in present))
    schema_hash=hashlib.sha256(_json([(p['table'].name,p['columns'],p['primary'],p['present'],[(c.name,str(c.type),c.nullable) for c in p['table'].columns]) for p in plans]).encode()).hexdigest()
    return plans,derived,schema_hash


def _convert(table,row):
    result={}
    for name,value in row.items():
        if isinstance(table.c[name].type,Boolean) and value is not None:
            if value not in (0,1,False,True):raise MigrationError('Non-Boolean SQLite value in table '+table.name+' column '+name)
            value=bool(value)
        result[name]=value
    return result


def _tag(value):
    if value is None:return ['null']
    if isinstance(value,bool):return ['bool',value]
    if isinstance(value,int):return ['int',str(value)]
    if isinstance(value,float):
        # Both backends store IEEE double precision; preserve signed zero too.
        return ['float',value.hex() if math.isfinite(value) else str(value)]
    if isinstance(value,Decimal):return ['decimal',str(value)]
    if isinstance(value,(datetime,date,datetime_time)):return [type(value).__name__,value.isoformat()]
    if isinstance(value,(bytes,bytearray,memoryview)):return ['bytes',base64.b64encode(bytes(value)).decode('ascii')]
    if isinstance(value,str):return ['str',value]
    raise MigrationError('Unsupported stored scalar type: '+type(value).__name__)


def _untag(value):
    kind=value[0]
    if kind=='null':return None
    if kind=='bool':return bool(value[1])
    if kind=='int':return int(value[1])
    if kind=='float':return float.fromhex(value[1])
    if kind=='bytes':return base64.b64decode(value[1])
    if kind=='str':return value[1]
    raise MigrationError('Unsupported primary-key checkpoint type')


def _chain(previous,row,columns):
    raw=_json([[name,_tag(row[name])] for name in columns]).encode('utf-8')
    return hashlib.sha256(bytes.fromhex(previous)+len(raw).to_bytes(8,'big')+raw).hexdigest()


def _text_column(column):
    scalar=column.type
    while isinstance(scalar,TypeDecorator):scalar=scalar.impl
    return isinstance(scalar,(String,Text))


def _source_chunks(source,plan,chunk_size,last_pk=None):
    if not plan['present']:return
    table=plan['table'];columns=plan['columns'];keys=plan['primary']
    ordered=[_quote(k)+(' COLLATE BINARY' if _text_column(table.c[k]) else '') for k in keys]
    query='SELECT '+','.join(_quote(c) for c in columns)+' FROM '+_quote(table.name)
    params=[]
    if last_pk is not None:
        if any(v is None for v in last_pk):raise MigrationError('Nullable primary key cannot resume table '+table.name)
        query+=' WHERE ('+','.join(ordered)+') > ('+','.join('?' for _ in keys)+')';params.extend(last_pk)
    query+=' ORDER BY '+','.join(ordered)
    cursor=source.execute(query,params)
    while True:
        values=cursor.fetchmany(chunk_size)
        if not values:break
        yield [_convert(table,dict(zip(columns,row))) for row in values]


def _save_progress(connection,name,last_pk,count,digest,state,verified_rows=None,verified_hash=None):
    values=dict(name=name,last_pk_json=_json([_tag(v) for v in last_pk]) if last_pk is not None else 'null',
                rows_copied=count,chain_hash=digest,state=state,verified_rows=verified_rows,verified_hash=verified_hash)
    statement=pg_insert(PROGRESS).values(**values)
    connection.execute(statement.on_conflict_do_update(index_elements=[PROGRESS.c.name],set_={k:v for k,v in values.items() if k!='name'}))


def _set_phase(connection,phase,failure_type=''):
    connection.execute(RUN.update().where(RUN.c.id==1).values(phase=phase,failure_type=failure_type,updated_at=_now()))
    connection.commit()


def _prepare_target(connection,plans,fingerprint,schema_hash,resume):
    # Dedicated public schema only. Refuse even empty pre-existing application tables.
    inspector=inspect(connection)
    existing=set(inspector.get_table_names(schema='public'))
    views=set(inspector.get_view_names(schema='public'))|set(inspector.get_materialized_view_names(schema='public'))
    for schema in inspector.get_schema_names():
        if schema in ('public','information_schema') or schema.startswith('pg_'):continue
        if inspector.get_table_names(schema=schema) or inspector.get_view_names(schema=schema) or inspector.get_materialized_view_names(schema=schema) or inspector.get_sequence_names(schema=schema):
            raise MigrationError('Target contains objects in another user schema; use a dedicated empty database')
    sequences=inspector.get_sequence_names(schema='public')
    connection.commit()
    if not resume:
        if existing or views or sequences:raise MigrationError('Target PostgreSQL database must be empty; existing user objects are never overwritten')
        CHECKPOINT_SCHEMA.create_all(connection)
        connection.execute(RUN.insert().values(id=1,source_fingerprint=_json(fingerprint),schema_hash=schema_hash,
            phase='preparing',created_at=_now(),updated_at=_now(),failure_type=''))
        connection.commit()
    else:
        if not {RUN.name,PROGRESS.name}<=existing:raise MigrationError('Resume requires this migrator\'s existing checkpoint tables')
        row=connection.execute(select(RUN).where(RUN.c.id==1)).mappings().first();connection.commit()
        if not row or row['source_fingerprint']!=_json(fingerprint):raise MigrationError('Resume source snapshot identity does not match')
        if row['schema_hash']!=schema_hash:raise MigrationError('Application schema changed since the migration started')
        if row['phase']=='ready':raise MigrationError('Migration is already ready; refusing to resume a potentially live target')
        allowed={p['table'].name for p in plans}|{RUN.name,PROGRESS.name}
        if views:raise MigrationError('Resume target contains unexpected user views')
        if existing-allowed:raise MigrationError('Resume target contains unexpected user tables')
    # Build table definitions without secondary indexes; data and constraints come first.
    metadata=MetaData()
    for plan in plans:
        copied=plan['table'].to_metadata(metadata)
        copied.indexes.clear()
    metadata.create_all(connection)
    connection.commit()


def _verify_table(connection,source,plan,chunk_size):
    name=plan['table'].name;columns=plan['columns'];source_count=target_count=0;source_hash=target_hash=ZERO
    for chunk in _source_chunks(source,plan,chunk_size):
        for row in chunk:source_hash=_chain(source_hash,row,columns);source_count+=1
    table=plan['table']
    order=[table.c[key].collate('C') if _text_column(table.c[key]) else table.c[key] for key in plan['primary']]
    statement=select(*(table.c[c] for c in columns)).order_by(*order).execution_options(stream_results=True,yield_per=chunk_size)
    result=connection.execute(statement).mappings()
    try:
        while True:
            values=result.fetchmany(chunk_size)
            if not values:break
            for row in values:target_hash=_chain(target_hash,_convert(table,dict(row)),columns);target_count+=1
    finally:result.close()
    connection.commit()
    if (source_count,source_hash)!=(target_count,target_hash):raise MigrationError('Full-row verification failed for table '+name)
    return dict(table=name,source_rows=source_count,target_rows=target_count,source_sha256=source_hash,target_sha256=target_hash,
                source_table_present=plan['present'],columns=columns)


def _install_indexes(connection,plans):
    for plan in plans:
        for index in sorted(plan['table'].indexes,key=lambda index:index.name or ''):
            index.create(connection,checkfirst=True)
    connection.commit()


def _reset_sequences(connection,plans,source):
    sequence_table=source.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone()
    source_highwater=dict(source.execute('SELECT name,seq FROM sqlite_sequence')) if sequence_table else {}
    for plan in plans:
        table=plan['table']
        for column in table.primary_key.columns:
            if not isinstance(column.type,Integer):continue
            sequence=connection.execute(text('SELECT pg_get_serial_sequence(:table_name,:column_name)'),dict(table_name='public.'+_quote(table.name),column_name=column.name)).scalar()
            if sequence:
                maximum=connection.execute(select(func.max(column))).scalar()
                prior=source_highwater.get(table.name)
                if prior is not None:maximum=max(maximum if maximum is not None else 0,int(prior))
                called=maximum is not None and maximum>=1
                connection.execute(text('SELECT setval(CAST(:sequence_name AS regclass),:value,:called)'),dict(sequence_name=sequence,value=int(maximum) if called else 1,called=called))
    connection.commit()


def migrate_sqlite(source_path,target_url,*,chunk_size=1000,resume=False,source_stopped=False,install_runtime=True,progress=None):
    """Copy a stopped, clean source using bounded memory and transactional PK checkpoints.

    progress(table_name, rows_copied) is an optional callback after a committed
    chunk; callback failures leave a valid resumable target checkpoint.
    """
    if not source_stopped:raise MigrationError('Confirm the source service is stopped with --source-stopped')
    if not isinstance(chunk_size,int) or not 1<=chunk_size<=10000:raise MigrationError('chunk_size must be between 1 and 10000')
    try:
        if make_url(target_url).get_backend_name() not in ('postgresql','postgres'):raise ValueError()
    except Exception:raise MigrationError('Target must be a valid PostgreSQL URL') from None
    path=Path(source_path).resolve();fingerprint=source_fingerprint(path)
    from .db import make_engine,install_runtime_schema
    engine=None;connection=None;owns_lock=False;prepared=False
    try:
        engine=make_engine(target_url,maintenance=True)
        connection=engine.connect()
        owns_lock=bool(connection.execute(text('SELECT pg_try_advisory_lock(:id)'),dict(id=LOCK_ID)).scalar());connection.commit()
        if not owns_lock:raise MigrationError('Another migration is using the target database')
        with readonly_source(path) as source:
            version=source.execute('PRAGMA data_version').fetchone()[0]
            plans,derived,schema_hash=_plans(source)
            _prepare_target(connection,plans,fingerprint,schema_hash,resume);prepared=True
            _assert_unchanged(path,fingerprint,source,version)
            _set_phase(connection,'copying')
            for plan in plans:
                table=plan['table'];saved=connection.execute(select(PROGRESS).where(PROGRESS.c.name==table.name)).mappings().first();connection.commit()
                if saved and saved['state'] in ('copied','verified'):continue
                last_pk=[_untag(v) for v in json.loads(saved['last_pk_json'])] if saved and saved['last_pk_json']!='null' else None
                count=saved['rows_copied'] if saved else 0;digest=saved['chain_hash'] if saved else ZERO
                for chunk in _source_chunks(source,plan,chunk_size,last_pk):
                    _assert_unchanged(path,fingerprint,source,version)
                    for row in chunk:digest=_chain(digest,row,plan['columns'])
                    count+=len(chunk);last_pk=[chunk[-1][key] for key in plan['primary']]
                    with connection.begin():
                        connection.execute(table.insert(),chunk)
                        _save_progress(connection,table.name,last_pk,count,digest,'copying')
                    if progress:progress(table.name,count)
                with connection.begin():_save_progress(connection,table.name,last_pk,count,digest,'copied')
            _set_phase(connection,'verifying')
            reports=[]
            for plan in plans:
                _assert_unchanged(path,fingerprint,source,version)
                report=_verify_table(connection,source,plan,chunk_size);reports.append(report)
                with connection.begin():
                    connection.execute(PROGRESS.update().where(PROGRESS.c.name==plan['table'].name).values(state='verified',verified_rows=report['target_rows'],verified_hash=report['target_sha256']))
            _assert_unchanged(path,fingerprint,source,version)
            _set_phase(connection,'installing')
            _install_indexes(connection,plans);_reset_sequences(connection,plans,source)
            if install_runtime:install_runtime_schema(engine,backfill=False)
            _assert_unchanged(path,fingerprint,source,version)
            if source_fingerprint(path)!=fingerprint:raise MigrationError('Source snapshot fingerprint changed before readiness')
            _set_phase(connection,'ready')
            return dict(status='ready',source=str(path),target_backend='postgresql',tables=reports,
                total_rows=sum(r['source_rows'] for r in reports),excluded_derived_tables=derived,
                hash_algorithm='ordered typed SHA256 chain v1',source_unchanged=True,
                runtime_schema_installed=bool(install_runtime),external_storage='DuckDB lakes, ZIPs and run artifacts stay in the existing storage-root; they are not copied by this database migration')
    except MigrationError:
        if connection is not None and prepared:
            try:connection.rollback();_set_phase(connection,'failed','MigrationError')
            except Exception:pass
        raise
    except Exception as exc:
        if connection is not None and prepared:
            try:connection.rollback();_set_phase(connection,'failed',type(exc).__name__)
            except Exception:pass
        raise MigrationError('Migration stopped safely; failure type '+type(exc).__name__+'; use --resume with the same unchanged source') from None
    finally:
        if connection is not None:
            try:
                connection.rollback()
                if owns_lock:connection.execute(text('SELECT pg_advisory_unlock(:id)'),dict(id=LOCK_ID));connection.commit()
            except Exception:
                # A disconnected backend releases session locks itself. Cleanup
                # must not replace the sanitized migration error with driver text.
                pass
            finally:
                try:connection.close()
                except Exception:pass
        if engine is not None:
            try:engine.dispose()
            except Exception:pass


def main(argv=None):
    from ..config import load_environment
    load_environment()
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True,type=Path)
    parser.add_argument('--target-url',default=os.getenv('NEWS2DOUYIN_DATABASE_URL',''))
    parser.add_argument('--chunk-size',type=int,default=1000)
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--source-stopped',action='store_true',help='Confirm source writers are stopped and the SQLite snapshot is clean')
    parser.add_argument('--report-file',type=Path)
    args=parser.parse_args(argv)
    forbidden={Path(str(args.source.resolve())+suffix) for suffix in ('','-wal','-shm','-journal')}
    if args.report_file and args.report_file.resolve() in forbidden:
        print(json.dumps(dict(status='failed',error='Report path must not overwrite the SQLite source or its sidecars')))
        return 1
    try:
        result=migrate_sqlite(args.source,args.target_url,chunk_size=args.chunk_size,resume=args.resume,source_stopped=args.source_stopped)
        code=0
    except MigrationError as exc:result=dict(status='failed',error=str(exc));code=1
    rendered=json.dumps(result,ensure_ascii=False,indent=2)
    if args.report_file:
        args.report_file.parent.mkdir(parents=True,exist_ok=True);args.report_file.write_text(rendered+'\n',encoding='utf-8')
    print(rendered)
    return code


if __name__=='__main__':raise SystemExit(main())
