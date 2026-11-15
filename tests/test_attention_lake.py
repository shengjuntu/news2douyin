"""Real immutable archives, DuckDB replay, and conservative observed attention."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
import subprocess
import sys

import duckdb
import pytest

from news2douyin.attention.lake import AttentionLake, AttentionLakeBusy
from news2douyin.collect.newsnow_archive import persist_batch


def write(root, name, sources, *, observed='2026-11-14T06:00:00Z', updated='2026-11-14T05:00:00Z'):
    reports, snapshots = [], []
    for source, status, items in sources:
        reports.append(dict(source_id=source, status=status, observed_at=observed,
            error_code='request_failed' if status=='failed' else ''))
        if status=='failed':
            continue
        payload=dict(id=source, status='cache', updatedTime=updated, items=items)
        snapshots.append(dict(source_id=source, observed_at=observed, response_status='cache',
            snapshot_reported_at=updated, payload=payload))
    report=persist_batch(root,root/name,dict(summary={},sources=reports,snapshots=snapshots))
    path=root/report['manifest_path']
    return report['manifest_path'],hashlib.sha256(path.read_bytes()).hexdigest()


def item(key, title=None):
    return dict(id=str(key), title=title or f'具体新闻事件{key}', url=f'https://example.com/news/{key}',
        extra=dict(info='2.3万热度',hover='来源摘要，仍需核验原文'),score=23000)


def ingest(lake, root, name, sources, **kwargs):
    return lake.ingest_manifest(*write(root,name,sources,**kwargs))


def test_full_archive_replay_is_idempotent_and_cache_report_clock_is_not_new_content(tmp_path):
    lake=AttentionLake(tmp_path)
    source=[('zhihu','success',[item(1),item(2)])]
    path,sha=write(tmp_path,'one',source)
    first=lake.ingest_manifest(path,sha)
    second=ingest(lake,tmp_path,'two',source,observed='2026-11-14T06:10:00Z',updated='2026-11-14T06:09:00Z')
    assert first['sources'][0]['content_key']==second['sources'][0]['content_key']
    assert not second['sources'][0]['changed']
    assert lake.ingest_manifest(path,sha)==first
    key=first['sources'][0]['items'][1]['key']
    metrics=lake.metrics([key],now='2026-11-14T06:15:00Z')
    assert metrics['rise_score']==0
    assert metrics['platform_count']==1
    assert metrics['platforms'][0]['list_position']==2
    assert metrics['platforms'][0]['cached']
    assert metrics['platforms'][0]['continuity']=='cached_content_repeat'
    assert metrics['platforms'][0]['content_first_observed_at']=='2026-11-14T06:00:00Z'
    assert metrics['platforms'][0]['upstream_freshness']=='unknown'
    assert metrics['series'][0]['observed_at']=='2026-11-14T06:00:00Z'
    with duckdb.connect(str(lake.path),read_only=True) as db:
        assert db.execute('SELECT count(*) FROM attention_contents').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM attention_observations').fetchone()[0]==2
        stored=db.execute('SELECT batch_json FROM attention_receipts LIMIT 1').fetchone()[0]
        assert '具体新闻' not in stored  # receipts hold references, not duplicated lists


def test_comparable_position_change_and_first_observation_no_rise(tmp_path):
    lake=AttentionLake(tmp_path)
    first=ingest(lake,tmp_path,'one',[('zhihu','success',[item(1),item(2),item(3)])])
    key=first['sources'][0]['items'][2]['key']
    initial=lake.metrics([key],now='2026-11-14T06:01:00Z')
    assert initial['rise_score'] is None
    assert initial['baseline_state']=='insufficient'
    ingest(lake,tmp_path,'two',[('zhihu','success',[item(3),item(1),item(2)])],observed='2026-11-14T06:20:00Z')
    result=lake.metrics([key],now='2026-11-14T06:21:00Z')
    assert result['rise_score']==100
    assert result['platforms'][0]['position_delta']==2
    assert result['platforms'][0]['previous_list_position']==3
    assert result['attention_score']==100


def test_new_topic_member_does_not_inherit_another_items_previous_position(tmp_path):
    lake=AttentionLake(tmp_path)
    old=ingest(lake,tmp_path,'one',[('zhihu','success',[item(9),item(8),item(1)])])
    new=ingest(lake,tmp_path,'two',[('zhihu','success',[item(2),item(9),item(1)])],observed='2026-11-14T06:10:00Z')
    keys=[old['sources'][0]['items'][2]['key'],new['sources'][0]['items'][0]['key']]
    result=lake.metrics(keys,now='2026-11-14T06:11:00Z')
    assert result['platforms'][0]['list_position']==1
    assert result['platforms'][0]['previous_list_position'] is None
    assert result['rise_score'] is None


@pytest.mark.parametrize('status,items',[('failed',[]),('empty',[]),('success',[item(2)])])
def test_failed_empty_or_missing_is_unknown_not_zero_or_a_fall(tmp_path,status,items):
    lake=AttentionLake(tmp_path)
    first=ingest(lake,tmp_path,'one',[('zhihu','success',[item(1)])])
    key=first['sources'][0]['items'][0]['key']
    ingest(lake,tmp_path,'two',[('zhihu',status,items)],observed='2026-11-14T06:10:00Z')
    result=lake.metrics([key],now='2026-11-14T06:11:00Z')
    assert result['attention_score'] is None
    assert result['rise_score'] is None
    assert result['platforms'][0]['list_position'] is None
    assert result['platform_count']==0
    assert result['series'][-1]['attention_score'] is None


def test_source_types_platform_aliases_and_unknowns_do_not_inflate_chinese_coverage(tmp_path):
    lake=AttentionLake(tmp_path)
    batch=ingest(lake,tmp_path,'one',[(source,'success',[item(1)]) for source in
        ('tencent','tencent-hot','github','producthunt','xueqiu','ithome','custom-feed')])
    keys=[row['items'][0]['key'] for row in batch['sources']]
    result=lake.metrics(keys,now='2026-11-14T06:01:00Z')
    assert result['platform_count']==1
    by_source={row['source_id']:row for row in result['platforms']}
    assert by_source['tencent-hot']['counts_for_attention']
    assert by_source['github']['audience_scope']=='global'
    assert by_source['custom-feed']['source_kind']=='unknown'
    assert not by_source['xueqiu']['counts_for_attention']
    assert not by_source['ithome']['counts_for_attention']


def test_out_of_order_replay_does_not_replace_current_state(tmp_path):
    lake=AttentionLake(tmp_path)
    recent=ingest(lake,tmp_path,'recent',[('zhihu','success',[item(2),item(1)])],observed='2026-11-14T07:00:00Z')
    ingest(lake,tmp_path,'old',[('zhihu','success',[item(1),item(2)])],observed='2026-11-14T06:00:00Z')
    key=recent['sources'][0]['items'][0]['key']
    result=lake.metrics([key],now='2026-11-14T07:01:00Z')
    assert result['platforms'][0]['list_position']==1
    assert result['platforms'][0]['position_delta']==1
    assert result['platforms'][0]['observed_at']=='2026-11-14T07:00:00Z'


def test_stale_not_continuously_observed_and_chart_is_bounded(tmp_path):
    lake=AttentionLake(tmp_path); key=None
    for minute in range(8):
        batch=ingest(lake,tmp_path,f'run{minute}',[('zhihu','success',[item(1)])],
            observed=f'2026-11-14T06:{minute:02}:00Z')
        key=batch['sources'][0]['items'][0]['key']
    result=lake.metrics([key],now='2026-11-14T06:10:00Z',max_points=3,hours=1)
    assert len(result['series'])<=3
    assert len(result['platforms'][0]['observations'])<=8
    result=lake.metrics([key],now='2026-11-14T13:00:00Z')
    assert result['attention_score'] is None
    assert result['platform_count']==0
    assert result['platforms'][0]['stale']


def test_manifest_and_archive_hashes_path_escape_and_replay_corruption_fail_closed(tmp_path):
    lake=AttentionLake(tmp_path)
    path,sha=write(tmp_path,'one',[('zhihu','success',[item(1)])])
    with pytest.raises(ValueError,match='checksum'):
        lake.ingest_manifest(path,'0'*64)
    batch=lake.ingest_manifest(path,sha)
    archive=tmp_path/batch['sources'][0]['archive_ref']
    archive.write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='checksum'):
        lake.ingest_manifest(path,sha)
    with pytest.raises(ValueError,match='inside storage_root'):
        lake.ingest_manifest('../outside.json',sha)
    assert archive.read_bytes()==b'corrupt'


def test_failed_response_is_verified_as_bytes_without_requiring_valid_nlp_data(tmp_path):
    import base64
    lake=AttentionLake(tmp_path)
    # JSON exponent overflow is a failed source response, not a legitimate
    # numeric heat value. Preserve its evidence without poisoning other sources.
    body=b'{"error":"unavailable","value":1e400}'
    report=persist_batch(tmp_path,tmp_path/'failed',dict(summary={},sources=[dict(source_id='zhihu',
        status='failed',observed_at='2026-11-14T06:00:00Z',error_code='invalid_json')],snapshots=[dict(
        source_id='zhihu',observed_at='2026-11-14T06:00:00Z',payload=None,
        raw_body_b64=base64.b64encode(body).decode(),raw_sha256=hashlib.sha256(body).hexdigest())]))
    path=tmp_path/report['manifest_path']
    batch=lake.ingest_manifest(report['manifest_path'],hashlib.sha256(path.read_bytes()).hexdigest())
    assert batch['sources'][0]['status']=='failed'
    assert batch['sources'][0]['items']==[]
    assert (tmp_path/batch['sources'][0]['archive_ref']).read_bytes()==body


def test_invalid_later_source_rolls_back_all_and_retry_after_correct_receipt(tmp_path):
    lake=AttentionLake(tmp_path)
    good=write(tmp_path,'good',[('zhihu','success',[item(1)])])
    report=json.loads((tmp_path/good[0]).read_text())
    report['sources'].append(dict(source_id='weibo',status='success',observed_at='2026-11-14T06:00:00Z'))
    report['manifest_path']='bad/newsnow-snapshots.json'
    bad=tmp_path/report['manifest_path'];bad.parent.mkdir()
    bad.write_text(json.dumps(report))
    with pytest.raises(ValueError,match='valid archived list'):
        lake.ingest_manifest(str(bad),hashlib.sha256(bad.read_bytes()).hexdigest())
    assert not lake.path.exists()
    assert lake.ingest_manifest(*good)['sources'][0]['items']


def test_interrupted_duckdb_transaction_rolls_back_and_same_receipt_recovers(tmp_path,monkeypatch):
    lake=AttentionLake(tmp_path)
    args=write(tmp_path,'one',[('zhihu','success',[item(1)]),('weibo','success',[item(2)])])
    original=lake._connect

    class Interrupt:
        def __init__(self,db):
            self.db=db; self.inserts=0
        def execute(self,sql,*args):
            if sql.startswith('INSERT INTO attention_observations'):
                self.inserts+=1
                if self.inserts==2:
                    raise RuntimeError('simulated interrupted writer')
            return self.db.execute(sql,*args)
        def __getattr__(self,name):
            return getattr(self.db,name)

    @contextmanager
    def interrupted():
        with original() as db:
            yield Interrupt(db)

    monkeypatch.setattr(lake,'_connect',interrupted)
    with pytest.raises(RuntimeError,match='interrupted writer'):
        lake.ingest_manifest(*args)
    monkeypatch.setattr(lake,'_connect',original)
    with lake._connect() as db:
        for table in ('attention_receipts','attention_contents','attention_content_items','attention_observations'):
            assert db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]==0
    result=lake.ingest_manifest(*args)
    assert len(result['sources'])==2
    assert lake.ingest_manifest(*args)==result


def test_first_seen_is_lifetime_and_future_observations_do_not_leak(tmp_path):
    lake=AttentionLake(tmp_path)
    old=ingest(lake,tmp_path,'old',[('zhihu','success',[item(1)])],observed='2026-11-01T06:00:00Z')
    key=old['sources'][0]['items'][0]['key']
    ingest(lake,tmp_path,'recent',[('zhihu','success',[item(1)])],observed='2026-11-14T06:00:00Z')
    ingest(lake,tmp_path,'future',[('zhihu','success',[item(2),item(1)])],observed='2026-11-15T06:00:00Z')
    result=lake.metrics([key],now='2026-11-14T06:01:00Z')
    assert result['first_seen']=='2026-11-01T06:00:00Z'
    assert result['last_seen']=='2026-11-14T06:00:00Z'
    assert result['observed_at']=='2026-11-14T06:00:00Z'
    assert result['platforms'][0]['list_position']==1
    assert result['platforms'][0]['content_first_observed_at']=='2026-11-01T06:00:00Z'


def test_metrics_many_shares_one_connection_and_matches_individual_results(tmp_path,monkeypatch):
    lake=AttentionLake(tmp_path)
    batch=ingest(lake,tmp_path,'one',[('zhihu','success',[item(1),item(2)]),('weibo','success',[item(1)])])
    by_source={row['source_id']:row for row in batch['sources']}
    mapping={'both':[by_source['zhihu']['items'][0]['key'],by_source['weibo']['items'][0]['key']],
        'other':[by_source['zhihu']['items'][1]['key']], 'unknown':['0'*64], 'empty':[]}
    original=lake._connect
    opened=[]
    @contextmanager
    def counted():
        opened.append(True)
        with original() as db:
            yield db
    monkeypatch.setattr(lake,'_connect',counted)
    many=lake.metrics_many(mapping,now='2026-11-14T06:01:00Z')
    assert len(opened)==1
    for topic,keys in mapping.items():
        assert many[topic]==lake.metrics(keys,now='2026-11-14T06:01:00Z')


def test_same_store_thread_writers_serialize_and_receipt_deduplicates(tmp_path):
    args=write(tmp_path,'one',[('zhihu','success',[item(1),item(2)])])
    def run(_):
        return AttentionLake(tmp_path).ingest_manifest(*args)
    with ThreadPoolExecutor(max_workers=4) as pool:
        batches=list(pool.map(run,range(8)))
    assert all(batch==batches[0] for batch in batches)
    with duckdb.connect(str(AttentionLake(tmp_path).path),read_only=True) as db:
        assert db.execute('SELECT count(*) FROM attention_receipts').fetchone()[0]==1


@pytest.mark.skipif(sys.platform=='win32',reason='POSIX ownership integration')
def test_other_process_cannot_open_owned_store(tmp_path):
    import fcntl
    lake=AttentionLake(tmp_path)
    lake.lock_path.parent.mkdir(parents=True)
    with lake.lock_path.open('a+b') as handle:
        fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        code='''import sys
from news2douyin.attention.lake import AttentionLake,AttentionLakeBusy
try:
    with AttentionLake(sys.argv[1])._connect(): pass
except AttentionLakeBusy: sys.exit(0)
sys.exit(3)
'''
        env=dict(os.environ,PYTHONPATH=os.path.abspath('src'))
        child=subprocess.run([sys.executable,'-c',code,str(tmp_path)],env=env,capture_output=True,text=True,timeout=20)
        assert child.returncode==0,child.stderr
    with lake._connect() as db:
        assert db.execute('SELECT 1').fetchone()==(1,)
