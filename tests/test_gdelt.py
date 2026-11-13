"""DOC API contract and normal collection workflow, offline and deterministic."""
from unittest.mock import Mock

import pytest
import requests
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from news2douyin.collect.providers import gdelt
from news2douyin.collect import service as collect
from news2douyin.server.app import create_app
from news2douyin.server.readiness import configuration_report
from news2douyin.storage.models import Article, ArticleVersion, GdeltDiscovery


LINK = 'https://example.org/warsaw-story'
REPORT = {'url': LINK, 'title': 'Warsaw transport vote', 'language': 'Polish',
          'sourcecountry': 'Poland', 'seendate': '20241027T060000Z', 'domain': 'example.org'}
DAY = {'date_str': '2024-10-27', 'timezone': 'Europe/Warsaw'}
CONFIG = {'search_query': 'Warsaw', **DAY}


def response(payload=None, status=200):
    return Mock(status_code=status, json=Mock(return_value=payload))


def test_request_translates_codes_and_local_dst_day(monkeypatch):
    get = Mock(return_value=response({'articles': [REPORT]}))
    monkeypatch.setattr(gdelt.requests, 'get', get)
    item, = gdelt.fetch_news(dict(CONFIG, country='DE', language='pl', max_items=20))
    assert get.call_args.args == (gdelt.ENDPOINT,)
    assert get.call_args.kwargs['params'] == dict(query='Warsaw sourcecountry:gm sourcelang:polish',
        mode='ArtList', format='json', sort='DateDesc', maxrecords=20,
        startdatetime='20241026220000', enddatetime='20241027225959')
    assert item['country'] == 'pl' and item['language'] == 'pl'
    assert item['gdelt']['seen_at'] == '2024-10-27T06:00:00Z'
    assert item['published_at'] == item['content'] == ''
    assert item['source']['domain'] == 'example.org'
    assert gdelt.query_for(CONFIG) == 'Warsaw'
    assert gdelt.query_for(dict(CONFIG, country='CH')) == 'Warsaw sourcecountry:sz'
    assert gdelt.query_for(dict(CONFIG, country='CN')) == 'Warsaw sourcecountry:ch'


def test_timeout_settings_are_applied_without_source_patch(monkeypatch):
    monkeypatch.setenv('NEWS2DOUYIN_GDELT_CONNECT_TIMEOUT','30')
    monkeypatch.setenv('NEWS2DOUYIN_GDELT_READ_TIMEOUT','90')
    get=Mock(return_value=response({'articles':[]}));monkeypatch.setattr(gdelt.requests,'get',get)
    gdelt.fetch_news(CONFIG)
    assert get.call_args.kwargs['timeout']==(30,90)
    monkeypatch.setenv('NEWS2DOUYIN_GDELT_READ_TIMEOUT','120')
    assert gdelt.request_timeouts()==(30,120)
    get.reset_mock();monkeypatch.setenv('NEWS2DOUYIN_GDELT_CONNECT_TIMEOUT','-1')
    with pytest.raises(ValueError,match='CONNECT_TIMEOUT'):gdelt.fetch_news(CONFIG)
    get.assert_not_called()


@pytest.mark.parametrize('options', [{'collection_mode':'top'}, {'country':'zz'}, {'language':'zz'},
                                    {'max_items':251}, {'max_items':0}, {'search_query':'ab'}])
def test_invalid_options_rejected_before_network(monkeypatch, options):
    monkeypatch.setattr(gdelt.requests, 'get', lambda *a, **kw: pytest.fail('unexpected request'))
    with pytest.raises(ValueError, match='GDELT'):
        gdelt.fetch_news(dict(CONFIG, **options))


def test_error_and_empty_are_distinct(monkeypatch):
    bad_text = response()
    bad_text.json.side_effect = ValueError('not JSON')
    bad_text.text = 'Your query was too short'
    get = Mock(side_effect=[response({'articles': []}), response({}), response(status=429),
        response(status=503), bad_text, response({'error':'broken'}), requests.Timeout(),
        response({'articles':[dict(REPORT, url='javascript:malformed')]})])
    monkeypatch.setattr(gdelt.requests, 'get', get)
    assert gdelt.fetch_news(CONFIG) == gdelt.fetch_news(CONFIG) == []
    for message in ['429','503','query was too short','列表','超时','链接']:
        with pytest.raises(RuntimeError, match=message):
            gdelt.fetch_news(CONFIG)


def test_upstream_date_window_is_checked(monkeypatch):
    wrong_day = dict(REPORT, seendate='20241025T120000Z')
    get = Mock(side_effect=[response({'articles':[wrong_day, REPORT]}), response({'articles':[wrong_day]})])
    monkeypatch.setattr(gdelt.requests, 'get', get)
    assert len(gdelt.fetch_news(CONFIG)) == 1
    with pytest.raises(RuntimeError, match='收录日期'):
        gdelt.fetch_news(CONFIG)


@pytest.mark.parametrize('rich_first', [True, False])
def test_trial_and_collect_from_both_sources(tmp_path, monkeypatch, rich_first):
    app = create_app(db_url=f'sqlite:///{tmp_path}/news.db', storage_root=str(tmp_path/'runs'))
    client = TestClient(app)
    monkeypatch.setattr(gdelt.requests, 'get', Mock(return_value=response({'articles':[REPORT]})))
    monkeypatch.setitem(collect.PROVIDERS, 'worldnewsapi', lambda config: [
        {'title':'Full Warsaw coverage', 'url':LINK+'?utm_source=rss', 'content':'Verified full article text.',
         'source':{'domain':'example.org'}, 'country':'pl', 'language':'pl',
         'published_at':'2024-10-26T07:00:00Z'}])
    profile = {'name':'Warsaw', 'provider':'gdelt', 'extra':{'search_query':'Warsaw'}, 'max_items':250}
    saved = client.post('/api/profiles', json=profile).json()
    assert (saved['country'],saved['language'],saved['extra']['collection_mode']) == ('','','search')
    assert saved['extra']['filter_mode'] == 'rules'
    assert next(p for p in configuration_report(app)['profiles'] if p['name']=='Warsaw')['state'] == 'configured'
    for changes in [{'max_items':251},{'extra':{'collection_mode':'top','search_query':'Warsaw'}},{'country':'zz'}]:
        assert client.post('/api/profiles', json=dict(profile, **changes)).status_code == 422
    assert client.post('/api/profiles', json={'name':'rich','provider':'worldnewsapi','extra':{'filter_mode':'rules'}}).status_code == 200
    assert '全球媒体' in client.get('/profiles?selected=Warsaw').text

    def finish(result):
        assert result.status_code == 202, result.text
        task = result.json()['task_id']
        app.state.worker.execute(app.state.tasks.claim())
        data = client.get(f'/api/tasks/{task}/diagnostics').json()
        assert data['task']['status'] == 'succeeded', data
        return task, data['stats']

    task, stats = finish(client.post('/api/profiles/Warsaw/test', json=DAY))
    assert stats['dry_run'] and stats['after_filter'] == 1
    assert LINK in client.get(f'/tasks/{task}/diagnostics').text
    with Session(app.state.engine) as db:
        assert not db.exec(select(Article)).all() and not db.exec(select(GdeltDiscovery)).all()
    order = ['rich','Warsaw'] if rich_first else ['Warsaw','rich']
    for index, name in enumerate(order+['Warsaw']):
        task, _ = finish(client.post('/api/tasks/collect', json={'profile_name':name, 'override':DAY}))
        if not rich_first and index == 0:
            assert '报道线索' in client.get('/daily?task_id='+task).text
    with Session(app.state.engine) as db:
        article, = db.exec(select(Article)).all()
        assert (article.provider, article.title, article.content, article.published_at) == (
            'worldnewsapi','Full Warsaw coverage','Verified full article text.','2024-10-26T07:00:00Z')
        discovery, = db.exec(select(GdeltDiscovery)).all()
        assert discovery.article_key == article.article_key and discovery.seen_at == '2024-10-27T06:00:00Z'
        assert len(db.exec(select(ArticleVersion)).all()) == (1 if rich_first else 2)
        html = client.get('/articles/'+article.article_key).text
        assert 'GDELT 收录记录' in html and 'Verified full article text.' in html
        if not rich_first:
            assert '报道线索' in client.get('/articles/'+article.article_key+'?revision=1').text
    app.state.engine.dispose()
