"""Graph projection preserves scope, source versions, action roles and limits."""
import json

import pytest
from sqlmodel import Session

from news2douyin.desk.people_graph import MAX_JOINT_PEOPLE, people_graph
from news2douyin.desk.models import RadarEntry, WatchEvent
from news2douyin.extraction.models import TextMention
from news2douyin.storage.models import ArticleEventLink, ArticleVersion, Event
from test_focus_people_view import UNTIL, add, focus, people_db
from test_postgres_integration import postgres_url


def mention(session, article, version_id, key, quote, roles, status='reported'):
    args = [dict(text=name, role=role, entity_type='person') for name, role in roles]
    data = dict(kind='meeting', quote=quote, arguments=args, status=status, field='content')
    session.add(TextMention(id=key, article=article, version_id=version_id,
                            revision=1, data=json.dumps(data)))


def read(engine, **changes):
    with Session(engine) as session:
        return people_graph(session, 'watched', until=UNTIL, **changes)


def test_same_action_connections_only_and_revision_links(people_db):
    with Session(people_db) as session:
        focus(session)
        focus(session, 'other')
        article, version = add(session, 'one')
        mention(session, article, version, 'meeting', 'Alice and Bob spoke about Carol.',
                [('Alice', 'actor'), ('Alice', 'speaker'), ('Bob', 'participant'), ('Carol', 'target')])
        mention(session, article, version, 'quote', 'Dana quoted Eve.',
                [('Dana', 'actor'), ('Eve', 'source')])
        mention(session, article, version, 'plan', 'Bob and Alice plan to attend.',
                [('Bob', 'participant'), ('Alice', 'actor')], 'planned')
        entry = session.get(RadarEntry, 'one')
        session.add(Event(event_key='second-group', event_title='The same source'))
        session.add(RadarEntry(key='second-group', title='Other grouping', seen=entry.seen, priority=1,
                    band='', reason='', evidence='', headings='', material='', sources=entry.sources, geography='[]'))
        session.add(ArticleEventLink(event_key='second-group', article_key=article))
        session.add(WatchEvent(watch='watched', event='second-group', choice='include'))
        old, _ = add(session, 'old', person='Old')
        session.add(ArticleVersion(article_key=old, revision=2, content_hash='replaced', payload_json='{}'))
        add(session, 'excluded', person='Excluded', choice='exclude')
        add(session, 'foreign', person='Other Focus', watch='other')
        add(session, 'removed', person='Rejected', rejected=True)
        session.commit()
    graph = read(people_db)
    assert graph['backend'] == 'focus_evidence'
    nodes = {node['id']: node for node in graph['nodes']}
    assert {n['title'] for n in nodes.values()} == {'Alice', 'Acme', 'Bob', 'Carol', 'Dana', 'Eve'}
    assert {node['kind'] for node in nodes.values()} == {'Person', 'Organization'}
    assert len(graph['edges']) == 3
    for edge in graph['edges']:
        names = {nodes[k]['title'] for k in (edge['source'], edge['target'])}
        assert edge['evidence_count'] == len(edge['evidence']) == 1
        item = edge['evidence'][0]
        assert item['source_path'].startswith('/articles/article-one?revision=1#m-')
        assert item['event_path'] in {'/events/one', '/events/second-group'}
        if edge['kind'] == 'represents':
            assert names == {'Alice', 'Acme'} and edge['directed']
        else:
            assert names == {'Alice', 'Bob'} and edge['kind'] == 'joint_participation'
            assert not edge['directed']
    plan = next(e for e in graph['edges'] if e['status'] == 'planned')
    assert '拟议' in plan['label']
    alice = next(n for n in nodes.values() if n['title'] == 'Alice')
    assert alice['activity_count'] == len(alice['activities']) == 3
    assert 'people_until=' + UNTIL in alice['url']
    source = next(a for a in alice['activities'] if a['source_path'].endswith('#m-meeting'))
    assert source['role_label'] == '行动者、发言者'
    assert source['time_kind'] == 'published'


def test_window_empty_reasons_and_crowded_people_list(people_db):
    with Session(people_db) as session:
        focus(session)
        focus(session, 'empty')
        article, version = add(session, 'crowd', person='Untyped', typed=False)
        session.commit()
        no_evidence = people_graph(session, 'watched', until=UNTIL)
        assert no_evidence['state'] == 'empty' and no_evidence['window']['sampled_events'] == 1
        assert '人物动作' in no_evidence['empty_reason']
        no_events = people_graph(session, 'empty', until=UNTIL)
        assert no_events['state'] == 'empty' and no_events['window']['sampled_events'] == 0
        assert no_events['empty_reason'] != no_evidence['empty_reason']
        guests = ['Guest' + str(i) for i in range(MAX_JOINT_PEOPLE + 1)]
        mention(session, article, version, 'crowd', ', '.join(guests), [(n, 'participant') for n in guests])
        session.commit()
    graph = read(people_db)
    assert graph['window']['crowded_actions'] == 1 and graph['truncated']
    assert len(graph['nodes']) == len(guests) and not graph['edges']
    assert all(n['activity_count'] == len(n['activities']) == 1 for n in graph['nodes'])
    assert len(read(people_db, limit=2)['nodes']) == 2
    with pytest.raises(ValueError):
        read(people_db, limit=True)


def test_preview_counts_stable_and_include_literal_span(people_db):
    with Session(people_db) as session:
        focus(session)
        for i in range(7):
            name = f'report-{i}'
            add(session, name, action_date=f'2026-11-{i + 1:02d}')
        latest = session.get(TextMention, 'mention-report-6')
        row = json.loads(latest.data)
        row['quote'] = ('A previous sentence. ' * 50) + row['quote']
        latest.data = json.dumps(row)
        session.add(latest)
        session.commit()
    first = read(people_db, limit=2)
    assert first == read(people_db, limit=2)
    edge, = first['edges']
    assert edge['evidence_count'] == 7 and len(edge['evidence']) == 3
    assert [row['time'] for row in edge['evidence']] == ['2026-11-07', '2026-11-06', '2026-11-05']
    assert edge['evidence'][0]['evidence'].startswith('…')
    assert 'Alice represents Acme' in edge['evidence'][0]['evidence']
    node = next(node for node in first['nodes'] if node['kind'] == 'Person')
    assert node['activity_count'] == 7 and len(node['activities']) == 5
