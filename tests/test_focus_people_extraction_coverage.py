"""Focus people coverage distinguishes queue history, grounded versions and people."""
import json

from sqlalchemy import delete
from sqlmodel import Session

from news2douyin.desk.models import RadarEntry, WatchEvent
from news2douyin.desk.people_graph import people_graph
from news2douyin.desk.people_view import page
from news2douyin.extraction import service
from news2douyin.extraction.models import ExtractionAttempt, TextMention, VersionReading
from news2douyin.storage.models import ArticleEventLink, ArticleVersion
from test_focus_people_view import UNTIL, add, focus, people_db
from test_postgres_integration import postgres_url


def test_reported_window_distinguishes_old_sources_failed_attempts_and_zero_people(people_db):
    with Session(people_db) as s:
        focus(s)
        for i in range(48):
            key = 'window-' + str(i)
            article, vid = add(s, key)
            s.delete(s.get(TextMention, 'mention-' + key))
            if i < 43:
                source = s.get(RadarEntry, key)
                source.sources = json.dumps([{'key': article}])
                s.add(ArticleVersion(article_key=article, revision=2, content_hash='revised-' + key,
                                     payload_json='{}'))
            elif i in (43, 44):
                job = s.get(VersionReading, vid)
                job.state = 'model_wait' if i == 43 else 'error'
                s.add(ExtractionAttempt(version_id=vid, method='model', attempts=2))
            elif i == 45:
                job = s.get(VersionReading, vid)
                job.state, job.method = 'ready', 'rules'
            elif i == 46:
                s.delete(s.get(VersionReading, vid))
            if i == 43:
                for kind in ('meeting', 'statement'):
                    quote = 'Russia-Ukraine talks in Abu Dhabi.'
                    args = [{'role':'actor', 'text':'Russia-Ukraine', 'entity_type':'unknown'},
                            {'role':'location', 'text':'Abu Dhabi', 'entity_type':'place'}]
                    s.add(TextMention(id=kind, version_id=vid, article=article, revision=1,
                                      data=json.dumps(dict(kind=kind, quote=quote, arguments=args))))
        s.commit()
        panel = page(s, 'watched', until=UNTIL)
        graph = people_graph(s, 'watched', until=UNTIL)
    w = graph['window']
    assert graph['nodes'] == graph['edges'] == panel['items'] == []
    assert graph['empty_reason'] == panel['empty_reason'] and '2 条' in graph['empty_reason']
    assert w['sampled_events'] == w['sampled_sources'] == 48
    assert w['ambiguous_legacy_sources'] == 43 and w['checked_sources'] == 5
    counts = w['extraction']
    assert panel['window']['extraction'] == counts
    assert counts['versions'] == 5 and counts['with_person_actions'] == 0
    assert counts['with_mentions'] == counts['with_reviewable_mentions'] == 1
    assert counts['statements_without_person'] == 2
    assert counts['examples_without_person'] == [dict(title='Primary window-43',
        source_path='/articles/article-window-43?revision=1#m-meeting')]
    assert counts['failed'] == counts['retrying'] == 1
    assert counts['completed_empty'] == counts['queued'] == counts['unqueued'] == 1
    assert counts['repair_candidates'] == 2


def test_legacy_evidence_rejection_and_repeated_event_share_one_version(people_db):
    with Session(people_db) as s:
        focus(s)
        article, first = add(s, 'good')
        _, rejected = add(s, 'rejected', rejected=True)
        s.delete(s.get(VersionReading, first))
        s.delete(s.get(VersionReading, rejected))
        entry = s.get(RadarEntry, 'good')
        s.add(RadarEntry(key='duplicate', title='Second group', seen=entry.seen, priority=1,
              band='', reason='', evidence='', headings='', material='', sources=entry.sources, geography='[]'))
        s.add(ArticleEventLink(article_key=article, event_key='duplicate'))
        s.add(WatchEvent(watch='watched', event='duplicate', choice='include'))
        s.commit()
        panel = page(s, 'watched', until=UNTIL)
    w = panel['window']
    stats = w['extraction']
    assert w['sampled_sources'] == w['checked_sources'] == 3
    assert stats['versions'] == stats['evidence_only'] == stats['with_mentions'] == 2
    assert stats['with_person_actions'] == stats['with_reviewable_mentions'] == 1
    assert stats['rejected_only_versions'] == 1
    assert stats['repair_candidates'] == stats['unqueued'] == 0
    assert stats['completed_empty'] == 0


def test_successful_zero_extraction_clears_failure_record_but_remains_complete(people_db, monkeypatch):
    with Session(people_db) as s:
        focus(s)
        _, vid = add(s, 'no-statements')
        s.exec(delete(TextMention))
        s.add(ExtractionAttempt(version_id=vid, method='rules', attempts=1))
        s.commit()
    monkeypatch.setenv('NEWS2DOUYIN_EVENT_READER', 'rules')
    monkeypatch.setattr(service.reader, 'extract', lambda *_: ([], 0))
    assert service.step(people_db)
    with Session(people_db) as s:
        assert s.get(ExtractionAttempt, vid) is None
        graph = people_graph(s, 'watched', until=UNTIL)
        stats = graph['window']['extraction']
    assert stats['completed'] == stats['completed_empty'] == 1
    assert stats['repair_candidates'] == stats['unqueued'] == stats['failed'] == 0
    assert graph['state'] == 'empty' and '未产出陈述' in graph['empty_reason']


def test_non_person_examples_are_scoped_capped_and_keep_local_version_links(people_db):
    with Session(people_db) as s:
        focus(s)
        for i in range(5):
            add(s, 'example-' + str(i), person='Untyped Actor', typed=False, unsafe_url=True)
        s.commit()
        graph = people_graph(s, 'watched', until=UNTIL)
    info = graph['window']['extraction']
    assert info['statements_without_person'] == 5
    examples = info['examples_without_person']
    assert len(examples) == 3
    assert len({row['source_path'] for row in examples}) == 3
    for row in examples:
        key = row['title'].removeprefix('Primary ')
        assert row['source_path'] == f'/articles/article-{key}?revision=1#m-mention-{key}'


def test_inconsistent_stored_mention_is_neither_a_person_nor_an_unprocessed_version(people_db):
    with Session(people_db) as s:
        focus(s)
        _, version = add(s, 'unexpected')
        s.delete(s.get(VersionReading, version))
        s.get(TextMention, 'mention-unexpected').revision = 2
        s.commit()
        graph = people_graph(s, 'watched', until=UNTIL)
    stats = graph['window']['extraction']
    assert graph['nodes'] == [] and stats['versions'] == stats['unknown'] == 1
    assert stats['with_mentions'] == stats['rejected_only_versions'] == 0
    assert stats['repair_candidates'] == stats['unqueued'] == stats['evidence_only'] == 0
