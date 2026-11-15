"""Actual files prove immutable evidence reuse and small durable checkpoints."""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

import pytest

from news2douyin.collect import newsnow_archive as archive


def batch(payload=None, *, body=None, observed='2026-11-14T06:00:00Z'):
    payload = payload if payload is not None else {'status':'cache','id':'zhihu','items':[
        {'id':'1','title':'具体事件甲','url':'https://news.example/1'},
        {'id':'2','title':'具体事件乙','url':'https://news.example/2'}]}
    body = body if body is not None else json.dumps(payload,ensure_ascii=False,indent=2).encode('utf-8')
    return dict(items=[],summary={'status':'complete','succeeded_sources':1},sources=[
        dict(source_id='zhihu',status='success',items=len(payload['items']))],snapshots=[dict(
        source_id='zhihu',observed_at=observed,response_status='cache',
        snapshot_reported_at='2026-11-14T05:30:00Z',snapshot_time_basis='upstream_reported',
        payload=payload,raw_sha256=hashlib.sha256(body).hexdigest(),
        raw_body_b64=base64.b64encode(body).decode('ascii'),content_type='application/json')])


def test_cached_body_reused_but_each_observation_manifest_remains(tmp_path):
    root=tmp_path/'runs'
    first=batch()
    report=archive.persist_batch(root,root/'day'/'one',first)
    assert archive.persist_batch(root,root/'day'/'one',first)==report
    snapshot=report['snapshots'][0]
    original=base64.b64decode(first['snapshots'][0]['raw_body_b64'])
    assert (root/snapshot['archive_ref']).read_bytes()==original
    again=archive.persist_batch(root,root/'day'/'two',batch(observed='2026-11-14T06:10:00Z'))
    assert again['snapshots'][0]['archive_ref']==snapshot['archive_ref']
    assert len(list((root/'newsnow/snapshots').glob('*.json')))==1
    assert report['manifest_path']!=again['manifest_path']
    assert report['snapshots'][0]['snapshot_reported_at']!=report['snapshots'][0]['observed_at']
    text=json.dumps(report)
    assert 'raw_body_b64' not in text and '"payload"' not in text


def test_changed_list_order_is_separate_evidence_without_overwriting_old_body(tmp_path):
    root=tmp_path/'runs';initial=batch()
    first=archive.persist_batch(root,root/'one',initial)
    changed=json.loads(base64.b64decode(initial['snapshots'][0]['raw_body_b64']))
    changed['items'].reverse()
    second=archive.persist_batch(root,root/'two',batch(changed))
    assert first['snapshots'][0]['archive_sha256']!=second['snapshots'][0]['archive_sha256']
    assert json.loads((root/first['snapshots'][0]['archive_ref']).read_bytes())['items'][0]['id']=='1'
    assert json.loads((root/second['snapshots'][0]['archive_ref']).read_bytes())['items'][0]['id']=='2'


def test_canonical_fallback_does_not_claim_raw_transport_hash(tmp_path):
    value=batch();snapshot=value['snapshots'][0]
    snapshot.pop('raw_body_b64');snapshot['raw_sha256']='f'*64
    canonical=json.dumps(snapshot['payload'],ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
    snapshot['payload_sha256']=hashlib.sha256(canonical).hexdigest()
    report=archive.persist_batch(tmp_path,tmp_path/'one',value)
    saved=report['snapshots'][0]
    assert saved['representation']=='canonical_json'
    assert saved['archive_sha256']==snapshot['payload_sha256']
    assert (tmp_path/saved['archive_ref']).read_bytes()==canonical


def test_error_html_and_no_response_have_honest_archive_references(tmp_path):
    body=b'<!doctype html><title>temporarily unavailable</title>'
    value=dict(items=[],summary={'status':'failed'},sources=[{'source_id':'bad','status':'failed'},
        {'source_id':'offline','status':'failed'}],snapshots=[dict(source_id='bad',response_status='error',
        raw_body_b64=base64.b64encode(body).decode(),raw_sha256=hashlib.sha256(body).hexdigest(),payload=None,
        content_type='text/html'),dict(source_id='offline',payload=None)])
    report=archive.persist_batch(tmp_path,tmp_path/'failed',value)
    bad,offline=report['snapshots']
    assert bad['archive_ref'].endswith('.body') and not bad['json_valid']
    assert (tmp_path/bad['archive_ref']).read_bytes()==body
    assert offline['archive_ref']=='' and offline['representation']=='no_response'
    assert report['summary']['status']=='failed'


def test_checksum_mismatch_or_payload_disagreement_never_publish_manifest(tmp_path):
    value=batch();value['snapshots'][0]['raw_sha256']='0'*64
    with pytest.raises(ValueError,match='checksum'):
        archive.persist_batch(tmp_path,tmp_path/'bad',value)
    assert not (tmp_path/'bad/newsnow-snapshots.json').exists()
    value=batch();value['snapshots'][0]['payload']['items'][0]['title']='changed after capture'
    with pytest.raises(ValueError,match='does not match'):
        archive.persist_batch(tmp_path,tmp_path/'bad',value)
    assert not (tmp_path/'bad/newsnow-snapshots.json').exists()


def test_existing_corrupt_archive_is_refused_and_source_not_replaced(tmp_path):
    value=batch();first=archive.persist_batch(tmp_path,tmp_path/'one',value)
    path=tmp_path/first['snapshots'][0]['archive_ref'];path.write_bytes(b'corrupt')
    with pytest.raises(ValueError,match='checksum'):
        archive.persist_batch(tmp_path,tmp_path/'two',value)
    assert path.read_bytes()==b'corrupt'
    assert not (tmp_path/'two/newsnow-snapshots.json').exists()


def test_archive_paths_and_limits_reject_before_publishing(tmp_path,monkeypatch):
    with pytest.raises(ValueError,match='inside'):
        archive.persist_batch(tmp_path/'root',tmp_path/'outside',batch())
    value=batch();value['snapshots'][0]['source_id']='../escape'
    with pytest.raises(ValueError,match='identifier'):
        archive.persist_batch(tmp_path,tmp_path/'one',value)
    monkeypatch.setattr(archive,'MAX_RESPONSE_BYTES',10)
    with pytest.raises(ValueError,match='byte limit'):
        archive.persist_batch(tmp_path,tmp_path/'one',batch())
    assert not (tmp_path/'one/newsnow-snapshots.json').exists()


def test_concurrent_same_snapshot_and_manifest_publish_complete_identical_files(tmp_path):
    value=batch()
    with ThreadPoolExecutor(max_workers=4) as pool:
        reports=list(pool.map(lambda _:archive.persist_batch(tmp_path,tmp_path/'one',value),range(8)))
    assert all(report==reports[0] for report in reports)
    assert not list(tmp_path.rglob('.newsnow-*'))
    assert json.loads((tmp_path/reports[0]['manifest_path']).read_text(encoding='utf-8'))==reports[0]


def test_large_stages_have_bounded_sql_pointer_and_frozen_reload(tmp_path):
    rows=[{'title':'A saved title '+str(index),'content':'资料'*300} for index in range(1000)]
    reference=archive.save_stage(tmp_path,tmp_path/'one','raw',rows)
    assert len(json.dumps(reference))<300 and reference['count']==1000
    assert archive.load_stage(tmp_path,reference)==rows
    assert archive.save_stage(tmp_path,tmp_path/'one','raw',rows)==reference
    filtered={'items':rows[:2],'mode':'rules','diagnostics':{'kept':2}}
    pointer=archive.save_stage(tmp_path,tmp_path/'one','filtered',filtered)
    assert archive.load_stage(tmp_path,pointer)==filtered
    with pytest.raises(ValueError,match='checksum'):
        archive.save_stage(tmp_path,tmp_path/'one','raw',rows[:1])
    assert archive.load_stage(tmp_path,reference)==rows


def test_stage_missing_corrupt_or_escaping_pointer_refuses_silent_refetch(tmp_path):
    pointer=archive.save_stage(tmp_path,tmp_path/'one','raw',[{'title':'original'}])
    escaped=dict(pointer,path='../outside/newsnow-stage-raw.json')
    with pytest.raises(ValueError,match='invalid'):
        archive.load_stage(tmp_path,escaped)
    path=tmp_path/pointer['path'];path.write_bytes(b'[{}]')
    with pytest.raises(ValueError,match='checksum'):
        archive.load_stage(tmp_path,pointer)
    path.unlink()
    with pytest.raises(ValueError,match='missing'):
        archive.load_stage(tmp_path,pointer)


def test_symlinked_archive_directory_cannot_write_outside_storage_root(tmp_path):
    root=tmp_path/'runs';outside=tmp_path/'outside'
    root.mkdir();outside.mkdir()
    try:(root/'newsnow').symlink_to(outside,target_is_directory=True)
    except (OSError,NotImplementedError):pytest.skip('symlink creation is unavailable')
    with pytest.raises(ValueError,match='inside'):
        archive.persist_batch(root,root/'one',batch())
    assert list(outside.iterdir())==[]
    assert not (root/'one/newsnow-snapshots.json').exists()


def test_batch_budget_is_checked_before_any_content_is_published(tmp_path,monkeypatch):
    value=batch()
    body=base64.b64decode(value['snapshots'][0]['raw_body_b64'])
    monkeypatch.setattr(archive,'MAX_BATCH_BYTES',len(body)-1)
    with pytest.raises(ValueError,match='batch exceeds'):
        archive.persist_batch(tmp_path,tmp_path/'one',value)
    assert not (tmp_path/'newsnow').exists()
    assert not (tmp_path/'one').exists()
