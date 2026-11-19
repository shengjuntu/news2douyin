"""Edition admission, provenance, fallback and bounded GDELT minute scheduling."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import Session, select

from news2douyin.intake import ngrams
from news2douyin.intake.official_models import OfficialEvent, OfficialMention
from news2douyin.intake.official_projection import mention_key
from news2douyin.selection import ngram_enrichment as service
from news2douyin.selection.ngram_models import NgramBudget, NgramTarget
from news2douyin.selection.source_models import SourcePreview
from news2douyin.storage.articles import identity_key
from news2douyin.storage.db import init_db, make_engine
from test_postgres_integration import postgres_url

URL = 'https://reg.example/story'
STAMP = '20261114151500'
NOW = datetime(2026, 11, 14, 16, 1, tzinfo=timezone.utc)  # Already the next Beijing day.


@pytest.fixture(params=['sqlite','postgres'])
def backend(request, tmp_path, monkeypatch):
    url = request.getfixturevalue('postgres_url') if request.param == 'postgres' else 'sqlite:///' + str(tmp_path / 'test.db')
    engine = make_engine(url)
    init_db(engine)
    monkeypatch.setenv('NEWS2DOUYIN_NGRAMS_ENABLED', '1')
    monkeypatch.setenv('NEWS2DOUYIN_NGRAMS_AUTO_TARGETS_PER_DAY', '0')
    monkeypatch.setattr(service, '_utc', lambda: NOW)
    monkeypatch.setattr(service, 'dependency_available', lambda: True)
    try:
        yield engine, tmp_path
    finally:
        engine.dispose()


def seed(s, eid, url=URL, actor1='FRANCE', actor2='POLAND'):
    when = '2026-11-14T15:15:00Z'
    s.add(OfficialEvent(event_id=eid, event_date='2026-11-14', feed='events', stamp=STAMP, row_index=1,
                        last_seen=when, actor1_name=actor1, actor2_name=actor2, event_root_code='04',
                        initial_articles=5, source_url=url))
    s.add(OfficialMention(key=mention_key(eid,url), event_id=eid, doc_identifier=url, last_seen=when,
                          first_feed='mentions', first_stamp=STAMP, first_row_index=2,
                          last_feed='mentions', last_stamp=STAMP, last_row_index=2))


def result(text, stamp=STAMP):
    return ngrams.NgramResult('ready', stamp=stamp, text=text, language='en',
                              archive_sha256='a'*64, fragments=9)


@pytest.mark.parametrize('sentence', [
    'France said Germany met with Poland yesterday.',
    'France never met with Poland.',
    'If France met with Poland, the talks were private.',
    'Brazil alleged France met with Poland.',
    'France met with Poland? Officials offered no comment.',
    'Poland met with France at the council.',
    'France and Poland commented on another meeting.',
    'France met with Germany, which thanked Poland.',
])
def test_unqualified_sentence_stays_linked_to_document_only(sentence):
    assert service.match_excerpt(sentence, 'en', 'FRANCE', 'POLAND', '04') == (sentence, [])


def test_grouped_scan_uses_closed_session_and_exact_event_qualification(backend, monkeypatch):
    engine, root = backend
    with Session(engine) as s:
        seed(s,'111')
        seed(s,'222', actor1='BRAZIL')
        s.commit()
    assert service.enqueue_event(engine,'111')['queued'] == 1
    assert service.enqueue_event(engine,'222')['queued'] == 1
    assert service.enqueue_event(engine,'222')['existing'] == 1
    calls = []
    sentence = 'France met with Poland to discuss the rail project.'
    def scan(_root, stamp, urls, allow_download):
        assert engine.pool.checkedout() == 0
        calls.append((stamp, urls))
        return {URL: result('A headline. ' + sentence)}
    monkeypatch.setattr(ngrams,'reconstruct_minute', scan)
    worker = service.NgramWorker(engine,root)
    assert worker.step() and not worker.step()
    assert calls == [(STAMP, {URL})]
    with Session(engine) as s:
        ok = service.event_status(s, '111')['items'][0]
        assert ok['state'] == 'ready' and ok['excerpt'] == sentence and ok['anchors'] == ['France','Poland','met with']
        assert ok['archives'][0]['url'] == ngrams.batch_url(STAMP) and ok['archives'][0]['sha256'] == 'a'*64
        assert ok['mention_ref']['row_index'] == 2 and ok['provenance'] == 'gdelt_web_ngrams'
        assert service.event_status(s,'222')['state'] == 'document_only'
        assert list(service.iter_recovered(s,'2026-11-13T16:00:00Z','2026-11-14T16:00:00Z')) == []
        assert [r['event_id'] for r in service.iter_recovered(s,'2026-11-14T16:00:00Z','2026-11-15T16:00:00Z')] == ['111']
        assert service.qualified_for_events(s,['111','222']) == {'111': ok}
        assert service.qualified_for_events(s,['111'],service._iso(NOW)) == {}
        assert s.get(SourcePreview,identity_key({'url':URL})) is None


def test_neighbor_minute_budget_and_transient_retry(backend,monkeypatch):
    engine,root = backend
    with Session(engine) as s:
        seed(s, '101')
        s.commit()
    service.enqueue_event(engine,'101')
    monkeypatch.setenv('NEWS2DOUYIN_NGRAMS_BATCHES_PER_DAY','2')
    stamps = []
    clock = [NOW]
    monkeypatch.setattr(service,'_utc',lambda:clock[0])
    def scan(_, stamp, urls, allow_download):
        stamps.append(stamp)
        status = 'failed' if len(stamps)==1 else 'missing_batch'
        return {URL: ngrams.NgramResult(status,stamp)}
    monkeypatch.setattr(ngrams,'reconstruct_minute',scan)
    worker = service.NgramWorker(engine,root)
    assert worker.step() and not worker.step()
    with Session(engine) as s:
        row = s.get(NgramTarget,mention_key('101',URL))
        assert row.state == 'failed' and row.minute == STAMP and row.minutes_left == 15
    clock[0] += timedelta(minutes=4)
    assert worker.step() and not worker.step()
    with Session(engine) as s:
        assert s.get(NgramTarget,mention_key('101',URL)).minute == '20261114151400'
        assert s.get(NgramBudget,NOW.date().isoformat()).scans == 2
    assert not service.NgramWorker(engine,root).step()  # Budget survives a process restart.
    clock[0] += timedelta(days=1)
    assert worker.step() and stamps == [STAMP, STAMP, '20261114151400']


def test_live_auto_discovery_works_before_digest_and_preserves_originals(backend, monkeypatch):
    engine,root = backend
    monkeypatch.setenv('NEWS2DOUYIN_NGRAMS_AUTO_TARGETS_PER_DAY','64')
    with Session(engine) as s:
        seed(s,'111')
        another = URL+'/already'
        seed(s,'112',another)
        s.add(SourcePreview(key=identity_key({'url':another}),source_url=another,title='Rail council meets',
                            excerpt='Original publisher extract.',origin='source_page'))
        s.commit()
    calls = []
    def scan(_, stamp, urls, allow_download):
        calls.append(urls)
        return {URL: result('France met with Poland to discuss the railway.')}
    monkeypatch.setattr(ngrams,'reconstruct_minute',scan)
    assert service.NgramWorker(engine,root).step()
    assert calls == [{URL}]
    with Session(engine) as s:
        assert service.event_status(s,'111')['state'] == 'ready'
        assert s.get(NgramTarget,mention_key('112',another)) is None
        assert s.get(NgramBudget,NOW.date().isoformat()).automatic == 1
        assert s.exec(select(SourcePreview)).one().excerpt == 'Original publisher extract.'


def test_disabled_manual_and_missing_mention(backend,monkeypatch):
    engine,root = backend
    with Session(engine) as s:
        seed(s,'100')
        s.commit()
    with pytest.raises(KeyError):
        service.enqueue_event(engine,'999')
    with pytest.raises(ValueError):
        service.enqueue_event(engine,'https://untrusted.example/')
    monkeypatch.setenv('NEWS2DOUYIN_NGRAMS_ENABLED','0')
    assert service.enqueue_event(engine,'100')['state'] == 'disabled'
    assert not service.NgramWorker(engine,root).step()
    with Session(engine) as s:
        assert not service.event_status(s,'100')['enabled']
        assert s.exec(select(NgramTarget)).all() == []

def test_scope_hint_wins_small_allowance_without_creating_membership(backend, monkeypatch):
    from news2douyin.desk.models import Watch, WatchEvent
    engine, root = backend
    monkeypatch.setenv('NEWS2DOUYIN_NGRAMS_AUTO_TARGETS_PER_DAY', '1')
    monkeypatch.setenv('NEWS2DOUYIN_NGRAMS_ALLOW_DOWNLOAD', '0')
    with Session(engine) as s:
        seed(s, '111', actor2='GERMANY')
        seed(s, '222', URL+'/warsaw')
        s.flush()
        s.get(OfficialEvent, '111').initial_articles = 1000
        s.add(Watch(key='my-focus', name='波兰', kind='长期事件', scope='POLAND', created='2026-01-01T01:00:00Z'))
        s.add(Watch(key='paused', name='德国', kind='长期事件', scope='GERMANY', enabled=False,
                    created='2026-01-01T01:00:00Z'))
        s.commit()
    worker = service.NgramWorker(engine, root)
    worker._discover(NOW)
    with Session(engine) as s:
        job = s.exec(select(NgramTarget)).one()
        assert job.event_id == '222' and job.priority == 2
        assert s.exec(select(WatchEvent)).all() == []
        state = service.event_status(s, '222')
        assert state['enabled'] and not state['disabled_reason']
        assert state['budget'] == dict(utc_day='2026-11-14', automatic_used=1, automatic_limit=1,
                                       scans_used=0, scans_limit=96, queued_active=1, queued_limit=200,
                                       cache_only=True, dependency_available=True)


def test_forced_on_without_extra_does_not_queue_or_fetch(backend, monkeypatch):
    engine, root = backend
    with Session(engine) as s:
        seed(s, '100')
        s.commit()
    monkeypatch.setattr(service, 'dependency_available', lambda: False)
    monkeypatch.setattr(ngrams, 'reconstruct_minute', lambda *_a, **_kw: pytest.fail('unexpected fetch'))
    reply = service.enqueue_event(engine, '100')
    assert reply['state'] == 'disabled' and 'gdeltnews' in reply['note']
    assert not reply['budget']['dependency_available']
    assert not service.NgramWorker(engine, root).step()
    with Session(engine) as s:
        status = service.event_status(s, '100')
        assert not status['enabled'] and 'gdeltnews' in status['disabled_reason']
        assert not s.exec(select(NgramTarget)).all()


def test_manual_queue_reports_exhausted_utc_scan_budget(backend, monkeypatch):
    engine, _root = backend
    monkeypatch.setenv('NEWS2DOUYIN_NGRAMS_BATCHES_PER_DAY','1')
    with Session(engine) as s:
        seed(s,'100')
        s.add(NgramBudget(day=NOW.date().isoformat(), scans=1))
        s.commit()
    reply = service.enqueue_event(engine,'100')
    assert reply['state'] == 'queued' and '预算' in reply['note']
    assert reply['budget']['scans_used'] == reply['budget']['scans_limit'] == 1
    assert reply['budget']['queued_active'] == 1
