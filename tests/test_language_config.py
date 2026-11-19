from types import SimpleNamespace
import sys

import pytest

from news2douyin.collect.providers import gdelt, search_news
from news2douyin.ingest.worldnewsapi import fetch_top_news
from news2douyin.search.schemas import ResearchQuery
from news2douyin.server.schemas.common import ProfilePayload
from news2douyin.translations import language


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / 'languages.toml'
    path.write_text('''
[labels]
"sr-latn-rs" = "塞尔维亚语（拉丁字母）"
bs = "波斯尼亚语"

[providers.worldnewsapi.aliases]
"Serbian (Latin)" = "sr-Latn-RS"

[providers.worldnewsapi.query]
"sr-latn-rs" = "sr"

[providers.gdelt.aliases]
Bosnian = "bs"

[providers.gdelt.query]
"bs-latn" = "Bosnian"
''', encoding='utf-8')
    monkeypatch.setenv(language.LANGUAGE_CONFIG_ENV, str(path))
    return path


def test_language_normalization_preserves_provider_declaration(registry):
    assert language.normalize_language('  ES_419 ') == 'es-419'
    assert language.normalize_language('FRE') == 'fr'
    assert language.normalize_language('sr-Latn-RS') == 'sr-latn-rs'
    assert language.normalize_language('en-US-u-ca-gregory') == 'en-us-u-ca-gregory'
    assert language.normalize_language('Serbian (Latin)') == ''
    value = '  Serbian (Latin)  '
    expected = {'language': 'sr-latn-rs', 'source_language': value}
    assert language.language_metadata(value, provider='worldnewsapi') == expected
    assert language.describe_language(declared=value, provider='worldnewsapi')['label'] == '塞尔维亚语（拉丁字母）'
    assert language.describe_language('No declaration')['basis'] == 'unknown'
    assert language.describe_language('No declaration', 'Und')['code'] == 'unknown'
    assert language.language_metadata('und') == {'language': '', 'source_language': 'und'}
    assert ResearchQuery(query='lithium', language='es_419').language == 'es-419'
    with pytest.raises(ValueError, match='请输入有效语种代码'):
        ResearchQuery(query='lithium', language='not/a/tag')


def test_language_file_reloaded_after_change(registry, monkeypatch):
    check = [30]
    monkeypatch.setattr(language, 'time', SimpleNamespace(monotonic=lambda: check[0]))
    assert language.describe_language(declared='bs')['label'] == '波斯尼亚语'
    registry.write_text('[labels]\nbs = "波斯尼亚语（当地）"\n', encoding='utf-8')
    check[0] += 1
    assert language.describe_language(declared='bs')['label'] == '波斯尼亚语（当地）'


def test_profile_and_gdelt_options_use_language_configuration(registry):
    profile = ProfilePayload.model_validate({
        'name': '试采',
        'provider': 'worldnewsapi',
        'language': 'Serbian (Latin)',
        'extra': {'collection_mode': 'search', 'search_query': 'lithium'},
    })
    assert profile.language == 'sr-latn-rs'
    assert gdelt.profile_codes()['languages']['bs-latn'] == 'Bosnian'
    assert gdelt.query_for({'search_query': 'lithium', 'language': 'bs-Latn'}) == 'lithium sourcelang:bosnian'
    assert gdelt.query_for({'search_query': 'lithium', 'language': 'FRE'}) == 'lithium sourcelang:french'
    rec = gdelt._article({'url':'https://example.test/1','title':'Example title','language':'Bosnian'}, '2026-01-01T00:00:00Z')
    assert rec['language'] == 'bs'
    assert rec['source_language'] == 'Bosnian'
    with pytest.raises(ValueError, match='不支持'):
        gdelt.query_for({'search_query': 'lithium', 'language': 'pt-BR'})


def test_worldnewsapi_search_requests_configured_tag_and_preserves_original(registry, monkeypatch):
    monkeypatch.setenv('API_KEY', 'offline-key')
    seen = {}

    def get(url, *, params, **kwargs):
        seen.update(params)
        news = [
            {'url':'https://example.test/1','title':'Example article','language':'Serbian (Latin)','text':'x'*50001},
            {'url':'https://example.test/2','title':'Upstream does not know','language':'und','summary':'Brief report'},
            {'url':'https://example.test/3','title':'Upstream omitted the field'},
        ]
        return SimpleNamespace(status_code=200, json=lambda: {'news': news})

    monkeypatch.setattr(search_news.requests, 'get', get)
    items = search_news.search_news({'query':'lithium','language':'sr-Latn-RS'})['items']
    assert seen['language'] == 'sr'
    assert [(item['language'], item['source_language']) for item in items] == [
        ('sr-latn-rs', 'Serbian (Latin)'), ('', 'und'), ('sr', ''),
    ]
    assert [item['research_meta']['content_kind'] for item in items] == ['excerpt', 'summary', 'metadata']


def test_worldnewsapi_top_requests_configured_tag_and_preserves_original(registry, monkeypatch):
    monkeypatch.setenv('API_KEY', 'offline-key')
    seen = {}

    def top_news(**kwargs):
        seen.update(kwargs)
        return {'top_news': [{'news': [
            {'url':'https://example.test/1','title':'Example article','language':'Serbian (Latin)','text':'x'*6001},
            {'url':'https://example.test/2','title':'Upstream omitted the field','summary':'Brief report'},
        ]}]}

    adapter = SimpleNamespace(
        Configuration=lambda **kwargs: SimpleNamespace(api_key={}),
        ApiClient=lambda config: config,
        NewsApi=lambda api_client: SimpleNamespace(top_news=top_news),
    )
    monkeypatch.setitem(sys.modules, 'worldnewsapi', adapter)
    items = fetch_top_news({'country':'rs','language':'sr-Latn-RS','cache_enabled':False})
    assert seen['language'] == 'sr'
    assert [(item['language'], item['source_language']) for item in items] == [
        ('sr-latn-rs', 'Serbian (Latin)'), ('sr', ''),
    ]
    assert [item['content_kind'] for item in items] == ['excerpt', 'summary']
