import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.events import workbench
from news2douyin.events.product_evidence import card, for_product
from news2douyin.server import events, interop
from news2douyin.storage.articles import latest_version, record_version
from news2douyin.storage.models import Article, ArticleEventLink, Event, EventMoment, EventProductEvidence
from news2douyin.tasks.control import TaskConflict


@pytest.fixture
def news(engine):
    app = FastAPI()
    events.register_event_routes(app, engine)
    interop.register(app, engine)
    with Session(engine) as session:
        for name in ['mine', 'port']:
            session.add(Event(event_key=name, event_title=name.title()))
        for name, event, raw in [('a', 'mine', {}), ('b', 'mine', {}), ('c', 'port', {}),
                                 ('lead', 'mine', {'content_kind': 'summary'})]:
            article = Article(article_key=name, title='Market update', url='https://example.test/' + name,
                              content='The copper mine resumed operation.', raw_json=json.dumps(raw))
            session.add(article)
            record_version(session, article)
            session.add(ArticleEventLink(event_key=event, article_key=name))
        session.commit()
    return TestClient(app)


def payload(engine, key='a', **overrides):
    with Session(engine) as session:
        v = latest_version(session, key)
        return {'article_key': key, 'revision': v.revision, 'content_hash': v.content_hash,
                'excerpt': 'copper mine resumed', 'product_id': 'copper', 'expected_version': 0, **overrides}


def test_reviewed_citation_requires_original_version_and_is_revocable(engine, news, monkeypatch):
    url = '/api/events/mine/product-evidence'
    for invalid in (payload(engine, 'lead'), payload(engine, 'c'), payload(engine, excerpt='invented'),
                    payload(engine, revision=88), payload(engine, product_id='Copper ;DROP'),
                    payload(engine, excerpt='Market update')):
        assert news.post(url, json=invalid).status_code == 422
    assert news.post(url, json=payload(engine, content_hash='other')).status_code == 409
    assert news.post(url, json=payload(engine), headers={'Origin': 'http://example.org'}).status_code == 403
    response = news.post(url, json=payload(engine))
    assert response.status_code == 200, response.text
    body = response.json()
    ref = body['items'][0]
    assert body['version'] == 1 and body['time_basis'] == 'latest' and body['occurred_on'] is None
    assert ref['published_at'] is None and ref['observed_at'] and ref['source_url'].endswith('/a')
    assert ref['article_path'] == '/articles/a?revision=1'
    assert news.post(url, json=payload(engine)).status_code == 409
    assert news.post(url, json=payload(engine, expected_version=1)).json()['version'] == 1

    with Session(engine) as session:
        a = session.exec(select(Article).where(Article.article_key == 'a')).one()
        a.content = 'Corrected document.'
        session.add(a)
        record_version(session, a)
        session.commit()
        got = workbench.export_evidence(session, 'mine')
        assert got['product_evidence'][0]['revision'] == 1
        assert any(v['article_key'] == 'a' and v['revision'] == 1 for v in got['source_versions'])
        with pytest.raises(TaskConflict):
            workbench.remove_source(session, 'mine', 'a', 1)
    assert news.get(url).json()['items'][0] == ref

    reverse = '/api/v1/interop/product-evidence'
    monkeypatch.setenv('NEWS2DOUYIN_INTEROP_API_KEY', 'fixture-key')
    assert news.get(reverse, params={'product_id':'copper'}).status_code == 401
    response = news.get(reverse, params={'product_id':'copper'}, headers={'X-API-Key':'fixture-key'})
    assert response.json()['data']['items'][0]['event_path'] == '/events/mine'
    for path, params in [(reverse, {'product_id': 'copper'}), (url, {})]:
        assert news.get(path, params=dict(params, as_of='2020-01-01'), headers={'X-API-Key':'fixture-key'}).status_code == 422
    result = news.request('DELETE', url + '/' + str(ref['id']), json={'expected_version': 1})
    assert result.json()['items'] == [] and result.json()['version'] == 2
    with Session(engine) as session:
        assert session.get(EventProductEvidence, ref['id']).revoked_at
        assert not for_product(session, 'copper')['items']
        assert workbench.remove_source(session, 'mine', 'a', 2)


def test_product_links_follow_verified_material_on_split_and_merge(engine, news):
    for i, key in enumerate(['a','b']):
        assert news.post('/api/events/mine/product-evidence', json=payload(engine,key,expected_version=i)).status_code == 200
    with Session(engine) as session:
        rows = for_product(session, 'copper', limit=1)
        tail = for_product(session, 'copper', limit=1, before_id=rows['next_before_id'])
        assert [rows['items'][0]['article_key'], tail['items'][0]['article_key']] == ['b','a']
        assert tail['next_before_id'] is None
        new_key = workbench.split(session,'mine',dict(expected_version=2,article_keys=['b'],
                                                      title='Smelter',note='Independent'))['target_event_key']
        assert [row['article_key'] for row in card(session,new_key)['items']] == ['b']
        assert [row['article_key'] for row in card(session,'mine')['items']] == ['a']
        workbench.merge(session,new_key,dict(target_event_key='port',expected_version=card(session,new_key)['version'],
                                             target_expected_version=0,note='Equivalent'))
        assert card(session,new_key)['items'] == []
        assert [row['article_key'] for row in card(session,'port')['items']] == ['b']
        session.add(EventMoment(moment_key='m',event_key='port',title='date',time_kind='occurred',date_start='2020-01-10',
                                reviewed=True,certainty='exact',sources_json='[{"article_key":"b"}]'))
        session.commit()
        assert card(session,'port')['occurred_on'] == '2020-01-10'


def test_revision_provenance_distinguishes_original_from_discovery():
    from news2douyin.research.materials import original_body

    source = {'provider':'gkg','content':'A mine closed.'}
    assert original_body(source) == ''
    assert original_body(dict(source, raw={'metadata': {'content_kind': 'text'}})) == source['content']
    assert original_body(dict(source, raw={'content_kind': 'text','material_kind':'discovery_metadata'})) == ''
    assert original_body(dict(source, provider='future-provider', raw={'metadata': {'content_kind': 'summary'}})) == ''
    assert original_body(dict(source, provider='worldnewsapi', raw={'research_meta': {'content_truncated': True}})) == ''
