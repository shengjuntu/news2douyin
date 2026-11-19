"""Historical repair uses the live decoder and one real SQL/DuckDB pair per test."""
from datetime import date, datetime, timezone
from hashlib import sha256
from io import BytesIO
from zipfile import ZipFile

import pytest
from sqlmodel import Session, select

from news2douyin.digest import backfill, service as digest
from news2douyin.digest.archive import iter_gkg
from news2douyin.digest.models import DailyDigestIssue
from news2douyin.intake import service as intake
from news2douyin.intake.feeds import FEEDS
from news2douyin.intake.lake import Lake
from news2douyin.intake.models import DailyRepairPart, FeedState, IntakeBatch

DAY = date(2025, 3, 16)
FEED = 'gkg_english'


def enable(engine, monkeypatch, active=(FEED,)):
    monkeypatch.setenv('NEWS2DOUYIN_GDELT_STREAM_ENABLED', '1')
    intake.initialize(engine)
    for feed in FEEDS:
        intake.configure(engine, feed, feed in active)


def raw_zip(stamp, title, slug):
    fields = [''] * 27
    fields[0:5] = [stamp+'-1', stamp, '1', 'media.example', 'https://media.example/'+slug]
    fields[26] = '<PAGE_TITLE>'+title+'</PAGE_TITLE>'
    output = BytesIO()
    with ZipFile(output, 'w') as archive:
        archive.writestr(stamp+'.gkg.csv', '\t'.join(fields)+'\n')
    return output.getvalue()


def seed_empty_slots(lake, stamps):
    # Closed slots with empty source files still have valid manifest receipts.
    empty = [('gkg_english', stamp, '0'*64, '1'*32, 0, '', 0, 0, 0, '2025-03-16T12:00:00Z') for stamp in stamps]
    with lake.connection(create=True) as db:
        db.executemany('INSERT INTO intake_archives VALUES (?,?,?,?,?,?,?,?,?,?)', empty)


def add_batch(lake, tmp_path, stamp, title, slug):
    path = tmp_path / (stamp + '.zip')
    path.write_bytes(raw_zip(stamp,title,slug))
    return lake.ingest_archive(FEED,stamp,path)


def job(engine):
    return backfill.list_jobs(engine)[0]


def task_ticks(engine, root, *times):
    for t in times:
        assert backfill.step(engine, root, now=t)


def test_coverage_distinguishes_due_failed_disabled_and_joined_jobs(engine, monkeypatch):
    now = datetime(2025, 3, 15, 16, 15, tzinfo=timezone.utc)
    enable(engine, monkeypatch, (FEED,'events_english'))
    with Session(engine) as s:
        s.add(IntakeBatch(key='gkg_english_20250315160000',feed=FEED,stamp='20250315160000',state='archived'))
        s.add(IntakeBatch(key='events_english_20250315161500',feed='events_english',stamp='20250315161500',state='error'))
        s.commit()
    today = backfill.coverage(engine,DAY,DAY,now=now)['days'][0]
    assert (today['expected'],today['stored'],today['missing'],today['failed'],today['not_due']) == (4,1,3,1,188)
    assert today['feeds'][0]['missing_examples'] == ['20250315161500']
    assert backfill._stamps(DAY)[-1] == '20250316154500'
    queued = backfill.enqueue(engine, DAY)
    assert queued['total'] == 192 and backfill.enqueue(engine,DAY,retry=True)['id'] == queued['id']
    with pytest.raises(ValueError):
        backfill.enqueue(engine, digest.default_day())
    monkeypatch.setenv('NEWS2DOUYIN_GDELT_STREAM_ENABLED','0')
    with pytest.raises(RuntimeError):
        backfill.enqueue(engine, DAY)


def test_new_and_preexisting_late_archives_historical_publication_and_daily_isolation(engine,tmp_path,monkeypatch):
    enable(engine,monkeypatch)
    monkeypatch.setenv('NEWS2DOUYIN_GKG_AUTO_EVENTS', '1')
    stamps = backfill._stamps(DAY)
    lake = Lake(tmp_path)
    seed_empty_slots(lake, stamps[2:])
    add_batch(lake, tmp_path, stamps[0], 'Harbor authority reopens the northern shipping channel', 'harbor')
    add_batch(lake, tmp_path, '20250316160000', 'Hospital trustees announce the appointment of a director', 'hospital')
    received_day = digest.default_day()
    requested = []
    def download(feed,stamp,path,*args,**kwargs):
        requested.append((feed,stamp))
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(raw_zip(stamp, 'Telecom regulator licenses a new satellite service', 'satellite'))
    monkeypatch.setattr(intake,'download',download)
    queued = backfill.enqueue(engine, DAY)
    assert job(engine)['status']=='queued' and not requested
    task_ticks(engine,tmp_path,100,101)
    assert requested == [(FEED,stamps[1])]
    assert len(lake.rows(FEED,stamps[1])) == 1
    assert lake.rows(FEED,stamps[1])[0]['repair_day']==DAY.isoformat()
    task_ticks(engine,tmp_path,102,103)
    with Session(engine) as s:
        token, scope = backfill.restored_gkg(s,DAY)
        assert token == queued['id'] and scope == {(FEED,stamps[0]),(FEED,stamps[1])}
        assert s.get(FeedState,FEED).last_complete == ''
        assert s.get(IntakeBatch,FEED+'_'+stamps[1]).sha256
    assert job(engine)['status']=='awaiting_digest'
    result = digest.build_issue(engine,DAY,storage_root=tmp_path)
    assert result['status']=='ready' and result['stats']['sources']['backfill_id']==token
    urls={item['source_url'] for item in result['items']}
    assert urls == {'https://media.example/harbor','https://media.example/satellite'}
    assert result['stats']['sources']['gkg_restored_rows']==2
    task_ticks(engine,tmp_path,108)
    assert job(engine)['status']=='completed'
    fresh = list(iter_gkg(tmp_path,received_day))
    assert {c.source_url for c in fresh} == {'https://media.example/harbor','https://media.example/hospital'}
    observed = result['stats']['sources']['gkg_restored_batches'][f'{FEED}:{stamps[1]}']
    assert observed > '2025-03-16T16:00:00Z'
    # Explicit repair projections remain available for research but cannot leak a
    # second copy back to today's local-event scan when auto events are enabled.
    from news2douyin.intake.projection import project_step
    from news2douyin.storage.models import ArticleVersion
    from news2douyin.desk.models import RadarEntry
    from news2douyin.storage.models import ArticleEventLink
    from news2douyin.digest.sources import iter_candidates
    for _ in range(3):
        project_step(engine,tmp_path)
    with Session(engine) as s:
        version = s.exec(select(ArticleVersion).where(ArticleVersion.origin=='intake_history')).one()
        link = s.exec(select(ArticleEventLink.event_key).where(ArticleEventLink.article_key==version.article_key)).one()
        s.add(RadarEntry(key=link,title='satellite',seen=observed,priority=1,band='low',
                         reason='',evidence='',headings='',material='',sources='',geography=''))
        s.commit()
        assert all(c.source_url != 'https://media.example/satellite' for c in iter_candidates(s,received_day))


def test_404_partial_preserves_old_snapshot_retries_bad_cache_and_publishes_only_when_requested(engine,tmp_path,monkeypatch):
    enable(engine,monkeypatch)
    stamps = backfill._stamps(DAY)
    lake=Lake(tmp_path)
    seed_empty_slots(lake,stamps[2:])
    add_batch(lake,tmp_path,stamps[0],'City council approves a new river crossing','bridge')
    good = raw_zip(stamps[1],'Transit agency adds a new commuter route','transit')
    path = lake.root/'raw'/FEED/(stamps[1]+FEEDS[FEED]['suffix'])
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(b'corrupt cached file')
    with Session(engine) as s:
        s.add(IntakeBatch(key=FEED+'_'+stamps[1],feed=FEED,stamp=stamps[1],state='archived',
                          sha256=sha256(good).hexdigest(),completed_at='2025-03-16T11:00:00Z'))
        s.add(DailyDigestIssue(day=DAY.isoformat(),status='ready',items_json='[{"key":"old"}]'))
        s.commit()
    monkeypatch.setattr(intake,'download',lambda *args,**kwargs: pytest.fail('must first verify cached archive'))
    backfill.enqueue(engine,DAY)
    task_ticks(engine,tmp_path,10,11,12)
    assert job(engine)['status']=='partial' and not path.exists() and len(list(path.parent.glob('*.rejected-*')))==1
    with Session(engine) as s:
        assert digest.read_issue(s,DAY)['items']==[{'key':'old'}]
        assert backfill.restored_gkg(s,DAY) == ('',set())
    def unavailable(*args,**kwargs):
        raise intake.IntakeHTTPError(404)
    monkeypatch.setattr(intake,'download',unavailable)
    backfill.enqueue(engine,DAY,retry=True)
    task_ticks(engine,tmp_path,20,21,22)
    partial = job(engine)
    assert (partial['status'],partial['done'],partial['failed']) == ('partial',95,1)
    assert '404' in partial['examples'][0]['error']
    assert backfill.coverage(engine,DAY,DAY)['days'][0]['failed']==1
    backfill.publish_available(engine,DAY)
    task_ticks(engine,tmp_path,23)
    replacement = digest.build_issue(engine,DAY,storage_root=tmp_path)
    task_ticks(engine,tmp_path,30)
    assert replacement['stats']['sources']['gkg_restored_rows']==1
    assert job(engine)['status']=='completed_partial'
    def repaired(feed,stamp,path,*args,**kwargs):
        path.write_bytes(good)
    monkeypatch.setattr(intake,'download',repaired)
    retried = backfill.enqueue(engine,DAY,retry=True)
    task_ticks(engine,tmp_path,50,51,52,53)
    repaired_issue = digest.build_issue(engine,DAY,storage_root=tmp_path)
    task_ticks(engine,tmp_path,59)
    assert repaired_issue['stats']['sources']['backfill_id'] == retried['id']
    assert repaired_issue['stats']['sources']['gkg_restored_rows']==2
    assert job(engine)['status']=='completed'


def test_retry_after_survives_receiver_restart_and_progress_is_idempotent(engine,tmp_path,monkeypatch):
    from news2douyin.intake.runtime import IntakeWorker
    enable(engine,monkeypatch)
    stamps = backfill._stamps(DAY)
    seed_empty_slots(Lake(tmp_path),stamps[1:])
    calls = []
    def download(feed, stamp, path, *args, **kwargs):
        calls.append(stamp)
        if len(calls)==1:
            raise intake.IntakeHTTPError(429,120)
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(raw_zip(stamp,'France establishes a board for pension oversight','board'))
    monkeypatch.setattr(intake,'download',download)
    # The live receiver and repair queue share the process owner's schedule.
    live = []
    def live_tick(*args,**kwargs):
        live.append(kwargs['now'])
        return False
    monkeypatch.setattr(intake,'step',live_tick)
    queued=backfill.enqueue(engine,DAY)
    worker=IntakeWorker(engine,tmp_path)
    for t in (100,101,102,103):
        worker.step(now=t)
    assert live==[100,102] and calls==[stamps[0]]
    waiting=job(engine)
    assert waiting['status']=='running' and waiting['retrying']==1 and waiting['next_at']==223
    assert backfill.enqueue(engine,DAY)['id']==queued['id']
    resumed=IntakeWorker(engine,tmp_path)
    for t in (121,122,222,223):
        resumed.step(now=t)
    assert calls==[stamps[0],stamps[0]] and job(engine)['done']==96
    with Session(engine) as s:
        row=s.get(DailyRepairPart,FEED+'_'+stamps[0])
        assert row.state=='done' and row.attempts==1
        assert s.get(FeedState,FEED).next_batch==''
