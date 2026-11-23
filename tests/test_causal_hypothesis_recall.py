"""Hypotheses, independent semantic recall, and the evidence boundary."""
import json
from unittest.mock import Mock

import pytest
from sqlmodel import Session

from news2douyin.causal import history, model, retrieval, service, vector_recall
from news2douyin.causal.runtime import CausalWorker
from news2douyin.event_embeddings.models import EventEmbeddingJob, EventEmbeddingRecord
from news2douyin.storage.db import init_db, make_engine
from news2douyin.storage.models import Article, ArticleEventLink, Event, EventState
from test_causal_model_isolated import SETTINGS, Response, completion, ranked, wire
from test_postgres_integration import postgres_url


ROOT = '港口暂停装卸'
STRIKE = '码头罢工'
FLOOD = '洪水封航'
START = '2010-01-01T00:00:00'
END = '2014-02-10T00:00:00'
QUOTE = 'Dockworkers stopped unloading after wage talks broke down.'


class Encoder:
    model = 'fixture'
    base_url = 'http://encoder.example/v1'
    dimensions = 2

    def __init__(self, engine):
        self.engine = engine
        self.calls = []

    def get_embedding(self, texts):
        assert self.engine.pool.checkedout() == 0
        self.calls.append(list(texts))
        return [[0., 1.] if text == FLOOD else [1., 0.] for text in texts]


@pytest.fixture(params=['sqlite', 'postgres'])
def causal_db(request, monkeypatch, tmp_path):
    monkeypatch.setenv('NEWS2DOUYIN_TIMEZONE', 'UTC')
    url = request.getfixturevalue('postgres_url') if request.param == 'postgres' else 'sqlite:///' + str(tmp_path/'db')
    engine = make_engine(url)
    init_db(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def add(session, client, key, title, raw='[1,0]', *, stamp='2014-02-03T03:00:00Z',
        body=QUOTE, profile=None, state='ready', merged=False):
    url = 'https://archive.example/' + key
    session.add_all([
        Event(event_key=key, event_title=title, first_seen_at='2026-10-01T00:00:00Z'),
        Article(article_key=key, title=title, content=body, url=url, canonical_url=url, published_at=stamp),
        ArticleEventLink(event_key=key, article_key=key),
        EventEmbeddingJob(event_key=key, state=state),
        EventEmbeddingRecord(event_key=key, model='fixture', profile=profile or retrieval.profile(client),
                             content_hash='fixture', embedding=raw)])
    if merged:
        session.add(EventState(event_key=key, merged_into='previous'))


def test_worker_uses_generated_causes_for_cross_language_recall_and_grounded_judgment(causal_db, monkeypatch, wire):
    engine = causal_db
    client = Encoder(engine)
    with Session(engine) as session:
        add(session, client, 'walkout', 'Workers walked off the job')
        case = service.create(session, ROOT, '2014-02-10', '', False, program=True, date_from='2010-01-01')
    queue, calls, _ = wire
    queue.extend([Response(completion({'hypotheses': [STRIKE, FLOOD]})),
                  Response(completion({'rankings': [ranked('walkout', evidence_quote=QUOTE,
                      reason='工资谈判破裂后工人停工，是否引起港口关闭仍需核对。', relation='possible_cause')]}))])
    monkeypatch.setattr(model, 'configure', lambda: SETTINGS)

    def encoder():
        with Session(engine) as session:
            progress = service.view(session, case)['nodes'][0]
        assert progress['queries'] == [STRIKE, FLOOD] and progress['job_state'] == 'running'
        assert progress['retrieval']['phase'] == 'retrieving'
        return client

    monkeypatch.setattr(retrieval, 'vector_client', encoder)
    archive = Mock(return_value=history.HistoryResult([], {'status': 'no_results'}))
    monkeypatch.setattr(history, 'search_history', archive)
    assert CausalWorker(engine).step()
    with Session(engine) as session:
        parent, child = service.view(session, case)['nodes']
    assert child['event_key'] == 'walkout' and child['relation'] == 'possible_cause'
    assert child['sources'][0]['quote'] == QUOTE and parent['queries'] == [STRIKE, FLOOD]
    report = parent['retrieval']
    assert report['local']['keyword_pool_count'] == 0 and report['local']['vector_only_count'] == 1
    assert report['local']['hypothesis_hits'] == [['walkout'], []]
    assert report['history']['trigger'] == 'no_dated_keyword_candidates'
    archive.assert_called_once()
    assert client.calls == [[STRIKE, FLOOD, ROOT]]
    requests = [json.loads(json.loads(params['data'])['messages'][1]['content']) for _, params, _ in calls]
    assert requests[0]['search_window_utc'] == {'from_inclusive': START, 'before': END}
    assert requests[1]['hypotheses'] == [STRIKE, FLOOD]
    assert [row['ref'] for row in requests[1]['candidates']] == ['walkout']


def test_each_hypothesis_keeps_its_neighbors_through_global_shortlist(causal_db, monkeypatch):
    engine = causal_db
    client = Encoder(engine)
    monkeypatch.setattr(retrieval, 'vector_client', lambda: client)
    # Force several batches. Stronger lexical and parent similarities favor the
    # competing strike candidates, so a pooled top 32 would drop the flood.
    monkeypatch.setattr(vector_recall, 'BATCH_SIZE', 7)
    with Session(engine) as session:
        for i in range(45):
            add(session, client, 'strike-%02d' % i, '码头罢工 '+str(i),
                stamp='201%d-04-02T03:00:00Z' % (i % 4))
        add(session, client, 'water', 'Rapid rise closed the channel', raw='[0,1]',
            body='River levels forced pilots to suspend navigation.')
        session.commit()
    pool = retrieval.collect_tree_pool(engine, [STRIKE, FLOOD], lower=START, upper=END,
                                       exclude=set(), event_text=ROOT)
    result = retrieval.rank_tree_pool(engine, pool)
    assert result.local['vector_scanned_count'] == 46
    strikes, floods = result.local['hypothesis_hits']
    assert len(strikes) == vector_recall.HITS_PER_HYPOTHESIS and floods == ['water']
    refs = [row['ref'] for row in result.rows]
    assert len(set(refs)) == len(refs) == 32
    assert set(strikes + floods) <= set(refs)
    assert client.calls == [[STRIKE, FLOOD, ROOT]]


def test_semantic_only_lane_checks_clocks_profile_sources_and_corrupt_vectors(causal_db, monkeypatch):
    engine = causal_db
    client = Encoder(engine)
    monkeypatch.setattr(retrieval, 'vector_client', lambda: client)
    with Session(engine) as s:
        add(s, client, 'earlier', 'A labor dispute', stamp='2014-02-10T02:00:00+03:00')
        add(s, client, 'late-offset', 'Other news', stamp='2014-02-09T23:00:00-04:00')
        add(s, client, 'cutoff', 'Later account', stamp=END+'Z')
        add(s, client, 'too-old', 'Ancient account', stamp='2008-12-01')
        add(s, client, 'undated', 'Unknown date', stamp='')
        add(s, client, 'ancestor', 'Previously examined')
        add(s, client, 'merged', 'Superseded', merged=True)
        add(s, client, 'profile', 'Different encoder', profile='previous')
        add(s, client, 'job', 'Stale vector', state='pending')
        add(s, client, 'headline', 'Title only', body='')
        for key,raw in [('invalid','oops'), ('null','[null,1]'), ('zero','[0,0]'),
                        ('dim','[1]'), ('nonfinite','[1e500,0]'), ('not-numbers','["1",0]')]:
            add(s, client, key, 'Damaged vector', raw)
        s.commit()
    pool = retrieval.collect_tree_pool(engine, [STRIKE], lower=START, upper=END, exclude={'ancestor'})
    assert pool.local['hypothesis_hits'] == [['earlier']]
    assert [item['ref'] for item in retrieval.rank_tree_pool(engine, pool).rows] == ['earlier']


def test_manual_empty_hypotheses_stop_before_search(causal_db, monkeypatch, wire):
    engine = causal_db
    with Session(engine) as session:
        case = service.create(session, ROOT, '2014-02-10', '', False)
    queue, calls, _ = wire
    queue.append(Response(completion({'hypotheses': []})))
    monkeypatch.setattr(model, 'configure', lambda: SETTINGS)
    search = Mock(side_effect=AssertionError('empty hypotheses must stop'))
    monkeypatch.setattr(retrieval, 'retrieve', search)
    assert CausalWorker(engine).step()
    with Session(engine) as session:
        root, = service.view(session, case)['nodes']
    assert root['job_state'] == 'ready' and len(calls) == 1
    search.assert_not_called()


def test_hypothesis_text_alone_cannot_supply_the_verbatim_anchor(wire):
    queue, _, _ = wire
    queue.append(Response(completion({'rankings': [ranked('prior', evidence_quote=STRIKE)]})))
    with pytest.raises(model.CausalModelError, match='逐字'):
        model.rank(SETTINGS, ROOT, [dict(ref='prior', text='Earlier activity',
                   sources=[dict(title='Port statement', quote=QUOTE)])], hypotheses=[STRIKE])
