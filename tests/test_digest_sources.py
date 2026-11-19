import json
from datetime import date, datetime, timezone

from sqlmodel import Session

from news2douyin.attention.models import AttentionEventMatch, AttentionItem, AttentionTopic
from news2douyin.desk.models import RadarEntry, Watch, WatchEvent
from news2douyin.digest import sources
from news2douyin.intake.official_models import OfficialEvent, OfficialMention
from news2douyin.selection.source_models import SourcePreview
from news2douyin.storage.articles import identity_key
from news2douyin.storage.models import Article, ArticleEventLink, ArticleIdentity, ArticleVersion

DAY = date(2025, 4, 9)
FIRST = '2025-04-08T16:00:00Z'
LAST = '2025-04-09T15:59:59Z'
NEXT = '2025-04-09T16:00:00Z'


def official(key, when=FIRST, url=''):
    return OfficialEvent(event_id=key, event_date='2025-03-15', last_seen=when,
                         feed='events_english', stamp='f', row_index=1,
                         source_url=url, ingested_at=when, event_root_code='04',
                         action_country_iso2='pl', goldstein=-6.5)


def mention(key, url, when, confidence, *, received=None):
    return OfficialMention(key=str(key), event_id='02', doc_identifier=url, last_seen=when,
        updated_at=received or when, confidence=confidence, first_feed='f', first_stamp='f',
        last_feed='f', last_stamp='f', first_row_index=1, last_row_index=1)


def preview(url, title, when):
    return SourcePreview(key=identity_key({'url': url}), source_url=url, title=title,
                         fetched_at=when)


def radar(key):
    return RadarEntry(key=key, title='later mutable title', seen=NEXT, priority=3,
                      band='b', reason='', evidence='', headings='', material='', sources='[]', geography='[]')


def version(key, revision, observed, title, url):
    return ArticleVersion(article_key=key, revision=revision, content_hash=str(revision),
        payload_json=json.dumps(dict(title=title, url=url, content=title + ' body')),
        observed_at=observed)


def topic(key, time=FIRST):
    return AttentionTopic(key=key, title='mutable topic title', created=FIRST, last_seen=time,
                          updated_at=time, attention_score=80, generation='g')


def test_official_finds_readable_low_confidence_mention_and_honors_cutoff(engine, monkeypatch):
    monkeypatch.setattr(sources, 'BATCH', 2)
    url = 'https://reports.example/2'
    later = 'https://late.example/8'
    with Session(engine) as session:
        session.add_all([official('01'), official('02', LAST), official('03', NEXT, url),
            topic('interest'), AttentionEventMatch(key='m', topic='interest', event_id='02',
                method='mention_document_url', generation='g', updated_at=LAST),
            mention(18, url, FIRST, 11), mention(19, later, LAST, 100, received=NEXT),
            preview(url, 'Talks produce written agreement', LAST), preview(later, 'Future evidence', NEXT)])
        session.add_all([mention(i, f'https://wire.example/no-title/{i}', FIRST, 95) for i in range(17)])
        session.commit()
        stats = {}
        rows = list(sources.iter_candidates(session, DAY, stats=stats))
    assert len(rows) == 1
    row = rows[0]
    assert (row.title, row.source_url, row.attention, row.impact, row.country) == (
        'Talks produce written agreement', url, 0.8, 0.65, 'PL')
    assert row.domains == ('reports.example', 'wire.example')
    assert (stats['official_seen'], stats['official_no_readable'], stats['official_emitted']) == (2, 1, 1)


def test_historical_local_and_official_use_prior_material_and_active_watches(engine, monkeypatch):
    monkeypatch.setattr(sources, 'BATCH', 1)
    link = 'https://example.com/doc'
    with Session(engine) as session:
        session.add_all([
            Article(article_key='a', title='future rewritten title', url=link, fetched_at=NEXT),
            ArticleIdentity(identity_key=identity_key({'url': link}), article_key='a'),
            version('a', 1, FIRST, 'Local source confirms new terms', link),
            version('a', 2, NEXT, 'Future rewrite', link),
            version('b', 1, LAST, 'Updated terms signed', 'https://other.example/terms'),
            version('c', 1, FIRST, 'bad source scheme', 'javascript:alert(1)'),
            official('9', FIRST, link), radar('e'), radar('bad'),
            ArticleEventLink(article_key='a', event_key='e'), ArticleEventLink(article_key='b', event_key='e'),
            ArticleEventLink(article_key='c', event_key='bad'),
            Watch(key='w', name='供应链', kind='长期事件', scope='供应链', created=FIRST),
            Watch(key='x', name='已排除', kind='长期事件', scope='供应链', created=FIRST),
            WatchEvent(watch='w', event='e', choice='include', first_seen=LAST),
            WatchEvent(watch='x', event='e', choice='exclude', first_seen=LAST)])
        session.commit()
        stats = {}
        rows = {row.key: row for row in sources.iter_candidates(session, DAY, stats=stats)}
        assert rows['gdelt:9'].title == 'Local source confirms new terms'
        local = rows['local:e']
        assert (local.title, local.domains, local.watches, local.observed_at) == (
            'Updated terms signed', ('example.com', 'other.example'), ('供应链',), LAST)
        assert (stats['local_seen'], stats['local_no_readable']) == (2, 1)
        cutoff = datetime(2025, 4, 9, 1, tzinfo=timezone.utc)
        earlier = {c.key: c for c in sources.iter_candidates(session, DAY, cutoff=cutoff)}
        assert earlier['local:e'].title == rows['gdelt:9'].title
        assert earlier['local:e'].watches == ()


def test_new_attention_requires_linked_item_and_does_not_use_future_metric(engine):
    with Session(engine) as session:
        top = topic('t')
        top.updated_at = NEXT
        session.add_all([top, topic('empty'), AttentionItem(key='early', topic='t', title='报道原题',
            source_id='weibo', url='https://weibo.example/a', last_seen=FIRST),
            AttentionItem(key='later', topic='t', title='未来文本', source_id='weibo',
                          url='https://weibo.example/future', last_seen=NEXT)])
        session.commit()
        stats = {}
        items = list(sources.iter_candidates(session, DAY, stats=stats))
        assert [(c.title, c.attention, c.source_kind) for c in items] == [('报道原题', None, 'newsnow')]
        assert (stats['newsnow_seen'], stats['newsnow_emitted'], stats['newsnow_no_readable']) == (2, 1, 1)


# The PostgreSQL driver streams the source query with a server-side cursor while
# the same transaction opens additional cursors to resolve source documents.
from test_postgres_integration import postgres_url


def test_streams_real_postgres_projection(postgres_url, monkeypatch):
    from news2douyin.storage.db import make_engine, init_db
    database = make_engine(postgres_url)
    try:
        init_db(database)
        test_historical_local_and_official_use_prior_material_and_active_watches(database, monkeypatch)
    finally:
        database.dispose()


def test_daily_recovers_older_code_by_actual_observation_day_without_future_metadata(engine):
    from news2douyin.selection.ngram_models import NgramTarget
    from news2douyin.digest.ranking import rank_day
    source = 'https://daily.example/summit'
    stamp = '20250408145500'
    quote = 'Poland and France held consultations in Warsaw.'
    def evidence(key, event, at, url=source):
        return NgramTarget(key=key,event_id=event,url=url,actor1='Poland',actor2='France',root_code='04',
            reference_stamp=stamp,reference_row=1,minute=stamp,minutes_left=0,requested_at=FIRST,
            observed_at=at,state='ready',language='en',excerpt=quote,method='gdeltnews',anchors_json='["Poland","consultations"]')
    with Session(engine) as session:
        # The current compact record changed tomorrow. Only the frozen evidence
        # was known today; its earlier actor/code must survive in the edition.
        session.add_all([official('33', NEXT), evidence('target','33',LAST),
                         evidence('tomorrow','future',NEXT), official('04',FIRST),
                         evidence('pending','04',FIRST,'https://other.example/events')])
        session.commit()
        stats = {}
        rows = list(sources.iter_candidates(session, DAY, stats=stats))
    assert {c.key for c in rows} == {'gdelt:33','gdelt:04'}
    assert all(c.actors in (('Poland','France'), ()) for c in rows)
    assert all(c.reconstruction['title_origin'] == 'reconstructed_sentence' for c in rows)
    old = rank_day(rows,mode='balanced_v031')
    assert not old['items']
    assert stats['official_reconstructed'] == 2
    for row in rows:
        assert row.observed_at == (LAST if row.key=='gdelt:33' else FIRST)
        assert row.excerpt == quote and row.action_evidence['kind'] == 'reconstructed_action_lead'


def test_exact_url_supplements_original_title_but_preserves_old_policy_input(engine):
    from news2douyin.selection.ngram_models import NgramTarget
    from news2douyin.digest.ranking import rank_day
    url = 'https://paper.example/brief'
    with Session(engine) as s:
        s.add_all([official('8', FIRST, url), preview(url,'Publisher headline on summit',FIRST),
            NgramTarget(key='ng',event_id='8',url=url,reference_stamp='s',reference_row=1,minute='s',minutes_left=0,
                requested_at=FIRST,observed_at=LAST,state='ready',excerpt='Poland and France signed the deal.')])
        s.commit()
        rows = list(sources.iter_candidates(s, DAY))
    assert len(rows) == 1 and rows[0].title == 'Publisher headline on summit'
    assert rows[0].reconstruction['title_origin'] == 'publisher'
    assert rank_day(rows,mode='balanced_v031')['items'][0]['excerpt'] == ''


def test_reconstruction_attaches_publishers_title_only_for_byte_exact_document_url():
    entry = dict(url='https://local.test/story?utm_source=radio', excerpt='Source observation')
    other = sources._Report('Another captured URL', 'https://local.test/story', '')
    report, data = sources._recovered_report(entry, other)
    assert report.title == entry['excerpt'] and data['title_origin'] == 'reconstructed_sentence'
    assert report.url == entry['url']
