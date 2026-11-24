"""Current focus scope, source revision and bounded read-only people projection."""
import json

import pytest
from sqlalchemy import event as sql_event, delete
from sqlmodel import Session

from news2douyin.desk import people_evidence, people_view
from news2douyin.desk.focus_models import FocusCandidate, FocusDefinition, FocusTrash
from news2douyin.desk.models import RadarEntry, Watch, WatchEvent
from news2douyin.extraction.models import MentionReview, TextMention
from news2douyin.storage.db import init_db, make_engine
from news2douyin.storage.models import Article, ArticleEventLink, ArticleVersion, Event, EventMoment, EventState
from sql_readonly_audit import is_read_only_execution
from test_postgres_integration import postgres_url


UNTIL = '2026-11-21'


@pytest.fixture(params=['sqlite', 'postgres'])
def people_db(engine, request):
    if request.param == 'sqlite':
        yield engine
    else:
        instance = make_engine(request.getfixturevalue('postgres_url'))
        try:
            init_db(instance)
            yield instance
        finally:
            instance.dispose()


def focus(session, key='watched', *, enabled=True):
    session.add(Watch(key=key, name='People '+key, kind='人物', scope='Alice', version=2, enabled=enabled))
    session.add(FocusDefinition(watch=key, description='Saved scope', spec_json='{}', scope_version=2))


def add(session, key, *, watch='watched', person='Alice', organization='Acme',
        seen='2026-11-20T08:00:00Z', choice='auto', scope=2, decision='core',
        published='2026-11-20T08:00:00Z', action_date='2026-11-01', status='reported',
        rejected=False, typed=True, unsafe_url=False):
    article = 'article-'+key
    url = 'javascript:alert(1)' if unsafe_url else 'https://qa.example/'+key
    quote = ((action_date+' ') if action_date else '')+person+' represents '+organization+' at a meeting.'
    session.add(Article(article_key=article, title='Primary '+key, url=url, content=quote, published_at=published))
    version = ArticleVersion(article_key=article, revision=1, content_hash='hash-'+key,
        payload_json=json.dumps(dict(title='Primary '+key, content=quote, url=url, published_at=published)))
    session.add(version); session.flush()
    action = dict(id='mention-'+key, article=article, revision=1, field='content', start=0, end=len(quote),
        quote=quote, trigger='represents', kind='meeting', status=status, time=action_date, date=action_date,
        arguments=[dict(role='actor', text=person, entity_type='person' if typed else 'unknown',
                        evidence=quote, field='content', start=0, end=len(quote)),
                   dict(role='participant', text=organization, entity_type='organization',
                        evidence=quote, field='content', start=0, end=len(quote))])
    source = dict(key=article, revision=1, version_id=version.id, url=url, title='Primary '+key,
                  when=published, time_kind='发布', actions=[action])
    session.add(Event(event_key=key, event_title='Event '+key))
    session.add(RadarEntry(key=key, title='Event '+key, seen=seen, priority=1, band='', reason='',
        evidence='', headings='', material='large text must not be read', sources=json.dumps([source]), geography='[]'))
    session.add(ArticleEventLink(article_key=article, event_key=key))
    session.add(WatchEvent(watch=watch, event=key, choice=choice))
    session.add(FocusCandidate(key='candidate-'+key, watch=watch, event=key, scope_version=scope,
        event_fingerprint='fixture', status='done', decision=decision))
    session.add(TextMention(id=action['id'], version_id=version.id, article=article, revision=1,
        data=json.dumps(action)))
    if rejected:
        session.add(MentionReview(id=action['id'], decision='rejected'))
    return article, version.id


def read(engine, **changes):
    with Session(engine) as session:
        return people_view.page(session, 'watched', days=30, until=UNTIL, **changes)


def test_people_read_respects_live_membership_versions_rejections_and_focus(people_db):
    with Session(people_db) as session:
        focus(session); focus(session, 'other')
        add(session, 'current')
        add(session, 'manual', person='Carol', choice='include', scope=1, decision='exclude')
        add(session, 'old-scope', person='Old Scope', scope=1)
        add(session, 'pending', person='Pending', decision='pending')
        add(session, 'excluded', person='Excluded', choice='exclude')
        add(session, 'merged', person='Merged', choice='include')
        session.add(EventState(event_key='merged', merged_into='current'))
        add(session, 'foreign', person='Foreign', watch='other')
        article, _ = add(session, 'stale', person='Old Revision')
        session.add(ArticleVersion(article_key=article, revision=2, content_hash='new', payload_json='{}'))
        add(session, 'rejected', person='Rejected Action', rejected=True)
        add(session, 'untyped', person='Untyped Name', typed=False)
        session.commit()
    data = read(people_db)
    assert {row['name'] for row in data['items']} == {'Alice', 'Carol'}
    assert data['window']['stale_sources'] == 1
    selected = data['selected']
    assert selected['name'] == 'Alice' and selected['provisional'] is True
    assert selected['activities'][0]['source_path'] == '/articles/article-current?revision=1#m-mention-current'
    assert selected['organizations'][0]['name'] == 'Acme'
    assert selected['organizations'][0]['relation'] == 'represents'
    assert selected['organizations'][0]['provisional'] is True
    assert selected['organizations'][0]['formal_affiliation'] is False


def test_removed_or_moved_article_link_cannot_survive_a_cached_source_projection(people_db):
    with Session(people_db) as session:
        focus(session)
        article, _ = add(session, 'removed', person='Detached')
        add(session, 'valid')
        session.exec(delete(ArticleEventLink).where(ArticleEventLink.article_key == article))
        session.add(ArticleEventLink(article_key=article, event_key='valid'))
        session.commit()
    data = read(people_db)
    assert [row['name'] for row in data['items']] == ['Alice']
    assert data['window']['removed_source_links'] == 1


def test_beijing_material_window_keeps_action_and_publication_clocks_distinct(people_db):
    with Session(people_db) as session:
        focus(session)
        add(session, 'lower', person='Lower', seen='2026-10-22T16:00:00Z', action_date='2000-01-01')
        add(session, 'too-old', person='Outside', seen='2026-10-22T15:59:59Z')
        add(session, 'last', person='Published Only', seen='2026-11-22T00:59:00+09:00', action_date='')
        add(session, 'future', person='Future', seen='2026-11-21T16:00:00Z')
        session.add(EventMoment(moment_key='unrelated-clock', event_key='last', title='Unrelated event clock',
            reviewed=True, certainty='exact', time_kind='occurred', date_start='1991-08-24'))
        session.commit()
    data = read(people_db)
    assert {row['name'] for row in data['items']} == {'Lower', 'Published Only'}
    assert data['window']['date_from'] == '2026-10-23'
    lower = next(row for row in data['items'] if row['name'] == 'Lower')
    action = read(people_db, person=lower['key'])['selected']['activities'][0]
    assert action['time'] == '2000-01-01' and action['time_kind'] == 'reported_occurrence'
    published = next(row for row in data['items'] if row['name'] == 'Published Only')
    action = read(people_db, person=published['key'])['selected']['activities'][0]
    assert action['time'] == '2026-11-20T08:00:00Z' and action['time_kind'] == 'published'


def test_paused_focus_is_readable_trash_and_missing_focus_are_not(people_db):
    with Session(people_db) as session:
        focus(session, enabled=False)
        add(session, 'paused', choice='include')
        session.commit()
    assert read(people_db)['items']
    with Session(people_db) as session:
        session.add(FocusTrash(watch='watched', name='Removed', deleted_at='2026-11-21T00:00:00Z'))
        session.commit()
    with pytest.raises(KeyError):
        read(people_db)
    with Session(people_db) as session, pytest.raises(KeyError):
        people_view.page(session, 'missing', until=UNTIL)


def test_global_identity_selection_and_safe_source_paths_do_not_copy_people_per_focus(people_db):
    with Session(people_db) as session:
        focus(session); focus(session, 'other')
        add(session, 'one', person='特朗普', unsafe_url=True)
        add(session, 'two', person='Donald Trump', watch='other')
        projection = session.get(RadarEntry, 'one')
        session.add(Event(event_key='same-claim', event_title='A second current event grouping'))
        session.add(RadarEntry(key='same-claim', title='A second current event grouping',
            seen=projection.seen, priority=1, band='', reason='', evidence='', headings='', material='',
            sources=projection.sources, geography='[]'))
        session.add(ArticleEventLink(event_key='same-claim', article_key='article-one'))
        session.add(WatchEvent(watch='watched', event='same-claim', choice='include'))
        session.commit()
    one = read(people_db)
    with Session(people_db) as session:
        two = people_view.page(session, 'other', until=UNTIL)
    assert one['items'][0]['key'] == two['items'][0]['key'] == 'person:donald-trump'
    assert not one['selected']['activities'][0]['source_url']
    assert one['selected']['activities'][0]['source_path'] == '/articles/article-one?revision=1#m-mention-one'
    assert one['selected']['activity_count'] == len(one['selected']['activities']) == 1
    assert set(one['selected']['activities'][0]['event_keys']) == {'one', 'same-claim'}
    missing = read(people_db, person='person:absent')
    assert missing['selected'] is None and missing['selection_missing']
    assert missing['requested_person'] == 'person:absent'


def test_view_caps_and_sql_are_bounded_read_only_and_never_load_raw_version_bodies(people_db, monkeypatch):
    monkeypatch.setattr(people_view, 'EVENT_LIMIT', 2)
    monkeypatch.setattr(people_view, 'PEOPLE_LIMIT', 1)
    monkeypatch.setattr(people_view, 'ACTIVITY_LIMIT', 1)
    with Session(people_db) as session:
        focus(session)
        for index in range(4):
            add(session, 'cap-'+str(index), person='Person '+str(index))
        session.commit()
    statements = []
    def audit(_connection, _cursor, sql, _params, context, _many):
        statements.append((sql, is_read_only_execution(sql, context)))
    sql_event.listen(people_db, 'before_cursor_execute', audit)
    try:
        data = read(people_db)
    finally:
        sql_event.remove(people_db, 'before_cursor_execute', audit)
    # Authority uses two PK reads (Watch + FocusTrash), followed by exactly
    # three set-based, bounded material/version/extraction reads.
    assert len(statements) <= 5 and all(read_only for _sql, read_only in statements)
    assert all('payload_json' not in sql and 'radarentry.material' not in sql and 'eventmoment' not in sql
               and 'count(' not in sql.lower() for sql, _read_only in statements)
    assert all('limit' in sql.lower() for sql, _read_only in statements[2:])
    assert data['truncated'] and data['window']['events_truncated'] and data['window']['people_truncated']
    assert data['window']['sampled_events'] == 2 and len(data['items']) == 1


def test_bad_window_or_person_is_rejected_without_projecting(people_db):
    with Session(people_db) as session:
        focus(session); session.commit()
        for changes in ({'days': 31}, {'days': True}, {'until': '2026-02-30'}, {'person': 'x'*161}):
            with pytest.raises(ValueError):
                people_view.page(session, 'watched', **changes)


def test_window_finds_older_linked_extraction_behind_recent_unextracted_events(people_db):
    from news2douyin.desk.people_graph import people_graph

    with Session(people_db) as session:
        focus(session)
        add(session, 'grounded', seen='2026-11-01T01:00:00Z')
        for index in range(120):
            key = f'without-{index}'
            session.add(RadarEntry(key=key, title=key, seen='2026-11-20T00:00:00Z',
                priority=1, band='', reason='', evidence='', headings='', material='', sources='[]', geography='[]'))
            session.add(WatchEvent(watch='watched', event=key, choice='include'))
        session.commit()
        for days in (30, 365):
            graph = people_graph(session, 'watched', days=days, until=UNTIL)
            assert graph['state'] == 'ready' and graph['window']['events_truncated']
            assert {n['title'] for n in graph['nodes']} == {'Alice', 'Acme'}


def test_timeline_latest_action_matches_graph_for_mixed_utc_offsets(people_db):
    from news2douyin.desk.people_graph import people_graph

    with Session(people_db) as session:
        focus(session)
        add(session, 'first', action_date='', published='2026-11-20T09:00:00+08:00')
        add(session, 'last', action_date='', published='2026-11-20T05:00:00Z')
        session.commit()
        graph = people_graph(session, 'watched', until=UNTIL)
        selected = people_view.page(session, 'watched', until=UNTIL)['selected']
    person = next(n for n in graph['nodes'] if n['title'] == selected['name'])
    assert selected['latest_at'] == selected['activities'][0]['time'] == person['activities'][0]['time']
    assert selected['activities'][0]['event_key'] == selected['organizations'][0]['event_key'] == 'last'


def test_postgres_evidence_reads_keep_exception_handlers_out_of_parallel_workers(postgres_url):
    # Both parse helpers catch invalid legacy values using a PL/pgSQL
    # subtransaction; PostgreSQL forbids entering it in a parallel operation.
    from sqlalchemy import text
    db = make_engine(postgres_url)
    try:
        init_db(db)
        with db.connect() as connection:
            flags = connection.execute(text("""
                SELECT oid::regproc::text, proparallel FROM pg_proc
                WHERE oid IN ('n2d_timestamp_or_null(text)'::regprocedure,
                              'n2d_jsonb_or_null(text)'::regprocedure)
            """)).all()
            assert len(flags) == 2
            assert all(flag == 'u' for _, flag in flags)
            assert connection.execute(text("""
                SELECT n2d_timestamp_or_null('not a date'), n2d_jsonb_or_null('malformed')
            """)).one() == (None, None)
    finally:
        db.dispose()
