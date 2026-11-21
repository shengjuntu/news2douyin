"""Opt-in wide local recall and temporary vector ranking; no external news."""
import pytest
from sqlalchemy import event,func
from sqlmodel import Session,select

from news2douyin.causal import retrieval
from news2douyin.event_embeddings.models import EventEmbeddingJob,EventEmbeddingRecord
from news2douyin.llm.client.embedding import EmbeddingError
from news2douyin.storage.models import Article,ArticleEventLink,Event
from news2douyin.storage.db import init_db,make_engine
from test_postgres_integration import postgres_url


LOWER='1989-01-01T00:00:00'
UPPER='2022-02-24T00:00:00'


def article(key,year=2014,*,body='Port sanctions constrained shipping capacity.',published=None):
    return Article(article_key=key,title='Port sanctions '+key,content=body,
        url='https://fixture.example/'+key,canonical_url='https://fixture.example/'+key,
        published_at=published if published is not None else f'{year}-01-01T00:00:00Z',
        fetched_at='2026-11-21T00:00:00Z')


class VectorClient:
    base_url='http://embedding.fixture/v1';model='fixture';dimensions=2
    def __init__(self,engine):self.engine=engine;self.calls=[]
    def get_embedding(self,texts):
        assert self.engine.pool.checkedout()==0,'Remote embedding retained a SQL transaction'
        self.calls.append(list(texts))
        return [[1.,0.] if ('selected mechanism' in text or text=='Port sanctions' or text=='target event')
                else [0.,1.] for text in texts]


def test_recently_ingested_historical_event_is_recalled_by_source_date_and_reuses_vector(engine,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_TIMEZONE','UTC')
    client=VectorClient(engine);monkeypatch.setattr(retrieval,'vector_client',lambda:client)
    with Session(engine) as s:
        s.add_all([Event(event_key='historical',event_title='Port sanctions',first_seen_at='2026-11-21T00:00:00Z'),
            article('historical-source',2010),ArticleEventLink(event_key='historical',article_key='historical-source'),
            EventEmbeddingJob(event_key='historical',state='ready'),
            EventEmbeddingRecord(event_key='historical',profile=retrieval.profile(client),model='fixture',
                content_hash='existing',embedding='[1,0]')]);s.commit()
    result=retrieval.retrieve_for_tree(engine,['Port sanctions'],event_text='target event',
        lower=LOWER,upper=UPPER,exclude=set())
    assert [row['ref'] for row in result]==['historical']
    assert result[0]['published_at']=='2010-01-01T00:00:00Z' and result[0]['occurred_at']==''
    assert result[0]['retrieval']['candidate_vector']=='stored' and result[0]['retrieval']['vector_score']==1.
    assert client.calls==[['Port sanctions','target event']]
    with Session(engine) as s:
        assert s.exec(select(func.count()).select_from(EventEmbeddingRecord)).one()==1
        assert s.exec(select(func.count()).select_from(Event)).one()==1


def test_wide_pool_is_ranked_before_top32_and_candidate_vectors_are_ephemeral(engine,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_TIMEZONE','UTC')
    client=VectorClient(engine);monkeypatch.setattr(retrieval,'vector_client',lambda:client)
    with Session(engine) as s:
        for year in (1991,2000,2010,2019):
            for i in range(20):
                s.add(article(f'{year}-{i:02}',year,body=('Port sanctions selected mechanism.' if year==1991 and i==19
                                                      else 'Port sanctions another unrelated mechanism.')))
        s.commit()
    rows=retrieval.retrieve_for_tree(engine,['Port sanctions'],event_text='target event',lower=LOWER,upper=UPPER,exclude=set())
    assert len(rows)==32 and rows[0]['ref']=='article:1991-19'
    assert rows[0]['retrieval']['pool_count']>32
    assert rows[0]['retrieval']['mode']=='lexical_embedding' and rows[0]['retrieval']['parent_vector_used']
    assert all(row['retrieval']['candidate_vector']=='computed' for row in rows)
    assert max(len(batch) for batch in client.calls[1:])<=retrieval.TREE_EMBED_BATCH
    with Session(engine) as s:
        assert s.exec(select(func.count()).select_from(EventEmbeddingRecord)).one()==0
        assert s.exec(select(func.count()).select_from(EventEmbeddingJob)).one()==0
        assert s.exec(select(func.count()).select_from(Event)).one()==0


def test_unconfigured_embedding_explicitly_uses_lexical_and_keeps_temporal_evidence(engine,monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_TIMEZONE','UTC');monkeypatch.delenv('EMBEDDING_BASE_URL',raising=False)
    with Session(engine) as s:
        s.add_all([article('old',1992),article('new',2025),article('at-cutoff',published=UPPER+'Z')])
        s.commit()
    rows=retrieval.retrieve_for_tree(engine,['Port sanctions'],lower=LOWER,upper=UPPER,exclude=set())
    assert {'article:old'}<={row['ref'] for row in rows}
    assert {'article:new','article:at-cutoff'}.isdisjoint(row['ref'] for row in rows)
    assert all(row['retrieval']['mode']=='lexical' and row['retrieval']['vector_score'] is None for row in rows)
    assert all(row['sources'][0]['quote']=='Port sanctions constrained shipping capacity.' for row in rows)


def test_configured_embedding_failure_is_not_silently_reported_as_empty(engine,monkeypatch):
    with Session(engine) as s:s.add(article('available'));s.commit()
    class Broken(VectorClient):
        def get_embedding(self,texts):raise EmbeddingError('controlled embedding unavailable')
    monkeypatch.setattr(retrieval,'vector_client',lambda:Broken(engine))
    with pytest.raises(EmbeddingError,match='unavailable'):
        retrieval.retrieve_for_tree(engine,['Port sanctions'],lower=LOWER,upper=UPPER,exclude=set())


def test_tree_path_is_read_only_uses_bounded_queries_and_excludes_ancestor_reports(engine,monkeypatch):
    monkeypatch.delenv('EMBEDDING_BASE_URL',raising=False)
    with Session(engine) as s:
        s.add_all([Event(event_key='ancestor',event_title='Port sanctions',first_seen_at='2010-01-01T00:00:00Z'),
            article('linked',2010),ArticleEventLink(event_key='ancestor',article_key='linked'),article('independent',2001)])
        s.commit()
    statements=[]
    def capture(_c,_cursor,sql,_args,context,_many):
        statements.append((sql,context.execution_options.get('n2d_query_name','')))
    event.listen(engine,'before_cursor_execute',capture)
    try:
        rows=retrieval.retrieve_for_tree(engine,['Port sanctions'],lower=LOWER,upper=UPPER,exclude={'ancestor'})
    finally:event.remove(engine,'before_cursor_execute',capture)
    assert [row['ref'] for row in rows]==['article:independent']
    assert not any(sql.lstrip().upper().startswith(('INSERT','UPDATE','DELETE','CREATE')) for sql,_ in statements)
    assert all('LIMIT' in sql.upper() for sql,name in statements if name in (
        'causal.tree.event_recall','causal.tree.article_recall'))


def test_legacy_retrieve_keeps_old_result_budget_and_query_embedding_protocol(engine,monkeypatch):
    client=VectorClient(engine);monkeypatch.setattr(retrieval,'vector_client',lambda:client)
    with Session(engine) as s:
        for i in range(20):s.add(article(f'old-{i:02}',2014))
        s.commit()
    rows=retrieval.retrieve(engine,['Port sanctions'],lower=LOWER,upper=UPPER,exclude=set())
    assert len(rows)<=12 and client.calls==[['Port sanctions']]
    assert all('retrieval' not in row for row in rows)


def test_exact_text_and_primary_url_duplicates_collapse_without_merging_distinct_events(engine,monkeypatch):
    monkeypatch.delenv('EMBEDDING_BASE_URL',raising=False)
    with Session(engine) as s:
        s.add(article('shared',2014))
        for key,title in [('same-a','Port sanctions'),('same-b','  PORT   SANCTIONS  '),
                          ('distinct','Port sanctions were extended')]:
            s.add(Event(event_key=key,event_title=title,first_seen_at='2026-11-21T00:00:00Z'))
            s.add(ArticleEventLink(event_key=key,article_key='shared'))
        s.commit()
    rows=retrieval.retrieve_for_tree(engine,['Port sanctions'],lower=LOWER,upper=UPPER,exclude=set())
    refs={row['ref'] for row in rows}
    assert 'distinct' in refs and len(refs & {'same-a','same-b'})==1
    assert len(rows)==2 and all(row['sources'][0]['url']=='https://fixture.example/shared' for row in rows)


def test_no_index_keywords_are_applied_to_a_bounded_temporal_subquery(engine,monkeypatch):
    monkeypatch.delenv('EMBEDDING_BASE_URL',raising=False)
    monkeypatch.setattr(retrieval,'index_available',lambda _session:False)
    captured=[]
    def capture(_c,_cursor,sql,_args,context,_many):
        if context.execution_options.get('n2d_query_name') in ('causal.tree.event_recall','causal.tree.article_recall'):
            captured.append(sql)
    event.listen(engine,'before_cursor_execute',capture)
    try:retrieval.retrieve_for_tree(engine,['Port sanctions'],lower=LOWER,upper=UPPER,exclude=set())
    finally:event.remove(engine,'before_cursor_execute',capture)
    assert len(captured)==2*retrieval.TREE_SLICES
    assert all(sql.upper().count('LIMIT')>=2 and ' IN (SELECT ' in sql for sql in captured)


@pytest.mark.parametrize('indexed',[True,False])
def test_postgres_historical_pool_and_correlated_time_bounds(postgres_url,monkeypatch,indexed):
    monkeypatch.setenv('NEWS2DOUYIN_TIMEZONE','UTC')
    monkeypatch.setattr(retrieval,'index_available',lambda _session:'pg_trgm' if indexed else False)
    engine=make_engine(postgres_url);init_db(engine)
    try:
        with Session(engine) as s:
            s.add_all([Event(event_key='backfilled',event_title='Port sanctions',first_seen_at='2026-11-21T00:00:00Z'),
                article('history',1999),ArticleEventLink(event_key='backfilled',article_key='history')]);s.commit()
        rows=retrieval.retrieve_for_tree(engine,['Port sanctions'],lower=LOWER,upper=UPPER,exclude=set())
        assert [row['ref'] for row in rows]==['backfilled']
        assert rows[0]['published_at']=='1999-01-01T00:00:00Z'
        assert rows[0]['retrieval']['mode']=='lexical'
    finally:engine.dispose()
