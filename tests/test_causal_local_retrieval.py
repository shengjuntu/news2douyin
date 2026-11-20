import json

from sqlmodel import Session

from news2douyin.causal import retrieval
from news2douyin.event_embeddings.models import EventEmbeddingJob, EventEmbeddingRecord
from news2douyin.intake.official_models import OfficialEvent, OfficialMention
from news2douyin.selection.source_models import SourcePreview
from news2douyin.storage.articles import identity_key
from news2douyin.storage.models import Article, ArticleEventLink, Event, EventMoment


LOWER = '2026-11-01T00:00:00'
UPPER = '2026-11-10T12:00:00'


def test_local_fallback_keeps_publisher_evidence_and_distinct_time_clocks(engine, monkeypatch):
    monkeypatch.delenv('EMBEDDING_BASE_URL', raising=False)
    article_url = 'https://paper.example/embargo'
    mention_url = 'https://another.example/meeting'
    published_at = '2026-11-03T10:00:00+02:00'
    exact_quote = 'The ministry discussed sanctions\nwith nearby countries.'
    with Session(engine) as session:
        session.add_all([
            Event(event_key='curated', event_title='Iran sanctions', first_seen_at='2026-11-06T01:00:00Z'),
            Event(event_key='undated', event_title='Iran review', first_seen_at='2026-11-07T01:00:00Z'),
            Event(event_key='excluded', event_title='Iran target', first_seen_at='2026-11-08T01:00:00Z'),
            EventMoment(moment_key='review', event_key='curated', title='Public announcement',
                        reviewed=True, certainty='exact', time_kind='occurred', date_start='2026-11-04',
                        sources_json=json.dumps([{'title': 'Iran sanctions', 'url': article_url,
                                                  'excerpt': exact_quote, 'published_at': published_at}])),
            Article(article_key='main', url=article_url, canonical_url=article_url, title='Iran sanctions',
                    content='Leading content. ' + exact_quote + ' Last line.', published_at=published_at),
            ArticleEventLink(event_key='curated', article_key='main'),
            Article(article_key='undated-article', url='https://paper.example/review', title='Iran review',
                    content='Original article without a stated publication date.', fetched_at='2026-11-07T01:02:00Z'),
            ArticleEventLink(event_key='undated', article_key='undated-article'),
            Article(article_key='target-article', url='https://paper.example/target', title='Iran target',
                    content='A source of the ancestor node.', published_at='2026-11-08T01:00:00Z'),
            ArticleEventLink(event_key='excluded', article_key='target-article'),
            Article(article_key='late', url='https://paper.example/later', title='Iran afterwards',
                    content='Published after this investigation clock.', published_at='2026-11-11T00:00:00Z'),
            Article(article_key='standalone', url='https://paper.example/standalone', title='Iran port',
                    content='Earlier port access report.', published_at='2026-11-01'),
            OfficialEvent(event_id='200', event_date='2026-11-01', actor1_name='IRAN',
                          source_url='https://another.example/only-a-url', feed='events_english', stamp='', row_index=1),
            OfficialEvent(event_id='201', event_date='2026-11-01', actor1_name='IRAN',
                          feed='events_english', stamp='', row_index=2),
            OfficialEvent(event_id='202', event_date='2026-11-01', actor1_name='IRAN',
                          source_url=article_url + '?utm_source=gdelt', feed='events_english', stamp='', row_index=3),
            OfficialMention(key='mention', event_id='201', doc_identifier=mention_url, first_feed='', first_stamp='',
                            first_row_index=1, last_feed='', last_stamp='', last_row_index=1),
            SourcePreview(key=identity_key({'url': mention_url}), source_url=mention_url, title='Original meeting report',
                          excerpt='Diplomats met to discuss Iran sanctions.', published_at='2026-11-01',
                          origin='source_page')
        ])
        session.commit()
    rows = {row['ref']: row for row in retrieval.retrieve(engine, ['Iran sanctions'], lower=LOWER,
                                                            upper=UPPER, exclude={'excluded'})}
    assert {'curated', 'undated', 'article:standalone', 'gdelt:201'} <= rows.keys()
    assert {'excluded', 'article:target-article', 'article:late', 'article:main', 'article:undated-article',
            'gdelt:200', 'gdelt:202'}.isdisjoint(rows)
    assert rows['curated']['occurred_at'] == '2026-11-04'
    assert rows['curated']['published_at'] == '2026-11-03T08:00:00Z'
    assert rows['curated']['sources'][0]['quote'] == exact_quote
    assert rows['undated']['occurred_at'] == rows['undated']['published_at'] == ''
    assert rows['undated']['sources'][0]['published_at'] == ''
    assert rows['gdelt:201']['occurred_at'] == ''
    assert rows['gdelt:201']['sources'][0] == {
        'url': mention_url, 'title': 'Original meeting report', 'quote': 'Diplomats met to discuss Iran sanctions.',
        'published_at': '2026-11-01', 'kind': 'publisher_preview',
    }


def test_utf8_queries_embed_in_one_batch_on_the_worker_profile(engine, monkeypatch):
    requests = []

    class VectorClient:
        base_url = 'https://embedding.example/v1'
        model = 'bge-m3'
        dimensions = 2

        def get_embedding(self, values):
            requests.append(values)
            return [[1., 0.] for _ in values]

    client = VectorClient()
    monkeypatch.setattr(retrieval, 'vector_client', lambda: client)
    with Session(engine) as session:
        session.add_all([
            Event(event_key='vector', event_title='Red Sea operations', first_seen_at='2026-11-03T01:00:00Z'),
            Article(article_key='source', url='https://source.example/sea', title='Operations briefing',
                    content='Officials reopened an important shipping route.', published_at='2026-11-03T01:00:00Z'),
            ArticleEventLink(event_key='vector', article_key='source'),
            EventEmbeddingJob(event_key='vector', state='ready'),
            EventEmbeddingRecord(event_key='vector', profile=retrieval.profile(client), model=client.model,
                                 content_hash='hash', embedding='[1,0]'),
        ])
        session.commit()
    rows = retrieval.retrieve(engine, ['船运', '运输协定', '港口', '第四条'], lower=LOWER, upper=UPPER, exclude=set())
    assert requests == [['船运', '运输协定', '港口']]
    assert [row['ref'] for row in rows] == ['vector'] and rows[0]['similarity'] == 1
