"""Single-process DuckDB lake, immutable ZIP evidence and replay-safe batch writes."""
from contextlib import contextmanager
from datetime import datetime
from html import unescape
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import zlib
from threading import RLock
from urllib.parse import urlsplit
from zipfile import ZipFile
from ..storage.models import utc_now_iso

_LOCK=RLock()
PARSER_VERSION='gdelt-intake-2'
SCHEMA="""CREATE TABLE IF NOT EXISTS intake_records_v2(
 feed VARCHAR, stamp VARCHAR, row_index BIGINT, record_id VARCHAR, document_date VARCHAR,
 url VARCHAR, title VARCHAR, valid BOOLEAN, error VARCHAR, observed_at VARCHAR,
 search_text VARCHAR, payload BLOB, codec VARCHAR, raw_sha256 VARCHAR, raw_bytes BIGINT,
 metadata_overlay_json VARCHAR DEFAULT '{}', parser_version VARCHAR DEFAULT 'gdelt-intake-2', PRIMARY KEY(feed,stamp,row_index));
CREATE TABLE IF NOT EXISTS intake_archives(
 feed VARCHAR,stamp VARCHAR,sha256 VARCHAR,md5 VARCHAR,compressed_bytes BIGINT,
 archive_path VARCHAR,rows BIGINT,valid_rows BIGINT,invalid_rows BIGINT,
 completed_at VARCHAR,PRIMARY KEY(feed,stamp));"""
SUMMARY_COLUMNS='feed,stamp,row_index,record_id,document_date,url,title,valid,error,observed_at'


def feed_kind(feed):
    from .feeds import kind_for
    return kind_for(feed)


def decode_payload(raw,feed,stamp,index,observed):
    if feed_kind(feed)=='gkg':return decoded_row(raw,feed,stamp,index,observed)
    from .official_parser import decoded_row as official_row
    return official_row(raw,feed,stamp,index,observed)


def search_text(row):
    metadata=row.get('metadata') or {}
    fields=('persons','orgs','themes','locations','mention_iso','actor1_name','actor2_name',
            'actor1_country','actor2_country','action_country','event_code','event_root_code')
    return ' '.join(str(value) for value in (row.get('title',''),row.get('url',''),
        *(metadata.get(name,'') for name in fields))).casefold()


def packed_row(row,raw,*,metadata_overlay=None):
    compressed=zlib.compress(raw,6)
    payload,codec=(compressed,'zlib') if len(compressed)<len(raw) else (raw,'raw')
    return [row.get(name,'') for name in SUMMARY_COLUMNS.split(',')]+[
        search_text(row),payload,codec,hashlib.sha256(raw).hexdigest(),len(raw),
        json.dumps(metadata_overlay or {},ensure_ascii=False,sort_keys=True),PARSER_VERSION]


def unpack_payload(row):
    payload=bytes(row['payload'])
    maximum=positive_limit('NEWS2DOUYIN_GKG_MAX_EXPANDED_BYTES',1024*1024*1024)
    expected=int(row['raw_bytes'])
    if not 0<=expected<=maximum:raise ValueError('lake_payload_size_invalid')
    if row['codec']=='zlib':
        decoder=zlib.decompressobj()
        raw=decoder.decompress(payload,expected+1)
        if len(raw)>expected or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
            raise ValueError('lake_payload_compression_invalid')
    elif row['codec']=='raw':raw=payload
    else:raise ValueError('lake_payload_codec_unknown')
    if len(raw)!=expected or hashlib.sha256(raw).hexdigest()!=row['raw_sha256']:
        raise ValueError('lake_payload_hash_mismatch')
    return raw



def positive_limit(name,default):
    try:value=int(os.getenv(name,str(default)))
    except ValueError:raise ValueError(name+' must be a positive integer')
    if value<=0:raise ValueError(name+' must be a positive integer')
    return value


def hashes(path):
    sha=hashlib.sha256();md5=hashlib.md5();size=0
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            sha.update(block);md5.update(block);size+=len(block)
    return sha.hexdigest(),md5.hexdigest(),size


def validate_key(feed,stamp):
    feed_kind(feed)
    if not re.fullmatch(r'\d{14}',stamp or ''):raise ValueError('invalid_intake_stamp')
    dt=datetime.strptime(stamp,'%Y%m%d%H%M%S')
    if dt.second or dt.minute%15:raise ValueError('invalid_intake_stamp')


def decoded_row(raw,feed,stamp,index,observed):
    error='';metadata={}
    try:line=raw.decode('utf-8')
    except UnicodeDecodeError:
        line=raw.decode('utf-8','replace');error='invalid_utf8'
        metadata['raw_bytes_base64']=base64.b64encode(raw).decode('ascii')
    line=line.rstrip('\r\n');columns=line.split('\t')
    rid=columns[0] if columns else '';date=columns[1] if len(columns)>1 else '';url=columns[4] if len(columns)>4 else '';title=''
    if len(columns)!=27:error=error or 'expected_27_columns'
    else:
        def extra(tag):
            found=re.search('<'+tag+'>(.*?)</'+tag+'>',columns[26],re.S)
            return unescape(found[1].strip()) if found else ''
        def terms(column):return list(dict.fromkeys(t.strip() for t in columns[column].split(';') if t.strip()))
        from ..collect.providers.gkg import FIPS
        locations=[r.split('#') for r in columns[9].split(';') if r]
        metadata.update(persons=terms(11),orgs=terms(13),themes=terms(7),locations=list(dict.fromkeys(r[1] for r in locations if len(r)>1)),
            mention_iso=sorted({FIPS[r[2].upper()] for r in locations if len(r)>2 and r[2].upper() in FIPS}),
            author=extra('PAGE_AUTHORS'),source=columns[3],source_collection=columns[2],translation=columns[25],
            enhanced_dates=columns[16],tone=columns[15],gkg_record_id=rid,document_date=date,
            enhanced_themes=columns[8],enhanced_locations=columns[10],enhanced_persons=columns[12],
            enhanced_orgs=columns[14],gcam=columns[17],sharing_image=columns[18],
            related_images=columns[19],embedded_images=columns[20],embedded_videos=columns[21],
            quotations=columns[22],all_names=columns[23],amounts=columns[24],extras_xml=columns[26])
        title=extra('PAGE_TITLE')
        if not re.fullmatch(r'\d{14}-T?\d+',rid):error=error or 'invalid_record_id'
        try:datetime.strptime(date,'%Y%m%d%H%M%S')
        except ValueError:error=error or 'invalid_document_date'
        try:
            parsed=urlsplit(url)
            if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password:
                metadata['projection_eligible']=False;metadata['projection_note']='non_web_or_invalid_document_url'
            else:metadata['projection_eligible']=True
        except ValueError:metadata['projection_eligible']=False;metadata['projection_note']='non_web_or_invalid_document_url'
    return dict(feed=feed,stamp=stamp,row_index=index,record_id=rid,document_date=date,url=url,title=title,
                metadata=metadata,valid=not error,error=error,raw_line=line,observed_at=observed)


class Lake:
    def __init__(self,storage_root):
        self.root=Path(storage_root).resolve()/'intake'
        self.path=self.root/'records.duckdb'

    @contextmanager
    def connection(self,create=False):
        # One cooperating receiver process owns writes. Reads share this lock;
        # HTTP never keeps a connection open while decoding large payloads.
        with _LOCK:
            if not create and not self.path.exists():yield None;return
            if create:self.root.mkdir(parents=True,exist_ok=True)
            import duckdb
            connection=duckdb.connect(str(self.path))
            try:
                if create:connection.execute(SCHEMA)
                yield connection
            finally:connection.close()

    @staticmethod
    def tables(connection):
        return {row[0] for row in connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main'").fetchall()}

    def ingest_archive(self,feed,stamp,path,expected_md5='',expected_bytes=0):
        validate_key(feed,stamp)
        sha,md5,size=hashes(path)
        if expected_bytes and size!=expected_bytes:raise ValueError('archive_size_mismatch')
        if expected_md5 and md5.lower()!=expected_md5.lower():raise ValueError('archive_md5_mismatch')
        maximum=positive_limit('NEWS2DOUYIN_GKG_MAX_EXPANDED_BYTES',1024*1024*1024)
        now=utc_now_iso()
        with self.connection(create=True) as connection:
            old=connection.execute('SELECT * FROM intake_archives WHERE feed=? AND stamp=?',[feed,stamp]).fetchone()
            if old:
                if old[2]!=sha:raise ValueError('immutable_batch_hash_changed')
                return dict(sha256=old[2],md5=old[3],compressed_bytes=old[4],archive_path=old[5],rows=old[6],valid_rows=old[7],invalid_rows=old[8],completed_at=old[9])
            connection.execute('BEGIN TRANSACTION')
            try:
                total=valid=0;chunk=[]
                with ZipFile(path) as archive:
                    members=archive.infolist()
                    if len(members)!=1:raise ValueError('archive_requires_one_data_member')
                    member=members[0]
                    from .feeds import permitted_members
                    if member.is_dir() or member.filename not in permitted_members(feed,stamp):
                        raise ValueError('unexpected_archive_member')
                    if member.file_size>maximum:raise ValueError('archive_expanded_size_limit')
                    consumed=0
                    with archive.open(member) as source:
                        for raw in source:
                            consumed+=len(raw)
                            if consumed>maximum:raise ValueError('archive_expanded_size_limit')
                            total+=1;row=decode_payload(raw,feed,stamp,total,now);valid+=int(row['valid'])
                            chunk.append(packed_row(row,raw))
                            if len(chunk)>=1000:
                                connection.executemany('INSERT INTO intake_records_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',chunk);chunk=[]
                    if consumed!=member.file_size:raise ValueError('archive_expanded_size_mismatch')
                if chunk:connection.executemany('INSERT INTO intake_records_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',chunk)
                connection.execute('INSERT INTO intake_archives VALUES (?,?,?,?,?,?,?,?,?,?)',[feed,stamp,sha,md5,size,str(Path(path).resolve()),total,valid,total-valid,now])
                connection.execute('COMMIT')
            except BaseException:
                connection.execute('ROLLBACK');raise
        return dict(sha256=sha,md5=md5,compressed_bytes=size,archive_path=str(Path(path).resolve()),rows=total,valid_rows=valid,invalid_rows=total-valid,completed_at=now)

    @staticmethod
    def material(row):
        if 'payload' in row:
            if row.get('parser_version')!=PARSER_VERSION:raise ValueError('lake_parser_version_unsupported')
            raw=unpack_payload(row)
            parsed=decode_payload(raw,row['feed'],row['stamp'],int(row['row_index']),row['observed_at'])
            overlay=json.loads(row.get('metadata_overlay_json') or '{}')
            for key in overlay.get('remove',[]):parsed['metadata'].pop(key,None)
            parsed['metadata'].update(overlay.get('set',{}))
            # Summary columns retain the interpretation used at ingestion.
            parsed.update({name:row[name] for name in SUMMARY_COLUMNS.split(',')})
            return parsed
        row=dict(row)
        if 'metadata_json' in row:row['metadata']=json.loads(row.pop('metadata_json'))
        return row

    def rows(self,feed,stamp,after=0,limit=100):
        validate_key(feed,stamp)
        after=max(0,int(after));limit=max(1,min(int(limit),5000))
        with self.connection() as connection:
            if connection is None:return []
            tables=self.tables(connection)
            for table in ('intake_records_v2','intake_records'):
                if table not in tables:continue
                cursor=connection.execute('SELECT * FROM '+table+' WHERE feed=? AND stamp=? AND row_index>? ORDER BY row_index LIMIT ?', [feed,stamp,after,limit])
                names=[column[0] for column in cursor.description]
                result=[dict(zip(names,row)) for row in cursor.fetchall()]
                if result:break
            else:result=[]
        return [self.material(row) for row in result]

    def raw_bytes(self,feed,stamp,row_index):
        validate_key(feed,stamp)
        with self.connection() as connection:
            if connection is None:raise KeyError('lake_record_missing')
            tables=self.tables(connection)
            if 'intake_records_v2' in tables:
                cursor=connection.execute('SELECT payload,codec,raw_sha256,raw_bytes FROM intake_records_v2 WHERE feed=? AND stamp=? AND row_index=?',[feed,stamp,row_index])
                names=[column[0] for column in cursor.description];value=cursor.fetchone()
                if value is not None:return unpack_payload(dict(zip(names,value)))
            # Legacy raw_line lost newline bytes. Recover exact bytes only from
            # the immutable ZIP, never pretend the decoded text is byte-exact.
            archive=connection.execute('SELECT archive_path,sha256 FROM intake_archives WHERE feed=? AND stamp=?',[feed,stamp]).fetchone()
        if not archive or not Path(archive[0]).is_file():raise ValueError('legacy_exact_bytes_require_archive')
        if hashes(archive[0])[0]!=archive[1]:raise ValueError('archive_sha256_mismatch')
        with ZipFile(archive[0]) as zipped:
            members=zipped.infolist()
            if len(members)!=1:raise ValueError('archive_requires_one_data_member')
            with zipped.open(members[0]) as source:
                for index,raw in enumerate(source,1):
                    if index==row_index:return raw
        raise KeyError('lake_record_missing')

    def archive_reference(self,feed,stamp,row_index):
        validate_key(feed,stamp)
        with self.connection() as connection:
            if connection is None:raise KeyError('archive_manifest_missing')
            row=connection.execute('SELECT sha256 FROM intake_archives WHERE feed=? AND stamp=?',[feed,stamp]).fetchone()
        if not row:raise KeyError('archive_manifest_missing')
        return dict(feed=feed,stamp=stamp,row_index=int(row_index),archive_sha256=row[0],parser_version=PARSER_VERSION)

    def search(self,q='',feed='',limit=50,offset=0):
        if feed:feed_kind(feed)
        conditions=[];values=[]
        if feed:conditions.append('feed=?');values.append(feed)
        if q:
            conditions.append('contains(search_text,lower(?))');values.append(q)
        where=' WHERE '+' AND '.join(conditions) if conditions else ''
        with self.connection() as connection:
            if connection is None:return dict(items=[],total=0)
            tables=self.tables(connection);parts=[]
            if 'intake_records_v2' in tables:parts.append('SELECT '+SUMMARY_COLUMNS+',search_text FROM intake_records_v2')
            if 'intake_records' in tables:
                # Legacy queries inspect text metadata but return narrow rows;
                # existing archives are not rewritten on service startup.
                parts.append('SELECT '+SUMMARY_COLUMNS+",lower(title||' '||url||' '||coalesce(metadata_json,'')) AS search_text FROM intake_records")
            if not parts:return dict(items=[],total=0)
            source='('+' UNION ALL '.join(parts)+') AS archived'
            total=connection.execute('SELECT count(*) FROM '+source+where,values).fetchone()[0]
            cursor=connection.execute('SELECT '+SUMMARY_COLUMNS+' FROM '+source+where+
                ' ORDER BY stamp DESC,feed,row_index LIMIT ? OFFSET ?',values+[max(1,min(int(limit),200)),max(0,int(offset))])
            names=[column[0] for column in cursor.description]
            items=[dict(zip(names,row)) for row in cursor.fetchall()]
        return dict(items=items,total=total,search_scope='title_url_entities_themes_places')
