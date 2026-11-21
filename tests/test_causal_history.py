"""The archive query treats GDELT as discovery and confirms publisher dates."""
from datetime import date
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from news2douyin.causal import history


URL = 'https://publisher.example/defence-talks'
TITLE = 'Security guarantee negotiations resumed'
QUOTE = 'Delegates discussed security guarantees and troop deployments during the meeting.'
LOWER = '1989-01-01T00:00:00'
UPPER = '2022-02-23T16:00:00'


def _page(markup='', quote=QUOTE):
    return f'<html><head><title>{TITLE}</title>{markup}</head><body><article><p>{quote}</p></article></body></html>'.encode()


def _metadata(value):
    return f'<meta property="article:published_time" content="{value}">'


def _ld_date(value, *, title=TITLE, category='NewsArticle'):
    item = {'@type': category, 'headline': title, 'datePublished': value}
    return '<script type="application/ld+json">' + json.dumps({'@graph': [item]}) + '</script>'


def _enable(monkeypatch):
    monkeypatch.setenv(history.PROJECT_ENV, 'gdelt-test-project')
    monkeypatch.delenv(history.DAYS_ENV, raising=False)
    monkeypatch.delenv(history.BYTES_ENV, raising=False)
    monkeypatch.setenv('NEWS2DOUYIN_TIMEZONE', 'Asia/Shanghai')


def test_history_window_is_bounded_and_publisher_date_is_strictly_before_beijing_root_day(monkeypatch):
    _enable(monkeypatch)
    late = 'https://publisher.example/late-update'
    missing = 'https://publisher.example/date-modified-only'
    dated = 'https://publisher.example/dated-report'
    calls = []

    def query(project, start, end, keywords, maximum):
        calls.append('query')
        assert project == 'gdelt-test-project'
        assert (start, end) == (date(2021, 11, 26), date(2022, 2, 24))
        assert 'russia' in keywords and 'defence' in keywords and 'ukraine' in keywords
        assert maximum == 12 * 1024 ** 3
        return ([dict(event_id=101, url=URL), dict(event_id=102, url=late),
                 dict(event_id=103, url=missing), dict(event_id=104, url=dated)], 123)

    def fetch(url, **options):
        calls.append(url)
        assert 0 < options['timeout_seconds'] <= history.FETCH_SECONDS
        assert options['max_bytes'] == history.MAX_DOCUMENT_BYTES
        headers = {
            URL: _metadata('2022-02-23T17:59:59+02:00'),
            late: _metadata('2022-02-24T00:00:00+08:00'),
            missing: '<meta name="date" content="2022-01-13"><meta property="article:modified_time" content="2022-02-23">',
            dated: _ld_date('2022-02-21'),
        }
        return url, 'text/html; charset=utf-8', _page(headers[url])

    monkeypatch.setattr(history, '_run_query', query)
    monkeypatch.setattr(history, 'safe_fetch', fetch)
    result = history.search_history(['俄罗斯、defence talks'], LOWER, UPPER, event_text='Ukraine security',
                                    checkpoint=lambda: calls.append('checkpoint'))
    assert calls == ['checkpoint', 'query', 'checkpoint', 'checkpoint', URL,
                     'checkpoint', late, 'checkpoint', missing, 'checkpoint', dated]
    assert result.diagnostics['status'] == 'verified'
    assert result.diagnostics['window_limited'] is True
    assert result.diagnostics['date_from'] == '2021-11-26'
    assert result.diagnostics['date_to'] == '2022-02-23'
    assert result.diagnostics['lead_count'] == result.diagnostics['attempted_count'] == 4
    assert result.diagnostics['bytes_billed'] == 123
    assert result.diagnostics['verified_count'] == 2
    assert result.diagnostics['outside_window_count'] == 1
    assert result.diagnostics['missing_publication_count'] == 1
    first, second = result.candidates
    assert first['occurred_at'] == ''
    assert first['published_at'] == '2022-02-23T15:59:59Z'
    assert second['published_at'] == '2022-02-21'
    assert first['sources'] == [dict(title=TITLE, quote=QUOTE, url=URL, published_at='2022-02-23T15:59:59Z',
                                     kind='publisher_history', gdelt_event_id='101')]


@pytest.mark.parametrize('markup,body,expected', [
    (_metadata('2022-01-01') + _ld_date('2022-02-02'), QUOTE, 'missing_publication_count'),
    (_ld_date('2022-01-02', title='An unrelated story'), QUOTE, 'missing_publication_count'),
    (_ld_date('2022-01-02', category='WebSite'), QUOTE, 'missing_publication_count'),
    (_metadata('2022-01-01') + '<meta name="description" content="Source description is not visible text">', '',
     'missing_excerpt_count'),
])
def test_unconfirmed_sources_are_not_promoted(monkeypatch, markup, body, expected):
    _enable(monkeypatch)
    monkeypatch.setattr(history, '_run_query', lambda *args: ([dict(event_id=1, url=URL)], 0))
    monkeypatch.setattr(history, 'safe_fetch', lambda url, **kw: (url, 'text/html', _page(markup, body)))
    result = history.search_history(['security talks'], LOWER, UPPER)
    assert result.candidates == []
    assert result.diagnostics['status'] == 'unverified'
    assert result.diagnostics[expected] == 1


def test_absence_of_project_old_window_and_cloud_error_are_distinct(monkeypatch):
    monkeypatch.delenv(history.PROJECT_ENV, raising=False)

    def forbidden(*args):
        pytest.fail('no cloud or publisher call should be made')

    monkeypatch.setattr(history, '_run_query', forbidden)
    monkeypatch.setattr(history, 'safe_fetch', forbidden)
    assert history.search_history(['talks'], LOWER, UPPER).diagnostics['status'] == 'unconfigured'
    _enable(monkeypatch)
    reports = history.search_history(['talks'], LOWER, '2014-01-01T00:00:00').diagnostics
    assert reports['status'] == 'out_of_range'
    monkeypatch.setenv(history.DAYS_ENV, '0')
    assert history.search_history(['talks'], LOWER, UPPER).diagnostics['error_code'] == 'invalid_configuration'
    monkeypatch.delenv(history.DAYS_ENV)
    monkeypatch.setattr(history, '_run_query', lambda *args: ([], 100))
    assert history.search_history(['talks'], LOWER, UPPER).diagnostics['status'] == 'no_results'

    class LeaseLost(Exception):
        pass

    def interrupted():
        raise LeaseLost()

    with pytest.raises(LeaseLost):
        history.search_history(['talks'], LOWER, UPPER, checkpoint=interrupted)


def test_cloud_job_uses_cost_cap_partitions_and_query_parameters(monkeypatch):
    captured = {}

    class Client:
        def __init__(self, **options):
            captured['client'] = options

        def query(self, sql, **options):
            captured.update(sql=sql, **options)
            return SimpleNamespace(result=lambda **kw: [{'event_id': 23, 'url': URL}],
                                   total_bytes_billed=123)

        def close(self):
            captured['closed'] = True

    google = ModuleType('google')
    cloud = ModuleType('google.cloud')
    cloud.bigquery = SimpleNamespace(Client=Client, QueryJobConfig=SimpleNamespace,
        ScalarQueryParameter=lambda *args: args, ArrayQueryParameter=lambda *args: args)
    google.cloud = cloud
    monkeypatch.setitem(sys.modules, 'google', google)
    monkeypatch.setitem(sys.modules, 'google.cloud', cloud)
    start, end = date(2022, 1, 1), date(2022, 2, 24)
    keyword = "don't quote this in SQL"
    rows, billed = history._run_query('test-project', start, end, [keyword], 12_345)
    assert rows == [dict(event_id=23, url=URL)] and billed == 123
    assert captured['closed'] and captured['client'] == dict(project='test-project', location='US')
    statement, config = captured['sql'], captured['job_config']
    assert 'gdelt-bq.gdeltv2.events_partitioned' in statement
    assert 'gdelt-bq.gdeltv2.eventmentions_partitioned' in statement
    assert statement.count('_PARTITIONTIME >= TIMESTAMP(@date_from)') == 2
    assert statement.count('_PARTITIONTIME < TIMESTAMP(@date_end)') == 2
    assert config.maximum_bytes_billed == 12_345 and config.job_timeout_ms == history.QUERY_SECONDS * 1000
    assert keyword not in statement and ('keywords', 'STRING', [keyword]) in config.query_parameters
    assert ('event_from', 'INT64', 20220101) in config.query_parameters

    class CostError(Exception):
        errors = [{'reason': 'bytesBilledLimitExceeded'}]

    def expensive_query(*args, **kwargs):
        raise CostError('private account information should not propagate')

    monkeypatch.setattr(Client, 'query', expensive_query)
    with pytest.raises(history._HistoryError, match='^bytes_limit$'):
        history._run_query('test-project', start, end, [keyword], 12_345)
