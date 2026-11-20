import json

import pytest
from sqlmodel import Session, create_engine

from news2douyin.event_embeddings import search
from news2douyin.event_embeddings.models import EventEmbeddingJob, EventEmbeddingRecord
from news2douyin.storage.models import Event, EventState


LOWER = '2026-01-01T00:00:00'
UPPER = '2026-01-20T00:00:00'


@pytest.fixture
def vector_db():
    engine = create_engine('sqlite://')
    for model in (Event, EventState, EventEmbeddingRecord, EventEmbeddingJob):
        model.__table__.create(engine)
    yield engine
    engine.dispose()


def add_vector(session, key, date, values, *, state='ready', profile='test', merged_into=None):
    session.add(Event(event_key=key, event_title=f'事件 {key}', first_seen_at=date))
    session.add(EventEmbeddingJob(event_key=key, state=state))
    session.add(EventEmbeddingRecord(event_key=key, profile=profile, model='test-model',
                                     content_hash=key, embedding=json.dumps(values)))
    if merged_into:
        session.add(EventState(event_key=key, merged_into=merged_into))


def test_search_filters_and_breaks_score_ties_by_recency_and_key(vector_db):
    with Session(vector_db) as session:
        add_vector(session, 'old-score-tie', '2026-01-01T00:00:00+00:00', [3, 0])
        add_vector(session, 'b', '2026-01-13T00:00:00Z', [1, 0])
        add_vector(session, 'a', '2026-01-13T00:00:00Z', [2, 0])
        add_vector(session, 'negative', '2026-01-14T00:00:00Z', [-1, 0])
        add_vector(session, 'zero', '2026-01-15T00:00:00Z', [0, 0])
        add_vector(session, 'wrong-size', '2026-01-16T00:00:00Z', [1, 0, 0])
        add_vector(session, 'pending', '2026-01-17T00:00:00Z', [1, 0], state='pending')
        add_vector(session, 'other-profile', '2026-01-18T00:00:00Z', [1, 0], profile='old')
        add_vector(session, 'merged', '2026-01-19T00:00:00Z', [1, 0], merged_into='a')
        add_vector(session, 'out-of-window', '2026-01-20T00:00:00+00:00', [1, 0])
        session.add(EventState(event_key='a', title_override='整理后的标题'))
        session.commit()
        rows = search.find_similar(session, profile='test', vector=[1, 0], lower=LOWER, upper=UPPER)
        assert [(item['event_key'], item['similarity']) for item in rows] == [
            ('a', 1), ('b', 1), ('old-score-tie', 1), ('negative', -1),
        ]
        assert rows[0]['title'] == '整理后的标题' and rows[0]['first_seen_at'] == '2026-01-13T00:00:00Z'
        assert search.find_similar(session, profile='test', vector=[0, 0], lower=LOWER, upper=UPPER) == []


def test_candidate_limit_applies_after_date_and_profile_filters(vector_db, monkeypatch):
    with Session(vector_db) as session:
        monkeypatch.setattr(search, 'CANDIDATE_LIMIT', 2)
        add_vector(session, 'perfect-too-old', '2026-01-01T00:00:00Z', [1, 0])
        add_vector(session, 'best-allowed', '2026-01-13T00:00:00Z', [1, 1])
        add_vector(session, 'worst-allowed', '2026-01-14T00:00:00Z', [-1, 0])
        add_vector(session, 'another-model', '2026-01-15T00:00:00Z', [1, 0], profile='other')
        session.commit()
        rows = search.find_similar(session, profile='test', vector=[1, 0], lower=LOWER, upper=UPPER, limit=1)
        assert rows[0]['event_key'] == 'best-allowed'
        assert rows[0]['similarity'] == pytest.approx(0.70710678)
