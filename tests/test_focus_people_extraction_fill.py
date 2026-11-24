"""The focus action requests a bounded set of real versions on the durable reader."""
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.desk.models import RadarEntry, Watch, WatchEvent
from news2douyin.desk.people_repair import LIMIT, PRECHECK_CHARS
from news2douyin.extraction import service
from news2douyin.extraction.models import ExtractionAttempt, MentionReview, TextMention, VersionReading
from news2douyin.server.app import create_app
from news2douyin.storage.models import Article, ArticleEventLink, ArticleVersion, Event
from test_postgres_integration import postgres_url


WINDOW = {'days': 30, 'until': '2026-11-20'}
WATCH = 'w_' + '1' * 32


@pytest.fixture(params=('sqlite', 'postgres'))
def web(request, tmp_path, monkeypatch):
    url = (request.getfixturevalue('postgres_url') if request.param == 'postgres'
           else 'sqlite:///' + str(tmp_path/'fill.db'))
    monkeypatch.setenv('NEWS2DOUYIN_EVENT_READER', 'rules')
    monkeypatch.setenv('NEWS2DOUYIN_FALKOR_URL', '')
    app = create_app(db_url=url, storage_root=str(tmp_path/'runs'))
    with Session(app.state.engine) as session:
        session.add(Watch(key=WATCH, name='焦点', kind='事件', scope='测试'))
        session.commit()
    client = TestClient(app)  # Worker progress is advanced explicitly in these tests.
    yield app.state.engine, client
    client.close()
    app.state.engine.dispose()


def source(session, key, *, state='baseline', choice='include', day='2026-11-18',
           document=None, statement=False):
    article, event = 'a-'+key, 'e-'+key
    url = 'https://example.org/' + key
    title = 'Donald Trump announced a decision.'
    session.add(Article(article_key=article, title=title, url=url))
    session.add(Event(event_key=event, event_title=title))
    payload = json.dumps({'title': title, 'content': title} if document is None else document)
    version = ArticleVersion(article_key=article, revision=1, content_hash='h-'+key, payload_json=payload)
    session.add(version)
    session.flush()
    job = session.get(VersionReading, version.id)
    assert job is not None
    if state is None:
        session.delete(job)
    else:
        job.state = state
        job.method = 'rules' if state == 'ready' else ''
        if state == 'rule_running':
            job.lease, job.until = 'active-lease', 9999999999.0
        if state == 'model_wait':
            job.until = 9999999999.0
        session.add(job)
    # Old radar snapshots may only have the article key and URL.
    session.add(RadarEntry(key=event, title=title, seen=day+'T08:00:00Z', priority=1,
                band='', reason='', evidence='', headings='', material='', geography='[]',
                sources=json.dumps([{'key': article, 'url': url}])))
    session.add(ArticleEventLink(article_key=article, event_key=event))
    session.add(WatchEvent(watch=WATCH, event=event, choice=choice))
    if statement:
        ident = 'old-'+key
        session.add(TextMention(id=ident, version_id=version.id, article=article, revision=1, data='{}'))
        session.add(MentionReview(id=ident, decision='rejected'))
    return version.id


def submit(client):
    response = client.post('/api/focus/'+WATCH+'/people/extraction', params=WINDOW)
    assert response.status_code == 202, response.text
    return response.json()


def test_fill_only_current_missing_and_does_not_overwrite_completed_reviews_or_leases(web):
    engine, client = web
    with Session(engine) as s:
        regular = source(s, 'outside', choice='exclude')
        source(s, 'too-old', day='2024-01-01')
        absent = source(s, 'missing', state=None)
        queued = source(s, 'baseline')
        source(s, 'empty-success', state='ready')
        running = source(s, 'running', state='rule_running')
        failed = source(s, 'failed', state='error')
        s.add(ExtractionAttempt(version_id=failed, attempts=4, method='model'))
        waiting = source(s, 'retry', state='model_wait', statement=True)
        s.add(ExtractionAttempt(version_id=waiting, attempts=1, method='model'))
        preserved = source(s, 'kept', state=None, statement=True)
        ungrounded = source(s, 'discovery', document={'raw': {'material_kind':'candidate_lead'}, 'title':'Headline'})
        source(s, 'gkg-headline', document={'provider':'gkg', 'raw':{'material_kind':'discovery_metadata'},
                                 'title':'Donald Trump announced a decision.'})
        s.commit()

    read = client.get('/api/focus/'+WATCH+'/people', params=WINDOW)
    assert read.status_code == 200
    with Session(engine) as s:
        assert s.get(VersionReading, absent) is None
        assert s.get(VersionReading, queued).state == 'baseline'
    result = submit(client)
    assert (result['queued'], result['pending'], result['done'], result['failed'],
            result['unavailable'], result['remaining']) == (2, 2, 2, 1, 2, 0)
    repeat = submit(client)
    assert repeat['queued'] == 0 and repeat['pending'] == 4

    with Session(engine) as s:
        assert s.get(VersionReading, running).lease == 'active-lease'
        assert s.get(ExtractionAttempt, failed).attempts == 4
        assert s.get(VersionReading, waiting).state == 'model_wait'
        assert s.get(VersionReading, preserved) is None
        assert s.get(MentionReview, 'old-kept').decision == 'rejected'
        assert s.get(VersionReading, ungrounded).state == 'metadata_only'

    # The normal queue has older IDs, yet both focus versions are processed first.
    assert service.step(engine) and service.step(engine)
    with Session(engine) as s:
        assert s.get(VersionReading, regular).state == 'baseline'
        for ident in (queued, absent):
            assert s.get(VersionReading, ident).state == 'ready'
            assert s.get(VersionReading, ident).method == 'rules'
            assert s.get(ExtractionAttempt, ident) is None
            assert s.exec(select(TextMention).where(TextMention.version_id == ident)).first()
    people = client.get('/api/focus/'+WATCH+'/people', params=WINDOW).json()
    assert any('Trump' in person['name'] for person in people['items'])
    assert submit(client)['queued'] == 0
    path = '/api/focus/'+WATCH+'/people/extraction'
    assert client.post(path, params={'days':5}).status_code == 422
    assert client.post(path, params=WINDOW, headers={'origin':'https://elsewhere.test'}).status_code == 403
    assert client.post('/api/focus/unknown/people/extraction', params=WINDOW).status_code == 404


def test_fill_caps_repeats_and_preserves_requested_model_after_priority_baseline(web):
    engine, client = web
    with Session(engine) as s:
        ordinary = source(s, 'ordinary', choice='exclude')
        ids = [source(s, 'batch-'+str(i)) for i in range(LIMIT + 2)]
        job = s.get(VersionReading, ids[0])
        job.note = 'model_after_baseline'
        s.add(job)
        s.commit()
    first = submit(client)
    assert (first['queued'], first['remaining']) == (LIMIT, 2)
    second = submit(client)
    assert (second['queued'], second['pending'], second['remaining']) == (2, LIMIT, 0)
    assert submit(client)['queued'] == 0
    assert service.step(engine, regular_first=True)
    assert service.step(engine)
    with Session(engine) as s:
        assert s.get(VersionReading, ordinary).state == 'ready'
        assert s.get(VersionReading, ids[0]).state == 'model_queued'


def test_large_payloads_are_checked_by_worker_and_bad_material_does_not_block_next_article(web):
    engine, client = web
    with Session(engine) as s:
        bad = source(s, 'too-deep')
        raw = '[' * (PRECHECK_CHARS + 1)
        s.get(ArticleVersion,bad).payload_json = raw + '0' + ']' * len(raw)
        lead = source(s, 'big-lead', document={'raw':{'material_kind':'candidate_lead'}, 'content':'H'*PRECHECK_CHARS})
        good = source(s, 'big-original', document={'title':'Donald Trump announced a decision.', 'raw':'x'*PRECHECK_CHARS})
        s.commit()
    first = submit(client)
    assert first['queued'] == first['worker_checks'] == 3
    for _ in range(3):
        service.step(engine)
    with Session(engine) as s:
        assert s.get(VersionReading, bad).state == s.get(VersionReading, lead).state == 'metadata_only'
        assert s.get(VersionReading, good).state == 'ready'
        assert not s.exec(select(TextMention).where(TextMention.version_id.in_([bad,lead]))).first()


def test_overlapping_focus_requests_share_one_priority_reading_and_do_not_reset_it(web):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    engine, client = web
    other = 'w_' + '2' * 32
    with Session(engine) as s:
        s.add(Watch(key=other, name='交叉焦点', kind='事件', scope='测试'))
        ident = source(s, 'shared', state=None)
        s.add(WatchEvent(watch=other, event='e-shared', choice='include'))
        s.commit()
    barrier = Barrier(2)

    def request(key):
        concurrent_client = TestClient(client.app)  # Do not start a second application lifespan.
        try:
            barrier.wait(timeout=10)
            response = concurrent_client.post('/api/focus/'+key+'/people/extraction', params=WINDOW)
            assert response.status_code == 202
            return response.json()['queued']
        finally:
            concurrent_client.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        counts = list(workers.map(request, (WATCH, other)))
    assert sorted(counts) == [0, 1]
    assert service.step(engine)
    with Session(engine) as s:
        assert s.get(VersionReading, ident).state == 'ready'
        assert s.exec(select(TextMention.id).where(TextMention.version_id == ident)).first()
