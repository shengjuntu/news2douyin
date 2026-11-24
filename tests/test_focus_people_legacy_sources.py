"""Resolve a missing revision at read time only when its same-event article version is unambiguous."""
import json

from sqlalchemy import delete
from sqlmodel import Session

from news2douyin.desk.models import RadarEntry
from news2douyin.storage.models import Article, ArticleEventLink, ArticleVersion
from test_focus_people_view import add, focus, people_db, read
from test_postgres_integration import postgres_url


def legacy(session, key, **pins):
    row = session.get(RadarEntry, key)
    source, = json.loads(row.sources)
    del source['revision'], source['version_id']
    source.update(pins)
    row.sources = json.dumps([source])
    session.add(row)


def change(session, key, revision, digest):
    version = ArticleVersion(article_key='article-'+key, revision=revision, content_hash=digest, payload_json='{}')
    session.add(version)
    session.flush()
    return version.id


def test_legacy_single_original_can_show_cited_people_without_writing_projection(people_db):
    with Session(people_db) as session:
        focus(session)
        add(session, 'one')
        legacy(session, 'one')
        cache = session.get(RadarEntry, 'one').sources
        session.commit()
    data = read(people_db)
    assert [item['name'] for item in data['items']] == ['Alice']
    action = data['selected']['activities'][0]
    assert action['source_path'] == '/articles/article-one?revision=1#m-mention-one'
    stats = data['window']
    assert stats['sampled_sources'] == stats['legacy_sources'] == stats['recovered_legacy_sources'] == 1
    assert not stats['invalid_sources']
    with Session(people_db) as session:
        assert session.get(RadarEntry, 'one').sources == cache


def test_legacy_exact_pins_missing_versions_and_ambiguous_hashes(people_db):
    with Session(people_db) as session:
        focus(session)
        for key in ('by-id', 'by-hash', 'multiple', 'reused', 'superseded', 'disagrees',
                    'baseline', 'pruned', 'versionless', 'removed-article', 'moved'):
            _, original = add(session, key)
            pins = {}
            if key in ('by-id', 'by-hash', 'multiple', 'reused', 'superseded', 'disagrees'):
                newer = change(session, key, 2, 'new')
                if key == 'by-id':
                    pins = {'version_id': newer}
                if key == 'by-hash':
                    pins = {'content_hash': 'new'}
                if key == 'reused':
                    change(session, key, 3, 'hash-'+key)
                    pins = {'content_hash': 'hash-'+key}
                if key == 'superseded':
                    pins = {'version_id': original}
                if key == 'disagrees':
                    pins = {'version_id': newer, 'content_hash': 'hash-'+key}
            if key == 'baseline':
                row = session.get(ArticleVersion, original)
                row.origin = 'legacy_baseline'
            if key == 'pruned':
                change(session, key, 2, 'new')
            if key in ('pruned', 'versionless'):
                session.exec(delete(ArticleVersion).where(ArticleVersion.id == original))
            if key == 'removed-article':
                session.exec(delete(Article).where(Article.article_key == 'article-'+key))
            if key == 'moved':
                session.exec(delete(ArticleEventLink).where(ArticleEventLink.event_key == key))
            legacy(session, key, **pins)
        session.commit()
    data = read(people_db)
    stats = data['window']
    assert stats['sampled_sources'] == stats['legacy_sources'] == 11
    assert stats['recovered_legacy_sources'] == stats['checked_sources'] == 2
    assert stats['ambiguous_legacy_sources'] == 4
    assert stats['missing_source_versions'] == 2
    assert stats['removed_source_links'] == 1
    assert stats['stale_sources'] == 2
    assert data['items'] == []  # Two proven v2 sources have only old v1 actions in this fixture.


def test_bad_explicit_pins_and_url_only_sources_never_take_legacy_shortcut(people_db):
    with Session(people_db) as session:
        focus(session)
        article, version_id = add(session, 'one')
        row = session.get(RadarEntry, 'one')
        row.sources = json.dumps([
            {'key':article, 'revision':0, 'version_id':0},
            {'key':article, 'revision':None},
            {'key':article, 'version_id':False},
            {'key':article, 'version_id':'1'},
            {'url':'https://qa.example/one', 'version_id':version_id},
            {'key':article, 'article_key':'different'},
        ])
        session.commit()
    data = read(people_db)
    stats = data['window']
    assert stats['invalid_sources'] == stats['sampled_sources'] == 6
    assert stats['legacy_sources'] == 4
    assert stats['recovered_legacy_sources'] == stats['checked_sources'] == 0
    assert not data['items']


def test_legacy_uses_the_current_event_assignment_and_its_article_metadata(people_db):
    from news2douyin.candidates.promotion import attach_articles
    from news2douyin.events.workbench import state_for
    from news2douyin.extraction.models import TextMention

    with Session(people_db) as session:
        focus(session)
        key = 'assigned'
        article, _ = add(session, key)
        newer = change(session, key, 2, 'new')
        session.exec(delete(ArticleEventLink).where(ArticleEventLink.event_key == key))
        state = state_for(session, key)
        assert attach_articles(session, state, [article]) == [article]
        legacy(session, key, title='Earlier headline', when='2000-01-01', url='https://qa.example/old')
        original = session.get(TextMention, 'mention-'+key)
        data = json.loads(original.data)
        data.update(id='new-mention', revision=2)
        session.add(TextMention(id='new-mention', article=article, version_id=newer, revision=2, data=json.dumps(data)))
        session.commit()
    data = read(people_db)
    stats = data['window']
    assert stats['assignment_recovered_sources'] == stats['recovered_legacy_sources'] == 1
    action, = data['selected']['activities']
    assert action['source_path'] == '/articles/article-assigned?revision=2#m-new-mention'
    assert action['source_title'] == 'Primary assigned'
    assert action['source_url'] == 'https://qa.example/assigned'


def test_legacy_editorial_pin_must_be_unambiguous_and_match_this_pair(people_db):
    from news2douyin.storage.models import EventState

    with Session(people_db) as session:
        focus(session)
        for key, note in [('other-only', None), ('mismatch', {'revision': 1}),
                          ('malformed', {'revision': '1'}), ('true', {'revision': True}),
                          ('wrong-hash', {'revision': 2, 'content_hash':'other'}),
                          ('old-pin', {'revision':2}), ('stale-projection', {'revision':2})]:
            article, first = add(session, key)
            if key not in ('malformed', 'true'):
                change(session, key, 2, 'updated')
            legacy(session, key, **({'version_id':first} if key=='old-pin'
                                 else {'revision':1} if key=='stale-projection' else {}))
            note_key = key if note is not None else 'different-event'
            state = session.get(EventState, note_key) or EventState(event_key=note_key)
            state.source_notes_json = json.dumps({article: note if note is not None else {'revision':2}})
            session.add(state)
        session.commit()
    data = read(people_db)
    stats = data['window']
    assert stats['checked_sources'] == stats['assignment_recovered_sources'] == 0
    assert stats['ambiguous_legacy_sources'] == 3
    assert stats['stale_sources'] == 4


def test_deep_or_overlarge_editorial_notes_and_source_json_are_explained(people_db):
    from news2douyin.desk.people_view import ASSIGNMENT_NOTES_LIMIT
    from news2douyin.storage.models import EventState

    deep = '[' * 4000 + '0' + ']' * 4000
    with Session(people_db) as session:
        focus(session)
        for key in ('deep', 'large', 'broken-sources'):
            article, _ = add(session, key)
            legacy(session, key)
            if key == 'broken-sources':
                session.get(RadarEntry, key).sources = deep
            else:
                state = EventState(event_key=key, source_notes_json=deep if key=='deep' else json.dumps(
                    {article: {'revision':1, 'note':'x'*ASSIGNMENT_NOTES_LIMIT}}))
                session.add(state)
        session.commit()
    data = read(people_db)
    assert not data['items']
    assert data['window']['ambiguous_legacy_sources'] == 2
    assert data['window']['invalid_sources'] == 1
    assert data['window']['source_caps']
