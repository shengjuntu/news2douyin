"""Six archive feeds through SQL discovery and explicit, metadata-safe selection."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func
from sqlmodel import Session, select

from news2douyin.intake import service as intake, projection
from news2douyin.intake.feeds import FEEDS
from news2douyin.intake.lake import Lake
from news2douyin.intake.models import IntakeBatch,FeedState
from news2douyin.intake.official_models import OfficialEvent,OfficialMention
from news2douyin.selection import service as selection
from news2douyin.storage.db import init_db,make_engine
from news2douyin.storage.models import Article,Event
from news2douyin.topics import service as topics
from news2douyin.topics.models import TopicWorkspace
from news2douyin.research.models import ResearchSource
from test_postgres_integration import postgres_url
from test_official_events import event_line,mention_line,zipped,STAMP
from test_intake_archive import line as gkg_line


@pytest.fixture(params=['sqlite','postgres'])
def pipeline_engine(request,tmp_path):
    url=request.getfixturevalue('postgres_url') if request.param=='postgres' else 'sqlite:///'+str(tmp_path/'pipeline.db')
    engine=make_engine(url);init_db(engine)
    yield engine
    engine.dispose()


def _stage(engine,root,feed,rows):
    path=root/(feed+'.zip');path.write_bytes(zipped(rows,feed))
    archived=Lake(root).ingest_archive(feed,STAMP,path)
    with Session(engine) as session:
        session.add(IntakeBatch(key=feed+'_'+STAMP,feed=feed,stamp=STAMP,state='archived',**archived))
        session.commit()
    return path


def _drain(engine,root):
    for _ in range(100):
        if not projection.project_step(engine,root,limit=2,time_budget_ms=10000):return
    raise AssertionError('Tiny fixture did not drain')


@pytest.mark.parametrize("legacy_events",[False,True],ids=["default-discovery","legacy-local-events"])
def test_six_archives_keep_filtered_rows_and_only_selection_starts_research(pipeline_engine,tmp_path,monkeypatch,legacy_events):
    if legacy_events:monkeypatch.setenv("NEWS2DOUYIN_GKG_AUTO_EVENTS","1")
    else:monkeypatch.delenv("NEWS2DOUYIN_GKG_AUTO_EVENTS",raising=False)
    engine=pipeline_engine
    # Start with the four new official feeds, keeping repeated observations in
    # the lake while the working index collapses event/document identities.
    paths=[]
    for feed in ('events_english','events_translation','mentions_english','mentions_translation'):
        raw=event_line() if feed.startswith('events') else mention_line(
            url='https://wire.example/article' if feed=='mentions_english' else 'https://other.example/report')
        paths.append(_stage(engine,tmp_path,feed,[raw,'bad\trow']))
    _drain(engine,tmp_path)
    with Session(engine) as session:
        assert session.exec(select(func.count()).select_from(OfficialEvent)).one()==1
        assert session.exec(select(func.count()).select_from(OfficialMention)).one()==2
        assert session.exec(select(func.count()).select_from(Article)).one()==0
        assert session.exec(select(func.count()).select_from(Event)).one()==0
        selected=selection.list_candidates(session,selection.checked(period='all',country='cn',cameo='07',min_sources=2))
        assert [row['event_id'] for row in selected['items']]==['001234567']
        assert selected['items'][0]['observed_sources']==2
        assert selection.list_candidates(session,selection.checked(period='all',cameo='19'))['items']==[]
    # Remaining two GKG feeds still use the existing article projection path.
    for number,feed in enumerate(('gkg_english','gkg_translation'),1):
        paths.append(_stage(engine,tmp_path,feed,[gkg_line(number,title='Unrelated historical source',
            url=f'https://gkg.example/{number}',translated=feed.endswith('translation')),'bad\trow']))
    _drain(engine,tmp_path)
    lake=Lake(tmp_path)
    for feed in FEEDS:
        rows=lake.rows(feed,STAMP)
        assert len(rows)==2 and not rows[1]['valid'] and rows[1]['raw_line']=='bad\trow'
    assert all(path.is_file() for path in paths)
    with Session(engine) as session:
        assert session.exec(select(func.count()).select_from(projection.IntakeProjectionReceipt)).one()==12
        for batch in session.exec(select(IntakeBatch)):
            assert batch.rows==2 and batch.projected_offset==2
        before_articles=session.exec(select(func.count()).select_from(Article)).one()
        before_events=session.exec(select(func.count()).select_from(Event)).one()
        assert before_articles==2
        assert before_events==(2 if legacy_events else 0)
    task=selection.select_candidate(engine,'001234567','qa-six-feeds-select')
    class Runtime:
        calls=[]
        def enqueue(self,*args,**kwargs):self.calls.append((args,kwargs))
    runtime=Runtime()
    with Session(engine) as session:revision=session.get(TopicWorkspace,task['key']).revision
    handoff=topics.handoff(engine,runtime,task['key'],dict(request_id='qa-six-feeds-research',revision=revision,action='deep'))
    assert len(runtime.calls)==1
    with Session(engine) as session:
        source=session.exec(select(ResearchSource).where(ResearchSource.case_id==handoff['case_id'])).one()
        assert source.kind=='event_metadata'
        assert session.exec(select(func.count()).select_from(Article)).one()==before_articles
        assert session.exec(select(func.count()).select_from(Event)).one()==before_events
    assert selection.select_candidate(engine,'001234567','qa-six-feeds-select')==task


def test_failed_feed_does_not_advance_its_cursor_or_block_other_kind(pipeline_engine,tmp_path,monkeypatch):
    engine=pipeline_engine
    intake.initialize(engine)
    blobs={feed:zipped([event_line() if feed.startswith('events') else mention_line()],feed)
           for feed in ('events_english','mentions_english')}
    import hashlib
    monkeypatch.setattr(intake,'discover',lambda feed:dict(stamp=STAMP,bytes=len(blobs[feed]),
        md5=hashlib.md5(blobs[feed]).hexdigest(),url=intake.url_for(feed,STAMP)))
    def download(feed,stamp,destination,*_):
        if feed=='events_english':raise intake.IntakeHTTPError(429,120)
        destination=Path(destination);destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(blobs[feed])
    monkeypatch.setattr(intake,'download',download)
    assert intake.step(engine,tmp_path,feed='events_english',now=1000)
    assert intake.step(engine,tmp_path,feed='mentions_english',now=1001)
    with Session(engine) as session:
        failed=session.get(FeedState,'events_english')
        assert failed.last_complete=='' and failed.next_batch==STAMP and failed.retry_after==1120
        assert session.get(FeedState,'mentions_english').last_complete==STAMP
    assert len(Lake(tmp_path).rows('mentions_english',STAMP))==1
