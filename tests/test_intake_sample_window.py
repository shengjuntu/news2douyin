"""Window rates use real elapsed time and offset deltas, never missing=zero."""
import importlib.util
from pathlib import Path


def sampler():
    path=Path(__file__).resolve().parents[1]/'tools/sample_intake_status.py'
    spec=importlib.util.spec_from_file_location('intake_sample_window_qa',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def sample(at,rows,processed,waiting,feeds=None):
    return dict(elapsed_seconds=at,data=dict(database_ok=True,counters=dict(rows=rows),
        projection=dict(processed_rows=processed,waiting=waiting,projected=None,skipped=None,
            counts_scope='since_upgrade_only',per_feed=feeds or [])))


def test_elapsed_window_uses_offset_progress_even_when_old_classified_totals_are_unknown():
    output=sampler().summarize([
        sample(0,1000,200,800,[dict(feed='gkg_english',processed_rows=100)]),
        dict(elapsed_seconds=5,error='Timeout'),
        sample(10,1080,250,830,[dict(feed='gkg_english',processed_rows=130),dict(feed='events_english',processed_rows=20)])])
    assert output['successful_samples']==2 and output['failed_samples']==1
    assert output['archived']==dict(delta=80,per_second=8,state='observed')
    assert output['processed']==dict(delta=50,per_second=5,state='observed')
    feeds={row['feed']:row for row in output['per_feed']}
    assert feeds['gkg_english']['processed']['per_second']==3
    assert feeds['events_english']['processed']['state']=='unknown'
    assert output['waiting_first']==800 and output['waiting_last']==830


def test_counter_reset_is_not_negative_throughput_and_failed_final_fetch_is_not_zero():
    output=sampler().summarize([sample(0,1000,200,800),sample(10,1100,50,1050),
        dict(elapsed_seconds=20,error='ConnectionError')])
    assert output['elapsed_seconds']==10
    assert output['processed']['delta'] is None
    assert output['processed']['state']=='counter_reset_or_scope_change'
    assert output['failed_samples']==1


def test_one_successful_sample_cannot_claim_a_rate():
    assert sampler().summarize([sample(0,1000,200,800)])['state']=='insufficient'
