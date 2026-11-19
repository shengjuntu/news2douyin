"""Event promotion preserves source identity and the editor's versioned decision."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from sqlmodel import Session, select

from news2douyin.candidates.promotion import promote
from news2douyin.candidates.service import candidate_token, resolve_candidate, record_reports
from news2douyin.events import workbench
from news2douyin.extraction.service import schedule_model
from news2douyin.storage.articles import latest_version
from news2douyin.storage.db import init_db, make_engine
from news2douyin.storage.models import Article, ArticleEventLink, Event, EventActivity
from news2douyin.storage.utils import loads
from news2douyin.tasks.control import TaskConflict
from test_postgres_integration import postgres_url


@pytest.fixture(params=['sqlite', 'postgres'])
def promotion_db(request):
    if request.param == 'sqlite':
        yield request.getfixturevalue('engine')
        return
    engine = make_engine(request.getfixturevalue('postgres_url'))
    init_db(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def record(session, *, kind, body, batch, url='https://paper.example/report'):
    now = datetime.now(timezone.utc).isoformat()
    (key,) = record_reports(session, [dict(title='افتتاح محطة جديدة', content=body,
                                           content_kind=kind, language='ar', url=url, fetched_at=now)],
                            provider='worldnewsapi', batch=batch)
    session.commit()
    return key, candidate_token(resolve_candidate(session, None, key))


def test_promotion_guard_and_retry(promotion_db):
    with Session(promotion_db) as session:
        key, token = record(session, kind='text', body='افتتحت المدينة المحطة الجديدة يوم الاثنين.', batch='original')
        assert not session.exec(select(Event)).all()
        with pytest.raises(TaskConflict, match='候选材料已变化'):
            promote(session, None, key, expected_token='stale-token', request_id='initial-promotion')
        session.rollback()
        with pytest.raises(KeyError):
            promote(session, None, 'report:nonexistent', expected_token=token, request_id='missing-candidate')
        session.rollback()
        assert not session.exec(select(Event)).all()

        result = promote(session, None, key, expected_token=token, request_id='initial-promotion',
                         title='新车站开放', note='核对了来源标题和开通日期')
        event = result['event_key']
        assert result['title'] == '新车站开放'
        assert promote(session, None, key, expected_token=token, request_id='initial-promotion',
                       title='新车站开放', note='核对了来源标题和开通日期')['event_key'] == event
        assert len(session.exec(select(Event)).all()) == 1
        with pytest.raises(TaskConflict, match='编号已用于'):
            promote(session, None, key, expected_token=token, request_id='initial-promotion', title='另一条')
        session.rollback()

        evidence, = result['sources']
        assert evidence['origin']['candidate_key'] == key
        assert evidence['origin']['source_ref']['source_language'] == 'ar'
        audit = session.exec(select(EventActivity).where(EventActivity.action == 'promote_candidate')).one()
        assert loads(audit.payload_json, {})['action'] == 'create_event'


def test_evidence_attachment_upgrades_lead_without_overwriting_history(promotion_db):
    with Session(promotion_db) as session:
        event = workbench.create_event(session, {'title': '人工计划'})
        key, token = record(session, kind='summary', body='ناقل جديد', batch='preview')
        attached = promote(session, None, key, expected_token=token, request_id='attach-preview',
                           event_key=event['event_key'], expected_version=event['version'])
        source, = attached['sources']
        assert source['requires_original_text']
        assert source['excerpt'] == 'ناقل جديد'
        old = latest_version(session, source['article_key'])
        assert loads(old.payload_json, {})['content'] == ''
        with pytest.raises(ValueError, match='原文'):
            schedule_model(session, old)

        text_key, text_token = record(session, kind='text', body='تم افتتاح المحطة صباح الاثنين.', batch='original')
        with pytest.raises(TaskConflict, match='事件已更新'):
            promote(session, None, text_key, expected_token=text_token, request_id='stale-version',
                    event_key=event['event_key'], expected_version=event['version'])
        session.rollback()
        updated = promote(session, None, text_key, expected_token=text_token, request_id='attach-original',
                          event_key=event['event_key'], expected_version=attached['version'])

        source, = updated['sources']
        assert source['revision'] == old.revision + 1
        assert not source['requires_original_text']
        assert source['origin']['candidate_key'] == text_key
        assert source['origin']['previous_origin']['candidate_key'] == key
        assert source['excerpt'] == 'تم افتتاح المحطة صباح الاثنين.'
        assert len(session.exec(select(Article)).all()) == len(session.exec(select(ArticleEventLink)).all()) == 1
        assert loads(old.payload_json, {})['content'] == ''


def test_concurrent_retransmissions_share_event_and_receipt(promotion_db):
    with Session(promotion_db) as session:
        key, token = record(session, kind='text', body='افتتحت المحطة.', batch='same-request')

    def create():
        with Session(promotion_db) as session:
            return promote(session, None, key, expected_token=token,
                           request_id='shared-request')['event_key']

    with ThreadPoolExecutor(max_workers=2) as pool:
        one, two = [future.result() for future in (pool.submit(create), pool.submit(create))]
    assert one == two
    with Session(promotion_db) as session:
        assert len(session.exec(select(Event)).all()) == len(session.exec(select(ArticleEventLink)).all()) == 1
        assert len(session.exec(select(EventActivity).where(EventActivity.action == 'promote_candidate')).all()) == 1
