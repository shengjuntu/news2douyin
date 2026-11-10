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
from threading import RLock
from urllib.parse import urlsplit
from zipfile import ZipFile
from ..storage.models import utc_now_iso

_LOCK=RLock()
SCHEMA='''CREATE TABLE IF NOT EXISTS intake_records(
 feed VARCHAR, stamp VARCHAR, row_index BIGINT, record_id VARCHAR, document_date VARCHAR,
 url VARCHAR, title VARCHAR, metadata_json VARCHAR, valid BOOLEAN, error VARCHAR,
 raw_line VARCHAR, observed_at VARCHAR, PRIMARY KEY(feed,stamp,row_index));
CREATE TABLE IF NOT EXISTS intake_archives(
 feed VARCHAR,stamp VARCHAR,sha256 VARCHAR,md5 VARCHAR,compressed_bytes BIGINT,
 archive_path VARCHAR,rows BIGINT,valid_rows BIGINT,invalid_rows BIGINT,
 completed_at VARCHAR,PRIMARY KEY(feed,stamp));'''


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
    if feed not in ('gkg_english','gkg_translation'):raise ValueError('unknown_intake_feed')
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
        # DuckDB supports threads in one process; no process writes this file concurrently.
        with _LOCK:
            if not create and not self.path.exists():yield None;return
            if create:self.root.mkdir(parents=True,exist_ok=True)
            import duckdb
            connection=duckdb.connect(str(self.path))
            try:
                if create:connection.execute(SCHEMA)
                yield connection
            finally:connection.close()

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
                    permitted={stamp+'.gkg.csv'} if feed=='gkg_english' else {stamp+'.gkg.csv',stamp+'.translation.gkg.csv'}
                    if member.is_dir() or member.filename not in permitted:
                        raise ValueError('unexpected_archive_member')
                    if member.file_size>maximum:raise ValueError('archive_expanded_size_limit')
                    consumed=0
                    with archive.open(member) as source:
                        for raw in source:
                            consumed+=len(raw)
                            if consumed>maximum:raise ValueError('archive_expanded_size_limit')
                            total+=1;row=decoded_row(raw,feed,stamp,total,now);valid+=int(row['valid'])
                            chunk.append([row[k] for k in ('feed','stamp','row_index','record_id','document_date','url','title')]+[json.dumps(row['metadata'],ensure_ascii=False),row['valid'],row['error'],row['raw_line'],now])
                            if len(chunk)>=1000:
                                connection.executemany('INSERT INTO intake_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',chunk);chunk=[]
                    if consumed!=member.file_size:raise ValueError('archive_expanded_size_mismatch')
                if chunk:connection.executemany('INSERT INTO intake_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',chunk)
                connection.execute('INSERT INTO intake_archives VALUES (?,?,?,?,?,?,?,?,?,?)',[feed,stamp,sha,md5,size,str(Path(path).resolve()),total,valid,total-valid,now])
                connection.execute('COMMIT')
            except BaseException:
                connection.execute('ROLLBACK');raise
        return dict(sha256=sha,md5=md5,compressed_bytes=size,archive_path=str(Path(path).resolve()),rows=total,valid_rows=valid,invalid_rows=total-valid,completed_at=now)

    def _rows(self,query,values):
        with self.connection() as connection:
            if connection is None:return []
            cursor=connection.execute(query,values);names=[d[0] for d in cursor.description]
            result=[dict(zip(names,row)) for row in cursor.fetchall()]
        for row in result:
            if 'metadata_json' in row:row['metadata']=json.loads(row.pop('metadata_json'))
        return result

    def rows(self,feed,stamp,after=0,limit=100):
        validate_key(feed,stamp)
        return self._rows('SELECT * FROM intake_records WHERE feed=? AND stamp=? AND row_index>? ORDER BY row_index LIMIT ?',[feed,stamp,max(0,int(after)),max(1,min(int(limit),5000))])

    def search(self,q='',feed='',limit=50,offset=0):
        conditions=[];values=[]
        if feed:
            if feed not in ('gkg_english','gkg_translation'):raise ValueError('unknown_intake_feed')
            conditions.append('feed=?');values.append(feed)
        if q:
            conditions.append('(contains(lower(title),lower(?)) OR contains(lower(url),lower(?)) OR contains(lower(metadata_json),lower(?)))');values.extend([q,q,q])
        where=' WHERE '+' AND '.join(conditions) if conditions else ''
        with self.connection() as connection:
            if connection is None:return dict(items=[],total=0)
            total=connection.execute('SELECT count(*) FROM intake_records'+where,values).fetchone()[0]
        items=self._rows('SELECT * FROM intake_records'+where+' ORDER BY stamp DESC,feed,row_index LIMIT ? OFFSET ?',values+[max(1,min(int(limit),200)),max(0,int(offset))])
        return dict(items=items,total=total)
