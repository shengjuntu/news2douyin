"""Earlier material must survive source/neighbor limits and timestamp spellings."""
from sqlmodel import Session, select

from news2douyin.causal import retrieval
from news2douyin.storage.models import Article, ArticleEventLink
from test_causal_hypothesis_recall import Encoder, ROOT, START, STRIKE, END, add, causal_db
from test_postgres_integration import postgres_url


def test_nearest_checks_evidence_before_reserving_a_slot(causal_db, monkeypatch):
    client = Encoder(causal_db)
    monkeypatch.setattr(retrieval, 'vector_client', lambda: client)
    with Session(causal_db) as s:
        for idx in range(30):
            # Nonempty body and valid date pass the cheap time scan. Material
            # classification only later reveals these are headline-only leads.
            add(s, client, f'lead-{idx:02}', 'Unrelated headline')
        add(s, client, 'z-grounded', 'Earlier dockworkers walkout', raw='[0.8,0.6]')
        s.flush()
        for a in s.exec(select(Article).where(Article.article_key.like('lead-%'))):
            a.raw_json = '{"material_kind":"candidate_lead"}'
        s.commit()
    pool = retrieval.collect_tree_pool(causal_db, [STRIKE], lower=START, upper=END,
                                       event_text=ROOT, exclude=set())
    assert pool.local['hypothesis_hits'] == [['z-grounded']]
    assert pool.local['vector_only_count'] == 1


def test_event_with_evidence_after_eight_headlines_is_recoverable(causal_db, monkeypatch):
    client = Encoder(causal_db)
    monkeypatch.setattr(retrieval, 'vector_client', lambda: client)
    with Session(causal_db) as s:
        add(s, client, 'old-port', 'Earlier dockworkers walkout', body='')
        for idx in range(10):
            key = f'link-{idx}'
            url = 'https://archive.example/' + key
            s.add(Article(article_key=key, title='Archive report', url=url, canonical_url=url,
                          content='', published_at='2014-02-01T15:00:00Z'))
            s.add(ArticleEventLink(event_key='old-port', article_key=key))
        url = 'https://archive.example/z-source'
        s.add(Article(article_key='z-source', title='Archive report', url=url, canonical_url=url,
                      content='Dockworkers walked out earlier this week.', published_at='2014-02-01T15:00:00Z'))
        s.add(ArticleEventLink(event_key='old-port', article_key='z-source'))
        s.commit()
    pool = retrieval.collect_tree_pool(causal_db, [STRIKE], lower=START, upper=END, exclude=set())
    assert pool.local['hypothesis_hits'] == [['old-port']]
    assert pool.candidates[0]['sources'][0]['url'] == url


def test_keyword_and_coverage_apply_exact_utc_before_limiting(causal_db, monkeypatch):
    monkeypatch.delenv('EMBEDDING_BASE_URL', raising=False)
    lower = '2014-02-09T22:00:00'
    with Session(causal_db) as s:
        def article(key, stamp):
            url = 'https://archive.example/' + key
            return Article(article_key=key, url=url, canonical_url=url, title='Dockworkers dispute',
                           content='Dockworkers stopped unloading.', published_at=stamp)
        # Lexicographically earlier out-of-window rows must not fill LIMIT 24.
        for i in range(50):
            s.add(article(f'late-{i}', '2014-02-09T22:00:01-03:00'))
        s.add(article('offset', '2014-02-10T02:00:00+03:00'))
        s.commit()
    pool = retrieval.collect_tree_pool(causal_db, ['Dockworkers', 'Volcano'], lower=lower, upper=END,
                                       exclude=set())
    assert pool.local['status'] == 'covered'
    assert pool.local['keyword_hypothesis_hits'] == [1, 0]
    assert [item['ref'] for item in pool.candidates] == ['article:offset']
    with Session(causal_db) as s:
        s.add(article('utc', '2014-02-09T23:00:00Z'))
        s.add(article('compact', '2014-02-10T02:00:00+0300'))
        s.add(article('hour', '2014-02-10T02:00:00+03'))
        s.add(article('fraction', '2014-02-09T23:59:59.999999Z'))
        s.commit()
    pool = retrieval.collect_tree_pool(causal_db, ['Dockworkers'], lower=lower, upper=END, exclude=set())
    assert {item['ref'] for item in pool.candidates} == {'article:utc', 'article:offset', 'article:compact',
                                                         'article:hour', 'article:fraction'}

def test_common_place_does_not_cover_all_competing_hypotheses(causal_db, monkeypatch):
    monkeypatch.delenv('EMBEDDING_BASE_URL', raising=False)
    with Session(causal_db) as s:
        s.add(Article(article_key='one', url='https://archive.example/article', title='Harbor closure',
                      content='The harbor workers went on strike.', published_at='2014-02-01T15:00:00Z'))
        s.commit()
    pool = retrieval.collect_tree_pool(causal_db, ['harbor strike', 'harbor flood'],
                                       lower=START, upper=END, exclude=set())
    assert pool.local['keyword_hypothesis_hits'] == [1, 0]
