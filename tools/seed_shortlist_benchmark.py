"""Create a disposable synthetic GKG lake for the four-arm shortlist replay.

Example (the input directory must be new):
    python -m pip install numpy   # DuckDB's Python scalar UDF for fixture zlib encoding
    python tools/seed_shortlist_benchmark.py bench-data --rows 500000 --day 2026-11-12
    news2douyin-daily-radar build --day 2026-11-12 --db-url sqlite:///bench-data/work.db --storage-root bench-data
    news2douyin-review compare-shortlist --day 2026-11-12 --db-url sqlite:///bench-data/work.db --storage-root bench-data --out bench-comparison

Both feeds receive synthetic rows at one fixed local observation time. Raw records
have valid hashes, compressed payloads, themes, location mentions and organisations.
This measures adapter and ranking costs. It does not model newsroom quality.
"""
import argparse
from datetime import date, timedelta
import json
from pathlib import Path
import sys
import zlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from sqlmodel import Session
from news2douyin.desk.models import Watch
from news2douyin.digest.service import last_closed_day
from news2douyin.intake.lake import Lake
from news2douyin.storage.db import init_db, make_engine


def seed(root, day, rows):
    root.mkdir(parents=True, exist_ok=False)
    src = Lake(root)
    batches = min(rows, 192)
    received = str(day) + 'T04:00:00Z'
    with src.connection(create=True) as conn:
        conn.execute("SET threads=2")
        conn.execute("SET memory_limit='2GB'")
        conn.create_function('compress_blob', lambda b: zlib.compress(b, 3), ['BLOB'], 'BLOB')
        # Scalable seed with different title, URL, raw payload, hashes, search projection and coded location per article.
        conn.execute(r"""
            CREATE TEMP TABLE tmp_story AS WITH ranges AS (
             SELECT i,(i*$batches//$rows)::INTEGER batch, CASE WHEN i%10=9 THEN 20000+i WHEN i%10<6 THEN (i//10)%400 ELSE 400+((i//10)%12000) END AS sid
             FROM range($rows) s(i)),
            arcs AS (SELECT *, CASE WHEN batch%2=0 THEN 'gkg_english' ELSE 'gkg_translation' END AS feed,
              strftime($midnight::TIMESTAMP+(batch//2)*INTERVAL '15 minutes','%Y%m%d%H%M%S') AS stamp,
              row_number() OVER (PARTITION BY batch ORDER BY i) AS row_index,
              'https://paper-'||((13*i+i//23)%89)::VARCHAR||'.example/r/'||md5(sid::VARCHAR) AS url,
              (['LEGISLATION','ENV_CLIMATECHANGE','ECON_TRADE_DISPUTE','EDUCATION','MEDICAL','AGRICULTURE','ARREST','MILITARY'])[1+(sid%8)] AS theme,
              (['UK','CA','FR','BR','US','MX','JA','IN'])[1+((sid//8)%8)] AS code,
              (['gb','ca','fr','br','us','mx','jp','in'])[1+((sid//8)%8)] AS iso,
              'City-'||(sid%300)::VARCHAR AS place, 'Organization '||substr(md5(sid::VARCHAR),1,12) AS actor,
              'District '||substr(md5(sid::VARCHAR),1,12) AS district FROM ranges),
            hl AS (SELECT *, CASE WHEN i%100=1 THEN '' WHEN sid%29=0 THEN 'Movie review: '||district||' and the city'
                     WHEN sid%31=0 THEN 'Save up to '||(sid%90)::VARCHAR||'% on outdoor clothing in '||district
                     WHEN sid%23=0 THEN district||' launched the Orbiter project '||sid::VARCHAR
                     ELSE district||' approved a public works project '||sid::VARCHAR END AS title FROM arcs)
            SELECT * FROM hl ORDER BY feed,stamp,row_index""",
            {"batches": batches, "rows": rows, "midnight":str(day)+" 00:00:00"})
        conn.execute(r"""INSERT INTO intake_records_v2 (feed,stamp,row_index,record_id,document_date,url,title,valid,error,observed_at,search_text,payload,codec,raw_sha256,raw_bytes)
            WITH rows AS (
             SELECT *,stamp||'-'||(i+1)::VARCHAR AS rid,'1#'||place||'#'||code||'#00#0#0#0' loc,
              '1#'||place||'#'||code||'#00#00#0#0#0#45' enhancedloc FROM tmp_story),
            raw_material AS (
             SELECT *, concat_ws(chr(9),rid,stamp,'1','source',url,'','',theme,theme||',32',loc,enhancedloc,'','',actor,actor||',15','','',
                                'wc:1200,'||repeat('c1.5:1,',32),'','','','','','','','','<PAGE_TITLE>'||title||'</PAGE_TITLE>') AS raw FROM rows)
            SELECT feed,stamp,row_index,rid,stamp,url,title,i%100<>2,CASE WHEN i%100=2 THEN 'synthetic_invalid' ELSE '' END,
               $received,lower(concat(title,' ',url,' [] [''',actor,'''] [''',theme,'''] [''',place,'''] [''',iso,''']       ')),
               compress_blob(encode(raw)),'zlib',sha256(raw),octet_length(encode(raw)) FROM raw_material ORDER BY feed,stamp,row_index""", {"received":received})
        conn.execute("""INSERT INTO intake_archives(feed,stamp,rows,valid_rows,invalid_rows,completed_at)
            SELECT feed,stamp,count(*),count(*) FILTER(WHERE valid),count(*) FILTER(WHERE NOT valid),$received
            FROM intake_records_v2 GROUP BY feed,stamp""", {"received":received})
        counts=conn.execute("SELECT count(*),sum(raw_bytes),sum(octet_length(payload)),min(raw_bytes),max(raw_bytes) FROM intake_records_v2").fetchone()
        check=conn.execute("SELECT payload FROM intake_records_v2 WHERE valid AND title<>'' LIMIT 1").fetchone()
        if check and len(zlib.decompress(check[0]).decode().split('\t')) != 27:
            raise RuntimeError('Invalid generated GKG source row')
    engine = make_engine(f'sqlite:///{root}/work.db')
    try:
        init_db(engine)
        created = str(day - timedelta(days=1)) + 'T00:00:00Z'
        with Session(engine) as session:
            for n in range(100):
                session.add(Watch(key=f'bench-{n}', name=f'QA {n}', kind='长期事件',
                                  scope=f'Regional fixture {n}', enabled=True, created=created))
            session.commit()
    finally:
        engine.dispose()
    record = dict(rows=counts[0], raw_bytes=counts[1], compressed_payload_bytes=counts[2],
                  min_raw_bytes=counts[3], max_raw_bytes=counts[4], duckdb_measured_bytes=src.path.stat().st_size,
                  archives=batches, day=str(day), enabled_watches=100)

    (root/'fixture.json').write_text(json.dumps(record, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(record))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--day', type=date.fromisoformat, default=last_closed_day())
    parser.add_argument('--rows', type=int, default=500000)
    args = parser.parse_args()
    if args.rows < 1 or args.day > last_closed_day():
        parser.error('Use a closed Beijing day and a positive row count')
    seed(args.directory.resolve(), args.day, args.rows)
