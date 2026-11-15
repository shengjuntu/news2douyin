"""Measure a synthetic legacy-to-V2 lake copy; never open production files.

Requires DuckDB. All source DBs and ZIPs live in a new temporary directory.
The output reports real file bytes and verified record hashes, not an estimate
of savings on the user's archive. Run: python tools/benchmark_intake_storage.py
--output docs/validation/0.26.0/intake-storage-benchmark.json
"""
import argparse
import json
from pathlib import Path
import random
import sys
import tempfile
import time
from zipfile import ZipFile, ZIP_DEFLATED

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from news2douyin.intake.lake import decoded_row, hashes
from news2douyin.intake.lake_migrate import compact_lake

LEGACY='''CREATE TABLE intake_records(feed VARCHAR, stamp VARCHAR, row_index BIGINT, record_id VARCHAR,
 document_date VARCHAR,url VARCHAR,title VARCHAR,metadata_json VARCHAR,valid BOOLEAN,error VARCHAR,
 raw_line VARCHAR,observed_at VARCHAR,PRIMARY KEY(feed,stamp,row_index));
 CREATE TABLE intake_archives(feed VARCHAR,stamp VARCHAR,sha256 VARCHAR,md5 VARCHAR,compressed_bytes BIGINT,
 archive_path VARCHAR,rows BIGINT,valid_rows BIGINT,invalid_rows BIGINT,completed_at VARCHAR,PRIMARY KEY(feed,stamp));'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--rows',type=int,default=1024)
    args=parser.parse_args()
    if not 100<=args.rows<=10000:parser.error('rows must be between100 and10000')
    import duckdb
    with tempfile.TemporaryDirectory(prefix='n2d-storage-benchmark-') as folder:
        root=Path(folder);intake=root/'intake';intake.mkdir()
        stamp='20261114000000';feed='gkg_english';observed='2026-11-14T00:00:00Z'
        path=intake/'raw'/feed/(stamp+'.gkg.csv.zip');path.parent.mkdir(parents=True)
        rng=random.Random(260);records=[];raw_bytes=raw_chars=0
        with ZipFile(path,'w',compression=ZIP_DEFLATED) as archive:
            with archive.open(stamp+'.gkg.csv','w') as member:
                for index in range(1,args.rows+1):
                    columns=['']*27
                    columns[0]=stamp+'-'+str(index);columns[1]=stamp;columns[2]='1'
                    columns[3]='fixture.invalid';columns[4]='https://fixture.invalid/'+str(index)
                    columns[7]='DIPLOMACY;ECONOMY';columns[9]='1#China#CH#CH#35#105#CH'
                    columns[11]='Alice;Bob';columns[13]='Example Organization'
                    columns[17]=','.join('c'+str(code)+'.1:'+str(rng.randrange(1000)) for code in range(1000))
                    columns[26]='<PAGE_TITLE>合成资料 '+str(index)+'</PAGE_TITLE>'
                    raw=('\t'.join(columns)+'\n').encode();member.write(raw)
                    row=decoded_row(raw,feed,stamp,index,observed)
                    assert row['valid']
                    raw_bytes+=len(raw);raw_chars+=len(row['raw_line'])
                    records.append([row[name] for name in ('feed','stamp','row_index','record_id','document_date','url','title')]+
                        [json.dumps(row['metadata'],ensure_ascii=False),row['valid'],row['error'],row['raw_line'],observed])
        source=intake/'records.duckdb';connection=duckdb.connect(str(source));connection.execute(LEGACY)
        connection.executemany('INSERT INTO intake_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',records)
        sha,md5,size=hashes(path)
        connection.execute('INSERT INTO intake_archives VALUES (?,?,?,?,?,?,?,?,?,?)',
            [feed,stamp,sha,md5,size,str(path),args.rows,args.rows,0,observed])
        connection.execute('CHECKPOINT');connection.close()
        started=time.perf_counter()
        result=compact_lake(root,root/'records-compact.duckdb',source_stopped=True,chunk_size=128)
        elapsed=(time.perf_counter()-started)*1000
        report=dict(synthetic=True,duckdb_version=duckdb.__version__,rows=args.rows,
            fixture='One GKG batch;1000 varying GCAM key/count entries per row;UTF8Chinese titles;seed260',
            raw_utf8_bytes=raw_bytes,decoded_text_characters_without_newlines=raw_chars,archive_bytes=size,
            legacy_duckdb_bytes=result['source_bytes'],compact_duckdb_bytes=result['destination_bytes'],
            measured_difference_bytes=result['source_bytes']-result['destination_bytes'],
            copy_verify_ms=round(elapsed,3),verified=result['status']=='verified',source_preserved=result['source_preserved'],
            activated=result['activated'],logical_hashes_equal=result['source_logical_sha256']==result['destination_logical_sha256'],
            limitations='Small synthetic fixture, not fielddata;filegranularity/compression/contentvary, no extrapolation to58.75GiB')
        args.output.parent.mkdir(parents=True,exist_ok=True);args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
