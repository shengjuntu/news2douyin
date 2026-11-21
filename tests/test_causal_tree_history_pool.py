"""History supplement shares time checks and ranking with existing tree recall."""

from sqlmodel import Session

from news2douyin.causal import retrieval
from news2douyin.intake.models import IntakeBatch
from news2douyin.intake.official_models import OfficialEvent
from test_causal_tree_retrieval import LOWER, UPPER, VectorClient, article


def collect(engine, query='Port sanctions'):
    return retrieval.collect_tree_pool(engine, [query], lower=LOWER, upper=UPPER, exclude=set())


def test_local_coverage_is_separate_from_hits_or_evidence(engine, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_TIMEZONE', 'UTC')
    assert collect(engine).local['status'] == 'gap'
    with Session(engine) as session:
        session.add(IntakeBatch(key='archived', feed='events_english', stamp='20141121000000',
                                state='archived', valid_rows=12))
        session.commit()
    pool = collect(engine)
    assert pool.local['status'] == 'unknown'
    assert pool.local['archive_receipt_present'] is True
    assert pool.local['recall_count'] == pool.local['eligible_count'] == 0
    with Session(engine) as session:
        session.add(article('unrelated-history', 2014))
        session.commit()
    pool = collect(engine, 'volcanic eruption')
    assert pool.local['status'] == 'covered'
    assert pool.local['dated_articles_present'] is True
    assert pool.local['recall_count'] == pool.local['eligible_count'] == 0


def test_empty_candidate_pool_keeps_hits_discarded_for_lack_of_local_evidence(engine, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_TIMEZONE', 'UTC')
    with Session(engine) as session:
        session.add(OfficialEvent(event_id='1', event_date='2014-11-21',
            actor1_name='Port sanctions', source_url='https://publisher.example/no-local-material',
            feed='events_english', stamp='20141121000000', row_index=1))
        session.commit()
    pool = collect(engine)
    ranked = retrieval.rank_tree_pool(engine, pool)
    assert ranked.rows == [] and ranked.mode == ''
    assert ranked.local['status'] == 'covered'
    assert ranked.local['recall_count'] == 1
    assert ranked.local['pool_count'] == ranked.local['eligible_count'] == ranked.local['vector_ranked_count'] == 0


def history(ref, url, date, *, kind='publisher_history', quote='Port sanctions constrained shipping capacity.'):
    return dict(ref=ref, text='Port sanctions selected mechanism', occurred_at='1999-01-01',
                gdelt_event_id='1234', gdelt_mention_url=url, sources=[
                    dict(url=url, title='Earlier publisher reporting', quote=quote, published_at=date,
                         kind=kind, gdelt_event_id='1234')])


def test_history_accepts_verified_earlier_publisher_time_and_deduplicates_with_local_pool(engine, monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_TIMEZONE', 'UTC')
    client = VectorClient(engine)
    monkeypatch.setattr(retrieval, 'vector_client', lambda: client)
    with Session(engine) as session:
        session.add(article('already-local', 2014)); session.commit()

    rows = [history('new', 'https://publisher.example/earlier?utm_source=gdelt', '2014-12-03'),
            history('local-duplicate', 'https://fixture.example/already-local?utm_source=gdelt', '2014-01-01'),
            history('history-duplicate', 'https://publisher.example/earlier', '2014-12-03'),
            history('unknown', 'https://publisher.example/unknown', ''),
            history('no-quote', 'https://publisher.example/title-only', '2014-11-21', quote=''),
            history('not-original', 'https://publisher.example/raw', '2014-11-21', kind='local_headline'),
            history('parent-day', 'https://publisher.example/parent', '2022-02-24'),
            history('before-floor', 'https://publisher.example/early', '1988-01-01')]
    ranked = retrieval.rank_tree_pool(engine, collect(engine), historical=rows)

    assert [row['ref'] for row in ranked.rows] == ['new', 'article:already-local']
    assert ranked.history_eligible_count == ranked.history_ranked_count == 1
    assert ranked.local['dated_evidence_count'] == ranked.local['vector_ranked_count'] == 1
    retained = ranked.rows[0]
    assert retained['occurred_at'] == '' and retained['published_at'] == '2014-12-03'
    assert retained['gdelt_event_id'] == '1234'
    assert retained['sources'][0]['kind'] == 'publisher_history'
    assert retained['sources'][0]['gdelt_event_id'] == '1234'
