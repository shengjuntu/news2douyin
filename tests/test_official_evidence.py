"""Coverage is about upstream document ids, independently of readable text or LLM use."""
from datetime import date, datetime, timezone
import json

import pytest
from sqlalchemy import event as sql_event
from sqlmodel import Session

from news2douyin.intake.models import IntakeBatch
from news2douyin.intake.official_models import OfficialEvent, OfficialMention
from news2douyin.intake.official_parser import document_domain
from news2douyin.selection.official_evidence import coverage_by_event, official_evidence
from news2douyin.storage.articles import identity_key
from news2douyin.storage.db import init_db, make_engine
from news2douyin.storage.models import ArticleIdentity, ArticleVersion
from test_postgres_integration import postgres_url

START = '2024-04-09T16:00:00Z'
END = '2024-04-10T16:00:00Z'
BEFORE = '2024-04-09T10:00:00Z'
DURING = '2024-04-09T22:00:00Z'
DAY = date(2024, 4, 10)
FEED = 'mentions_english'
FIRST = '20240409100000'
SECOND = '20240409220000'
FUTURE = '20240410160000'


def batch(stamp, received):
    return IntakeBatch(key=FEED + '_' + stamp, feed=FEED, stamp=stamp, completed_at=received)


def mention(key, url, begin, end, first, last, received):
    return OfficialMention(key=key, event_id='1001', doc_identifier=url, domain=document_domain(url),
        first_seen=begin, last_seen=end, updated_at=received, first_feed=FEED, first_stamp=first,
        first_row_index=1, last_feed=FEED, last_stamp=last, last_row_index=1,
        confidence=80, sentence_id=2, actor1_offset=45, action_offset=65)


def official():
    return OfficialEvent(event_id='1001', event_date='2024-04-08', first_seen=BEFORE,
        added_at=BEFORE, last_seen=END, ingested_at=BEFORE, feed='events_english', stamp=FIRST, row_index=3,
        actor1_name='A', actor2_name='B', actor1_country='ITA', actor1_country_iso2='it',
        event_code='040', event_base_code='040', event_root_code='04', action_place='Rome',
        initial_articles=999, source_url='https://unmentioned.example/base')


def verify(session):
    updates = [
        mention('old', 'https://old.example/first', BEFORE, END, FIRST, FUTURE, END),
        mention('repeat-domain', 'https://old.example/another', DURING, DURING, SECOND, SECOND, DURING),
        mention('new', 'https://new.example/news', DURING, DURING, SECOND, SECOND, DURING),
        mention('also-new', 'https://new.example/analysis', DURING, DURING, SECOND, SECOND, DURING),
        mention('non-web', 'gcast:1234', DURING, DURING, SECOND, SECOND, DURING),
        mention('too-late', 'https://unseen.example/news', DURING, DURING, FUTURE, FUTURE, END),
        mention('future', 'https://future.example/update', END, END, FUTURE, FUTURE, END),
    ]
    session.add_all([official(), batch(FIRST, BEFORE), batch(SECOND, DURING), batch(FUTURE, END), *updates])
    session.commit()
    expected = dict(documents=5, domains=2, new_documents=4, new_domains=1, history_complete=True)
    assert coverage_by_event(session, ['1001','n/a'], START, END) == {
        '1001':expected, 'n/a':dict(documents=0, domains=0, new_documents=0, new_domains=0, history_complete=True)}
    docs = {row['document']:row for row in official_evidence(session, '1001', DAY)['documents']}
    assert len(docs) == 5 and docs['gcast:1234']['url'] == ''
    old = docs['https://old.example/first']
    assert old['first_ref']['stamp'] == FIRST and old['last_ref'] is None and old['last_seen'] == ''
    assert old['confidence'] is None and old['action_offset'] is None
    payload = official_evidence(session, '1001', DAY, limit=2)
    assert payload['coverage'] == expected and payload['documents_limited'] and len(payload['documents']) == 2
    assert payload['action_place']['name'] == 'Rome' and payload['actor1']['iso2'] == 'it'
    assert payload['cameo_root'] == '04' and payload['source_ref']['row_index'] == 3
    assert official_evidence(session,'unknown',DAY) is None
    with pytest.raises(ValueError):
        official_evidence(session, '1001', DAY, now=datetime(2024,1,1,tzinfo=timezone.utc))


def test_sqlite_coverage(engine):
    with Session(engine) as session:
        verify(session)


def test_postgres_coverage(postgres_url):
    pg = make_engine(postgres_url)
    try:
        init_db(pg)
        with Session(pg) as session:
            verify(session)
            verify_gkg(session)
    finally:
        pg.dispose()


def verify_gkg(session):
    exact='https://stable.example/story'
    near='https://stable.example/story?utm_source=wire'
    session.add_all([mention('exact',exact,DURING,DURING,SECOND,SECOND,DURING),
                     mention('near',near,DURING,DURING,SECOND,SECOND,DURING)])
    session.add(ArticleIdentity(identity_key=identity_key({'url':exact}),article_key='a'))
    for index, seen in enumerate((DURING,END), start=1):
        raw=dict(uri=exact,record_id='20240409220000-'+str(index),intake=dict(feed='gkg_english',
                 stamp=SECOND,row_index=index),metadata=dict(persons=['A non-participant']))
        session.add(ArticleVersion(article_key='a',revision=index,content_hash=str(index),
            origin='intake_metadata',payload_json=json.dumps(dict(raw=raw)),observed_at=seen))
    session.commit()
    docs=official_evidence(session,'1001',DAY)['documents']
    matched={r['document']:r['gkg'] for r in docs if r['gkg']}
    assert list(matched)==[exact] and matched[exact]['row_index']==1 and matched[exact]['revision']==1


def test_exact_gkg_does_not_assign_another_url_or_future_revision(engine):
    with Session(engine) as session:
        session.add(official())
        verify_gkg(session)


def test_uncertain_first_pointer_and_batch_query_cost(engine):
    with Session(engine) as s:
        # Latest pre-cutoff record is usable. Earlier source observation was backfilled
        # only after the cutoff; no established first time nor growth bonus.
        s.add_all([batch(FUTURE, END), mention('m','https://a.example/x',BEFORE,DURING,FUTURE,SECOND,DURING)])
        s.commit()
        statements=[]
        def audit(conn,cursor,statement,p,c,many): statements.append(statement.lower())
        sql_event.listen(engine,'before_cursor_execute',audit)
        try:
            result=coverage_by_event(s, ['1001', *map(str,range(99))],START,END)
        finally:
            sql_event.remove(engine,'before_cursor_execute',audit)
        assert result['1001'] == dict(documents=1, domains=1, new_documents=0,new_domains=0,history_complete=False)
        assert len(statements)==1 and 'doc_identifier' not in statements[0]

def test_late_first_archive_is_not_fresh_action_or_retrospectively_visible(engine):
    receipt_day = '2024-04-14T22:00:00Z'
    with Session(engine) as s:
        s.add_all([official(), batch(FIRST, receipt_day), mention('late', 'https://a.example/old',
                   BEFORE, BEFORE, FIRST, FIRST, receipt_day)])
        s.commit()
        old = coverage_by_event(s, ['1001'], START, END)['1001']
        assert (old['documents'], old['new_domains']) == (0, 0)
        late = official_evidence(s, '1001', date(2024,4,15))
        assert late['coverage_time_basis'] == 'gdelt_first_mention_before_local_cutoff'
        assert (late['coverage']['documents'], late['coverage']['new_documents'], late['coverage']['new_domains']) == (1,0,0)
