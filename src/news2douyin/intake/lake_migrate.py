"""Offline, verified copy of a lake into the single-payload format.

Never activates, replaces or deletes the original DB/ZIP. The receiver must be
stopped; its OS lock and DuckDB's process lock additionally protect the copy.
Logical column lengths do not predict physical space recovered. Only the
completed destination file size is reported as an actual compacted size.
"""
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4
from zipfile import ZipFile

from .lake import Lake, SCHEMA, SUMMARY_COLUMNS, hashes, decode_payload, packed_row, unpack_payload, positive_limit
from .feeds import permitted_members


class LakeCompactionError(RuntimeError):
    pass


def _logical(row):
    return {key:row.get(key) for key in (*SUMMARY_COLUMNS.split(','),'raw_line','metadata')}


def _digest(rows):
    digest=hashlib.sha256()
    for row in rows:
        data=json.dumps(_logical(row),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')
        digest.update(len(data).to_bytes(8,'big'));digest.update(data)
    return digest.hexdigest()


def _overlay(metadata,decoded):
    return dict(set={key:value for key,value in metadata.items() if key not in decoded or decoded[key]!=value},
                remove=[key for key in decoded if key not in metadata])


def compact_lake(storage_root,destination,*,source_stopped=False,chunk_size=128,progress=None):
    if not source_stopped:raise LakeCompactionError('Stop the receiver and projection processes and pass source_stopped=True')
    if not isinstance(chunk_size,int) or not 1<=chunk_size<=1000:raise ValueError('chunk_size must be between 1 and 1000')
    from .runtime import ReceiverLock
    import duckdb
    lake=Lake(storage_root);source_path=lake.path.resolve();destination=Path(destination).expanduser().resolve()
    if not source_path.is_file():raise LakeCompactionError('Source lake does not exist')
    if destination==source_path or destination.exists():raise LakeCompactionError('Destination must be a new file distinct from the source')
    destination.parent.mkdir(parents=True,exist_ok=True)
    temporary=destination.with_name(destination.name+'.partial-'+uuid4().hex)
    ownership=ReceiverLock(storage_root).acquire()
    source=target=None;archives=records=0;source_digest=hashlib.sha256();target_digest=hashlib.sha256()
    source_size=0
    try:
        source=duckdb.connect(str(source_path),read_only=True)
        source_size=source_path.stat().st_size
        target=duckdb.connect(str(temporary));target.execute(SCHEMA)
        tables=lake.tables(source)
        unknown=tables-{'intake_records','intake_records_v2','intake_archives'}
        if unknown:raise LakeCompactionError('Unknown non-lake tables require a separate preservation plan: '+', '.join(sorted(unknown)))
        if 'intake_archives' not in tables:raise LakeCompactionError('Source archive manifest missing')
        # A source without a matching immutable manifest cannot be silently
        # discarded. Count on the key only; never aggregate large raw columns.
        for table in ('intake_records','intake_records_v2'):
            if table in tables:
                orphan=source.execute('SELECT 1 FROM '+table+' r WHERE NOT EXISTS (SELECT 1 FROM intake_archives a WHERE a.feed=r.feed AND a.stamp=r.stamp) LIMIT 1').fetchone()
                if orphan:raise LakeCompactionError('Source records have no archive manifest')
        previous_feed=previous_stamp=''
        while True:
            manifest=source.execute('SELECT * FROM intake_archives WHERE feed>? OR (feed=? AND stamp>?) ORDER BY feed,stamp LIMIT 1',
                [previous_feed,previous_feed,previous_stamp]).fetchone()
            if not manifest:break
            feed,stamp,sha,md5,size,path,total,valid,invalid,completed=manifest
            previous_feed,previous_stamp=feed,stamp
            path=Path(path)
            if not path.is_file():
                # Storage roots can move. Identity is the verified hash, not
                # the old absolute path in the legacy catalog.
                from .feeds import FEEDS
                path=lake.root/'raw'/feed/(stamp+FEEDS[feed]['suffix'])
            if not path.is_file() or hashes(path)!=(sha,md5,size):
                raise LakeCompactionError('Immutable archive is missing or its hash/size differs: '+feed+'/'+stamp)
            candidates=[]
            for table in ('intake_records_v2','intake_records'):
                if table in tables and source.execute('SELECT 1 FROM '+table+' WHERE feed=? AND stamp=? LIMIT 1',[feed,stamp]).fetchone():candidates.append(table)
            if total and len(candidates)!=1:raise LakeCompactionError('Batch record storage is missing or overlaps formats')
            table=candidates[0] if candidates else None
            source_cursor=0;page=[];page_offset=0;copied=0;count_valid=0;raw_digest=hashlib.sha256()
            target.execute('BEGIN TRANSACTION')
            try:
                with ZipFile(path) as archive:
                    members=archive.infolist()
                    if len(members)!=1 or members[0].filename not in permitted_members(feed,stamp):
                        raise LakeCompactionError('Unexpected archive member')
                    maximum=positive_limit('NEWS2DOUYIN_GKG_MAX_EXPANDED_BYTES',1024*1024*1024)
                    if members[0].file_size>maximum:raise LakeCompactionError('Archive expanded size limit exceeded')
                    with archive.open(members[0]) as stream:
                        pending=[];consumed=0
                        for index,raw in enumerate(stream,1):
                            consumed+=len(raw)
                            if consumed>maximum:raise LakeCompactionError('Archive expanded size limit exceeded')
                            if page_offset>=len(page):
                                cursor=source.execute('SELECT * FROM '+table+' WHERE feed=? AND stamp=? AND row_index>? ORDER BY row_index LIMIT ?',
                                    [feed,stamp,source_cursor,chunk_size]) if table else None
                                names=[column[0] for column in cursor.description] if cursor else []
                                page=[dict(zip(names,item)) for item in cursor.fetchall()] if cursor else []
                                page_offset=0
                            if not page:raise LakeCompactionError('Archive has records missing from the source lake')
                            stored=page[page_offset];page_offset+=1;source_cursor=int(stored['row_index'])
                            if source_cursor!=index:raise LakeCompactionError('Source batch row numbers are not contiguous')
                            before=lake.material(stored)
                            parsed=decode_payload(raw,feed,stamp,index,stored['observed_at'])
                            if before['raw_line']!=parsed['raw_line']:
                                raise LakeCompactionError('Source raw text differs from immutable archive')
                            if table=='intake_records_v2' and unpack_payload(stored)!=raw:
                                raise LakeCompactionError('Source exact bytes differ from immutable archive')
                            encoded=before.get('metadata',{}).get('raw_bytes_base64')
                            if encoded:
                                import base64
                                if base64.b64decode(encoded)!=raw:raise LakeCompactionError('Source invalid-UTF8 evidence differs from archive')
                            overlay=_overlay(before.get('metadata',{}),parsed.get('metadata',{}))
                            after=dict(parsed,**{key:before[key] for key in SUMMARY_COLUMNS.split(',')})
                            after['metadata']=dict(parsed['metadata'])
                            for key in overlay['remove']:after['metadata'].pop(key,None)
                            after['metadata'].update(overlay['set'])
                            if _logical(before)!=_logical(after):raise LakeCompactionError('Decoded record semantics changed')
                            encoded=json.dumps(_logical(before),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
                            source_digest.update(len(encoded).to_bytes(8,'big'));source_digest.update(encoded)
                            raw_digest.update(len(raw).to_bytes(8,'big'));raw_digest.update(raw)
                            pending.append(packed_row(before,raw,metadata_overlay=overlay))
                            copied+=1;count_valid+=int(before['valid'])
                            if len(pending)>=chunk_size:
                                target.executemany('INSERT INTO intake_records_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',pending);pending=[]
                        if consumed!=members[0].file_size:raise LakeCompactionError('Archive expanded size mismatch')
                        if pending:target.executemany('INSERT INTO intake_records_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',pending)
                if copied!=int(total) or count_valid!=int(valid) or copied-count_valid!=int(invalid):
                    raise LakeCompactionError('Source manifest row counts differ')
                if table and source.execute('SELECT 1 FROM '+table+' WHERE feed=? AND stamp=? AND row_index>? LIMIT 1',[feed,stamp,copied]).fetchone():
                    raise LakeCompactionError('Source contains rows missing from archive')
                # Verify the destination by rereading stored bytes and metadata,
                # not merely trusting INSERT row counts or in-memory values.
                after_cursor=0;verified_raw=hashlib.sha256();verified_count=0
                while True:
                    cursor=target.execute('SELECT * FROM intake_records_v2 WHERE feed=? AND stamp=? AND row_index>? ORDER BY row_index LIMIT ?', [feed,stamp,after_cursor,chunk_size])
                    names=[column[0] for column in cursor.description];page=[dict(zip(names,item)) for item in cursor.fetchall()]
                    if not page:break
                    for stored in page:
                        after_cursor=int(stored['row_index']);raw=unpack_payload(stored)
                        verified_raw.update(len(raw).to_bytes(8,'big'));verified_raw.update(raw)
                        row=lake.material(stored);data=json.dumps(_logical(row),ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
                        target_digest.update(len(data).to_bytes(8,'big'));target_digest.update(data);verified_count+=1
                if verified_count!=copied or verified_raw.hexdigest()!=raw_digest.hexdigest():
                    raise LakeCompactionError('Destination exact-byte verification failed')
                target.execute('INSERT INTO intake_archives VALUES (?,?,?,?,?,?,?,?,?,?)',
                    [feed,stamp,sha,md5,size,str(path.resolve()),total,valid,invalid,completed])
                target.execute('COMMIT')
            except BaseException:
                target.execute('ROLLBACK');raise
            archives+=1;records+=copied
            if progress:progress(dict(archives=archives,records=records,feed=feed,stamp=stamp))
        if source_digest.hexdigest()!=target_digest.hexdigest():raise LakeCompactionError('Destination semantic hash differs')
        target.execute('CHECKPOINT');target.close();target=None;source.close();source=None
        # The destination is published only after every source archive passes.
        # Atomic create-only publication: another completed copy cannot be
        # overwritten between the initial existence check and final publish.
        try:os.link(temporary,destination)
        except FileExistsError:raise LakeCompactionError('Destination appeared during the copy; it was preserved') from None
        temporary.unlink()
        return dict(status='verified',source=str(source_path),destination=str(destination),source_bytes=source_size,
            destination_bytes=destination.stat().st_size,archives=archives,records=records,
            source_logical_sha256=source_digest.hexdigest(),destination_logical_sha256=target_digest.hexdigest(),
            source_preserved=True,activated=False)
    finally:
        if target is not None:target.close()
        if source is not None:source.close()
        ownership.release()
        # Failed partial output is not an activated archive and can be removed.
        if temporary.exists():temporary.unlink()
