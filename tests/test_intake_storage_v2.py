"""Single-payload retention, legacy compatibility and verified offline copies."""
import base64
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import pytest
from sqlmodel import Session,select

from news2douyin.intake.lake import Lake,decoded_row,hashes
from news2douyin.intake.lake_migrate import compact_lake,LakeCompactionError
from news2douyin.intake.projection import project_step,import_document
from news2douyin.storage.models import Article,ArticleVersion
from news2douyin.desk.models import GkgSource
from test_intake_projection import setup,line,archive

STAMP='20261104120000'


def raw_zip(root,raw):
    root=Path(root);root.mkdir(parents=True,exist_ok=True)
    path=root/'evidence.zip'
    with ZipFile(path,'w') as archive:archive.writestr(STAMP+'.gkg.csv',raw)
    return path


def legacy(root,raw,*,extra=None):
    import duckdb
    lake=Lake(root);lake.root.mkdir(parents=True,exist_ok=True)
    path=raw_zip(lake.root,raw)
    sha,md5,size=hashes(path)
    connection=duckdb.connect(str(lake.path))
    connection.execute('CREATE TABLE intake_records(feed VARCHAR,stamp VARCHAR,row_index BIGINT,record_id VARCHAR,document_date VARCHAR,url VARCHAR,title VARCHAR,metadata_json VARCHAR,valid BOOLEAN,error VARCHAR,raw_line VARCHAR,observed_at VARCHAR,PRIMARY KEY(feed,stamp,row_index))')
    connection.execute('CREATE TABLE intake_archives(feed VARCHAR,stamp VARCHAR,sha256 VARCHAR,md5 VARCHAR,compressed_bytes BIGINT,archive_path VARCHAR,rows BIGINT,valid_rows BIGINT,invalid_rows BIGINT,completed_at VARCHAR,PRIMARY KEY(feed,stamp))')
    now='2026-11-04T12:00:00Z';valid=total=0
    for index,value in enumerate(raw.splitlines(keepends=True),1):
        row=decoded_row(value,'gkg_english',STAMP,index,now)
        if extra:row['metadata'].update(extra)
        connection.execute('INSERT INTO intake_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            [row[k] for k in ('feed','stamp','row_index','record_id','document_date','url','title')]+[
                json.dumps(row['metadata']),row['valid'],row['error'],row['raw_line'],now])
        valid+=int(row['valid']);total+=1
    connection.execute('INSERT INTO intake_archives VALUES (?,?,?,?,?,?,?,?,?,?)', ['gkg_english',STAMP,sha,md5,size,str(path),total,valid,total-valid,now])
    connection.close()
    return lake,path


def test_new_rows_store_one_payload_and_preserve_exact_invalid_utf8(tmp_path):
    raw=line(STAMP,1,'Alice met Bob.').encode()+b'bad\xff\trow\r\n'
    lake=Lake(tmp_path);path=raw_zip(tmp_path,raw)
    lake.ingest_archive('gkg_english',STAMP,path)
    with lake.connection() as connection:
        assert 'intake_records' not in lake.tables(connection)
        columns=[row[1] for row in connection.execute("PRAGMA table_info('intake_records_v2')").fetchall()]
        assert 'metadata_json' not in columns and 'raw_line' not in columns
        assert connection.execute('SELECT count(*) FROM intake_records_v2').fetchone()==(2,)
    assert lake.raw_bytes('gkg_english',STAMP,2)==b'bad\xff\trow\r\n'
    bad=lake.rows('gkg_english',STAMP,after=1)[0]
    assert not bad['valid'] and base64.b64decode(bad['metadata']['raw_bytes_base64'])==b'bad\xff\trow\r\n'
    summary=lake.search(q='Alice')['items'][0]
    assert 'metadata' not in summary and 'raw_line' not in summary


def test_new_large_gcam_is_single_payload_not_search_blob(tmp_path):
    cells=line(STAMP,1,'A source').rstrip('\n').split('\t');cells[17]='c1:10,'*10000
    raw=('\t'.join(cells)+'\n').encode()
    lake=Lake(tmp_path);lake.ingest_archive('gkg_english',STAMP,raw_zip(tmp_path,raw))
    with lake.connection() as connection:
        search,payload,codec=connection.execute('SELECT search_text,payload,codec FROM intake_records_v2').fetchone()
        assert 'c1:10' not in search and len(payload)<len(raw) and codec=='zlib'
    assert lake.rows('gkg_english',STAMP)[0]['metadata']['gcam']==cells[17]


def test_legacy_read_does_not_rewrite_and_compaction_keeps_overlay_bytes_and_source(tmp_path):
    raw=line(STAMP,1,'Alice old').encode()+b'malformed\xff\r\n'
    lake,path=legacy(tmp_path,raw,extra={'future_parser_note':'keep this derived value'})
    before=hashlib.sha256(lake.path.read_bytes()).hexdigest()
    old=lake.rows('gkg_english',STAMP)
    assert old[0]['metadata']['future_parser_note'].startswith('keep')
    destination=tmp_path/'compact.duckdb'
    report=compact_lake(tmp_path,destination,source_stopped=True,chunk_size=1)
    assert report['status']=='verified' and report['records']==2 and not report['activated']
    assert hashlib.sha256(lake.path.read_bytes()).hexdigest()==before
    new=Lake(tmp_path);new.path=destination
    assert new.rows('gkg_english',STAMP)==old
    assert new.raw_bytes('gkg_english',STAMP,2)==b'malformed\xff\r\n'
    assert report['source_logical_sha256']==report['destination_logical_sha256']
    assert path.read_bytes()
    with new.connection() as connection:assert 'intake_records' not in new.tables(connection)


def test_compaction_refuses_live_intent_overwrite_or_missing_evidence(tmp_path):
    lake,path=legacy(tmp_path,line(STAMP,1,'One').encode())
    with pytest.raises(LakeCompactionError,match='Stop'):compact_lake(tmp_path,tmp_path/'copy.db')
    with pytest.raises(LakeCompactionError,match='new file'):compact_lake(tmp_path,lake.path,source_stopped=True)
    path.unlink()
    with pytest.raises(LakeCompactionError,match='archive'):compact_lake(tmp_path,tmp_path/'copy.db',source_stopped=True)
    assert lake.path.exists() and not (tmp_path/'copy.db').exists()
    assert not list(tmp_path.glob('*.partial-*'))


def test_payload_corruption_is_not_returned_as_valid_evidence(tmp_path):
    lake=Lake(tmp_path);lake.ingest_archive('gkg_english',STAMP,raw_zip(tmp_path,line(STAMP,1,'One').encode()))
    with lake.connection() as connection:connection.execute("UPDATE intake_records_v2 SET raw_sha256='wrong'")
    with pytest.raises(ValueError,match='hash'):lake.rows('gkg_english',STAMP)


def test_new_gkg_pg_metadata_is_compact_and_external_payload_untouched(setup):
    engine,storage=setup
    cells=line(STAMP,1,'Alice met Bob.').rstrip('\n').split('\t');cells[17]='c1:1,'*20000
    archive(engine,storage,['\t'.join(cells)+'\n'])
    assert project_step(engine,storage)
    with Session(engine) as session:
        article=session.exec(select(Article)).one();gkg=session.exec(select(GkgSource)).one()
        version=session.exec(select(ArticleVersion)).one()
        for payload in (article.raw_json,gkg.attributes,version.payload_json):
            assert 'c1:1,' not in payload and 'archive_sha256' in payload
        assert json.loads(gkg.attributes)['persons']==['Alice']
    body=dict(provider='industry',record_id='external',uri='https://industry.example/full',title='Full',content='Whole body',metadata={'gcam':'external field must remain'})
    result=import_document(engine,body)
    with Session(engine) as session:
        article=session.exec(select(Article).where(Article.article_key==result['article_key'])).one()
        assert json.loads(article.raw_json)['metadata']==body['metadata']


def test_compaction_mixed_legacy_and_v2_preserves_both_batches(tmp_path):
    lake,path=legacy(tmp_path,line(STAMP,1,'Legacy').encode())
    next_stamp='20261104121500';next_path=tmp_path/'second.zip'
    raw=line(next_stamp,2,'New compact').encode()
    with ZipFile(next_path,'w') as zipped:zipped.writestr(next_stamp+'.gkg.csv',raw)
    lake.ingest_archive('gkg_english',next_stamp,next_path)
    destination=tmp_path/'mixed-copy.duckdb'
    result=compact_lake(tmp_path,destination,source_stopped=True)
    assert result['archives']==2 and result['records']==2
    target=Lake(tmp_path);target.path=destination
    assert target.rows('gkg_english',STAMP)==lake.rows('gkg_english',STAMP)
    assert target.rows('gkg_english',next_stamp)==lake.rows('gkg_english',next_stamp)
    assert target.raw_bytes('gkg_english',next_stamp,1)==raw
    with target.connection() as connection:
        assert lake.tables(connection)=={'intake_records_v2','intake_archives'}


def test_compaction_refuses_orphan_rows_without_dropping_them(tmp_path):
    lake,path=legacy(tmp_path,line(STAMP,1,'Keep').encode())
    with lake.connection() as connection:
        connection.execute("UPDATE intake_records SET stamp='20261104121500'")
    with pytest.raises(LakeCompactionError,match='no archive manifest'):
        compact_lake(tmp_path,tmp_path/'orphan-copy.db',source_stopped=True)
    assert lake.path.exists() and not (tmp_path/'orphan-copy.db').exists()


def test_unsupported_parser_version_does_not_silently_reinterpret_archive(tmp_path):
    raw=line(STAMP,1,'One').encode();lake=Lake(tmp_path)
    lake.ingest_archive('gkg_english',STAMP,raw_zip(tmp_path,raw))
    with lake.connection() as connection:
        connection.execute("UPDATE intake_records_v2 SET parser_version='future-unsupported'")
    with pytest.raises(ValueError,match='parser_version'):lake.rows('gkg_english',STAMP)
    assert lake.raw_bytes('gkg_english',STAMP,1)==raw


def test_default_gkg_discovery_does_not_create_research_event_or_extraction_job(setup,monkeypatch):
    from news2douyin.candidates.promotion import promote
    from news2douyin.candidates.service import candidate_token, list_reports
    from news2douyin.storage.models import Event
    from news2douyin.extraction.models import VersionReading
    engine,storage=setup
    monkeypatch.delenv('NEWS2DOUYIN_GKG_AUTO_EVENTS',raising=False)
    archive(engine,storage,[line(STAMP,1,'Alice met Bob in Paris.')])
    assert project_step(engine,storage)
    with Session(engine) as session:
        assert session.exec(select(Article)).one().title.startswith('Alice')
        assert session.exec(select(Event)).first() is None
        assert session.exec(select(VersionReading)).one().state=='metadata_only'
    # After the connector observation is selected, the same article can enter the event.
    selected=import_document(engine,dict(provider='editorial',record_id='chosen',uri='https://media.example/article/1',
        title='Alice met Bob in Paris.',content='Alice met Bob in Paris yesterday.'))
    with Session(engine) as session:
        assert session.exec(select(Event)).first() is None
        candidate, = list_reports(session,provider='editorial')
        promote(session,storage,candidate.key,expected_token=candidate_token(candidate),request_id='choose-editorial-report')
        assert session.exec(select(Event)).first() is not None
    next_stamp='20261104121500'
    archive(engine,storage,[line(next_stamp,1,'New GKG title')],stamp=next_stamp)
    assert project_step(engine,storage)
    with Session(engine) as session:
        assert session.exec(select(Event)).first() is not None
        assert session.exec(select(Article).where(Article.article_key==selected['article_key'])).one().content


def test_compaction_refuses_unknown_user_tables_instead_of_omitting_them(tmp_path):
    lake,path=legacy(tmp_path,line(STAMP,1,'Keep').encode())
    with lake.connection() as connection:
        connection.execute('CREATE TABLE my_analysis AS SELECT 42 AS value')
    with pytest.raises(LakeCompactionError,match='non-lake tables'):
        compact_lake(tmp_path,tmp_path/'copy.db',source_stopped=True)
    with lake.connection() as connection:assert connection.execute('SELECT value FROM my_analysis').fetchone()==(42,)
