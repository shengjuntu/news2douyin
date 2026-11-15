"""Events/Mentions source contracts, raw completeness and compact SQL indexing."""
import io
import json
from pathlib import Path
from zipfile import ZipFile

import pytest
from sqlalchemy import func
from sqlmodel import SQLModel, Session, select

from news2douyin.intake import service, projection
from news2douyin.intake.feeds import FEEDS, kind_for, permitted_members
from news2douyin.intake.lake import Lake
from news2douyin.intake.official_models import OfficialEvent, OfficialMention
from news2douyin.intake.official_parser import decoded_row
from news2douyin.intake.official_projection import project_row
from news2douyin.storage.models import Article, Event, NewsWriteLock
from news2douyin.storage.db import make_engine
from test_postgres_integration import postgres_url

STAMP='20261114041500'
NEXT='20261114043000'
OBSERVED='2026-11-14T04:20:00Z'


@pytest.fixture(params=['sqlite','postgres'])
def engine(request,tmp_path):
    # This unit scope does not run application startup/backfill or network work.
    url=request.getfixturevalue('postgres_url') if request.param=='postgres' else 'sqlite:///'+str(tmp_path/'official-index.db')
    result=make_engine(url)
    SQLModel.metadata.create_all(result)
    with Session(result) as session:
        session.add(NewsWriteLock(name='collection'));session.commit()
    try:yield result
    finally:result.dispose()


def event_line(event_id='001234567',code='072',date='20261113',added=STAMP,url='https://wire.example/article'):
    c=['']*61
    c[0:5]=[event_id,date,date[:6],date[:4],'2026.9']
    c[5:8]=['CHNGOV','CHINA','CHN'];c[15:18]=['USAGOV','UNITED STATES','USA']
    c[25:35]=['1',code,code[:3],code[:2],'2','7.0','12','3','8','-2.5']
    c[51:61]=['1','Beijing, China','CH','CH22','','39.9','116.4','-1898541',added,url]
    return '\t'.join(c)


def mention_line(event_id='001234567',when=STAMP,url='https://wire.example/article',confidence='90',sentence='2'):
    return '\t'.join([event_id,STAMP,when,'1','wire.example',url,sentence,'1','20','35','1',confidence,'1000','-2.5','',''])


def parsed(text,feed='events_english',stamp=STAMP,index=1):
    return decoded_row(text.encode(),feed,stamp,index,OBSERVED)


def zipped(lines,feed='events_english',stamp=STAMP):
    out=io.BytesIO()
    suffix=FEEDS[feed]['suffix'][:-4]
    with ZipFile(out,'w') as archive:archive.writestr(stamp+suffix,'\n'.join(lines)+'\n')
    return out.getvalue()


def test_verified_six_feed_names_match_official_manifest_and_translation_members():
    assert len(FEEDS)==6
    assert service.url_for('events_english',STAMP).endswith(STAMP+'.export.CSV.zip')
    assert service.url_for('mentions_translation',STAMP).endswith(STAMP+'.translation.mentions.CSV.zip')
    assert kind_for('events_translation')=='events'
    assert STAMP+'.export.CSV' in permitted_members('events_translation',STAMP)
    assert STAMP+'.translation.export.CSV' in permitted_members('events_translation',STAMP)
    with pytest.raises(ValueError):kind_for('../events')


def test_event_parser_preserves_source_identity_codes_countries_dates_and_float():
    row=parsed(event_line())
    assert row['valid'],row
    meta=row['metadata']
    assert meta['event_id']=='001234567'
    assert meta['event_code']=='072' and meta['event_base_code']=='072' and meta['event_root_code']=='07'
    assert meta['actor1_country']=='CHN' and meta['actor1_country_iso2']=='cn'
    assert meta['action_country']=='CH' and meta['action_country_iso2']=='cn'
    assert meta['actor2_country_iso2']=='us'
    assert meta['event_date']=='2026-11-13' and meta['added_at']=='2026-11-14T04:15:00Z'
    assert meta['latitude']==39.9 and meta['longitude']==116.4 and meta['goldstein']==7
    assert meta['initial_mentions']==12
    # Legitimate root-level event/base codes need not have three digits.
    assert parsed(event_line(code='04'))['valid']


def test_mention_parser_locates_document_and_does_not_confuse_field_positions():
    row=parsed(mention_line(),feed='mentions_english')
    assert row['valid'],row
    meta=row['metadata']
    assert row['url']=='https://wire.example/article' and meta['source_name']=='wire.example'
    assert meta['doc_identifier']==row['url'] and meta['domain']=='wire.example'
    assert meta['event_time']==meta['mention_time']=='2026-11-14T04:15:00Z'
    assert meta['sentence_id']==2 and meta['actor1_offset']==1 and meta['action_offset']==35
    assert meta['confidence']==90 and meta['doc_length']==1000 and meta['in_raw_text'] is True
    citation=parsed(mention_line(url='JSTOR:123'),feed='mentions_translation')
    assert citation['valid'] and citation['metadata']['domain']==''


@pytest.mark.parametrize('feed,text',[
    ('events_english','bad\trow'),('mentions_translation','bad\trow'),
    ('events_english',event_line(date='20261399')),
    ('mentions_english',mention_line(confidence='101')),
])
def test_malformed_rows_are_marked_but_original_text_survives(feed,text):
    row=parsed(text,feed=feed)
    assert not row['valid'] and row['error'] and row['raw_line']==text


def test_invalid_utf8_is_not_silently_validated():
    row=decoded_row((event_line()+'\xff').encode('latin1'),'events_english',STAMP,1,OBSERVED)
    assert not row['valid'] and row['error']=='invalid_utf8' and row['metadata']['raw_bytes_base64']


def test_nonfinite_goldstein_and_invalid_geometry_are_not_accepted():
    fields=event_line().split('\t');fields[30]='NaN'
    assert not parsed('\t'.join(fields))['valid']
    fields=event_line().split('\t');fields[56]='91'
    assert not parsed('\t'.join(fields))['valid']


def test_compact_index_keeps_raw_event_separate_from_local_research_and_article(engine):
    with Session(engine) as session:
        result=project_row(session,parsed(event_line()))
        session.commit()
        row=session.get(OfficialEvent,'001234567')
        assert result['status']=='projected' and row.event_code=='072'
        assert row.initial_mentions==12 and row.first_seen==row.last_seen=='2026-11-14T04:15:00Z'
        assert (row.feed,row.stamp,row.row_index)==('events_english',STAMP,1)
        assert session.exec(select(func.count()).select_from(Article)).one()==0
        assert session.exec(select(func.count()).select_from(Event)).one()==0


def test_repeated_mentions_count_document_once_and_out_of_order_observations_keep_refs(engine):
    with Session(engine) as session:
        for index,when in [(2,NEXT),(1,STAMP),(3,NEXT)]:
            project_row(session,parsed(mention_line(when=when),feed='mentions_english',stamp=when,index=index))
        session.commit()
        assert session.exec(select(func.count()).select_from(OfficialMention)).one()==1
        mention=session.exec(select(OfficialMention)).one()
        assert mention.first_seen=='2026-11-14T04:15:00Z'
        assert mention.last_seen=='2026-11-14T04:30:00Z'
        assert (mention.first_stamp,mention.first_row_index)==(STAMP,1)
        assert (mention.last_stamp,mention.last_row_index)==(NEXT,3)
        assert session.get(OfficialEvent,'001234567') is None  # no placeholder local/source event
        project_row(session,parsed(event_line()))
        session.commit()
        assert session.get(OfficialEvent,'001234567').last_seen==mention.last_seen
        project_row(session,parsed(mention_line(url='https://other.example/report',when=NEXT),feed='mentions_translation',stamp=NEXT,index=5))
        session.commit()
        assert session.exec(select(func.count()).select_from(OfficialMention)).one()==2
        assert session.exec(select(func.count(func.distinct(OfficialMention.domain)))).one()==2


def test_older_event_batch_cannot_roll_latest_event_metadata_backwards(engine):
    with Session(engine) as session:
        project_row(session,parsed(event_line(code='073'),stamp=NEXT))
        project_row(session,parsed(event_line(code='072'),stamp=STAMP))
        session.commit()
        row=session.get(OfficialEvent,'001234567')
        assert row.event_code=='073' and row.stamp==NEXT


def test_skipped_invalid_record_creates_no_source_index_rows(engine):
    with Session(engine) as session:
        result=project_row(session,parsed('bad'))
        session.commit()
        assert result['status']=='skipped'
        assert session.exec(select(func.count()).select_from(OfficialEvent)).one()==0


def test_manifest_discovery_selects_each_matching_official_feed_without_gkg_assumption(monkeypatch):
    body='\n'.join('100 '+'a'*32+' http://data.gdeltproject.org/gdeltv2/'+STAMP+item['suffix'] for item in FEEDS.values()).encode()
    class Response:
        status_code=200;headers={}
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def iter_content(self,_):yield body
    monkeypatch.setattr(service.requests,'get',lambda *_a,**_k:Response())
    for feed in FEEDS:
        record=service.discover(feed)
        assert record['url']==service.url_for(feed,STAMP)


def test_generic_runtime_flag_preserves_old_off_setting(monkeypatch):
    monkeypatch.delenv('NEWS2DOUYIN_GDELT_STREAM_ENABLED',raising=False)
    monkeypatch.setenv('NEWS2DOUYIN_GKG_STREAM_ENABLED','0')
    assert not service.runtime_enabled()
    monkeypatch.setenv('NEWS2DOUYIN_GDELT_STREAM_ENABLED','1')
    assert service.runtime_enabled()
    monkeypatch.setenv('NEWS2DOUYIN_GDELT_STREAM_ENABLED','')
    assert not service.runtime_enabled()


def _stage(engine,tmp_path,feed,lines,stamp=STAMP):
    from news2douyin.intake.models import IntakeBatch
    path=tmp_path/(feed+'-'+stamp+'.zip');path.write_bytes(zipped(lines,feed,stamp))
    result=Lake(tmp_path).ingest_archive(feed,stamp,path)
    with Session(engine) as session:
        session.add(IntakeBatch(key=feed+'_'+stamp,feed=feed,stamp=stamp,state='archived',**result))
        session.commit()
    return path


@pytest.mark.parametrize('feed',['events_english','events_translation','mentions_english','mentions_translation'])
def test_official_full_archive_to_sql_receipt_is_replayable_without_local_article(engine,tmp_path,feed):
    from news2douyin.intake.projection import IntakeProjectionReceipt,project_step
    from news2douyin.intake.models import IntakeBatch
    _stage(engine,tmp_path,feed,[event_line() if kind_for(feed)=='events' else mention_line(),'bad\trow'])
    assert project_step(engine,tmp_path,limit=2,time_budget_ms=10000)
    with Session(engine) as session:
        receipts=session.exec(select(IntakeProjectionReceipt).order_by(IntakeProjectionReceipt.row_index)).all()
        assert [row.status for row in receipts]==['projected','skipped']
        assert session.exec(select(func.count()).select_from(Article)).one()==0
        assert session.exec(select(func.count()).select_from(Event)).one()==0
        batch=session.get(IntakeBatch,feed+'_'+STAMP)
        assert batch.projected_offset==2
        batch.projected_offset=0;session.add(batch);session.commit()
    assert project_step(engine,tmp_path,limit=2,time_budget_ms=10000)
    with Session(engine) as session:
        assert session.exec(select(func.count()).select_from(IntakeProjectionReceipt)).one()==2
        model=OfficialEvent if kind_for(feed)=='events' else OfficialMention
        assert session.exec(select(func.count()).select_from(model)).one()==1
        assert session.get(IntakeBatch,feed+'_'+STAMP).projected_offset==2
    assert len(Lake(tmp_path).rows(feed,STAMP))==2


def test_old_gkg_backlog_cannot_starve_new_events_and_mentions(engine,tmp_path):
    from news2douyin.intake.projection import project_step
    from news2douyin.intake.models import IntakeBatch
    from test_intake_archive import line as gkg_line
    old='20261104120000'
    _stage(engine,tmp_path,'gkg_english',[gkg_line(i) for i in range(10)],stamp=old)
    _stage(engine,tmp_path,'events_english',[event_line()])
    _stage(engine,tmp_path,'mentions_english',[mention_line()])
    for _ in range(3):assert project_step(engine,tmp_path,limit=1,time_budget_ms=10000)
    with Session(engine) as session:
        assert session.get(OfficialEvent,'001234567') is not None
        assert session.exec(select(func.count()).select_from(OfficialMention)).one()==1
        assert session.get(IntakeBatch,'gkg_english_'+old).projected_offset==1
