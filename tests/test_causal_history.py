"""The archive query treats GDELT as discovery and confirms publisher dates."""
from datetime import date
import json
from datetime import datetime, timezone

import pytest

from news2douyin.causal import history
from news2douyin.history_archive.search import ArchiveLeads


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
    monkeypatch.delenv(history.DAYS_ENV, raising=False)
    monkeypatch.setenv('NEWS2DOUYIN_TIMEZONE', 'Asia/Shanghai')


def test_history_window_is_bounded_and_publisher_date_is_strictly_before_beijing_root_day(monkeypatch):
    _enable(monkeypatch)
    late = 'https://publisher.example/late-update'
    missing = 'https://publisher.example/date-modified-only'
    dated = 'https://publisher.example/dated-report'
    calls = []

    def query(engine, root, start, end, keywords, maximum, *, cutoff):
        calls.append('query')
        assert engine == 'db' and root == 'storage'
        assert (start, end) == (date(2021, 11, 26), date(2022, 2, 24))
        assert cutoff == datetime(2022, 2, 23, 16, tzinfo=timezone.utc)
        assert 'russia' in keywords and 'defence' in keywords and 'ukraine' in keywords
        assert maximum == history.MAX_LEADS
        return ArchiveLeads([dict(event_id=101, url=URL, version=2, source_kind='events'),
                             dict(event_id=102, url=late),
                             dict(event_id=103, url=missing), dict(event_id=104, url=dated)], {})

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

    monkeypatch.setattr(history, 'find_leads', query)
    monkeypatch.setattr(history, 'safe_fetch', fetch)
    result = history.search_history(['俄罗斯、defence talks'], LOWER, UPPER, event_text='Ukraine security',
                                    checkpoint=lambda: calls.append('checkpoint'),
                                    engine='db', storage_root='storage')
    assert calls == ['checkpoint', 'query', 'checkpoint', 'checkpoint', URL,
                     'checkpoint', late, 'checkpoint', missing, 'checkpoint', dated]
    assert result.diagnostics['status'] == 'verified'
    assert result.diagnostics['window_limited'] is True
    assert result.diagnostics['date_from'] == '2021-11-26'
    assert result.diagnostics['date_to'] == '2022-02-23'
    assert result.diagnostics['lead_count'] == result.diagnostics['attempted_count'] == 4
    assert result.diagnostics['provider'] == 'gdelt_archive'
    assert result.diagnostics['verified_count'] == 2
    assert result.diagnostics['outside_window_count'] == 1
    assert result.diagnostics['missing_publication_count'] == 1
    first, second = result.candidates
    assert first['occurred_at'] == ''
    assert first['published_at'] == '2022-02-23T15:59:59Z'
    assert second['published_at'] == '2022-02-21'
    assert first['sources'] == [dict(title=TITLE, quote=QUOTE, url=URL, published_at='2022-02-23T15:59:59Z',
                                     kind='publisher_history', gdelt_event_id='101',
                                     gdelt_version=2, archive_source_type='events')]


@pytest.mark.parametrize('markup,body,expected', [
    (_metadata('2022-01-01') + _ld_date('2022-02-02'), QUOTE, 'missing_publication_count'),
    (_ld_date('2022-01-02', title='An unrelated story'), QUOTE, 'missing_publication_count'),
    (_ld_date('2022-01-02', category='WebSite'), QUOTE, 'missing_publication_count'),
    (_metadata('2022-01-01') + '<meta name="description" content="Source description is not visible text">', '',
     'missing_excerpt_count'),
])
def test_unconfirmed_sources_are_not_promoted(monkeypatch, markup, body, expected):
    _enable(monkeypatch)
    monkeypatch.setattr(history, 'find_leads', lambda *args, **kwargs: ArchiveLeads([dict(event_id=1, url=URL)], {}))
    monkeypatch.setattr(history, 'safe_fetch', lambda url, **kw: (url, 'text/html', _page(markup, body)))
    result = history.search_history(['security talks'], LOWER, UPPER, engine='db', storage_root='storage')
    assert result.candidates == []
    assert result.diagnostics['status'] == 'unverified'
    assert result.diagnostics[expected] == 1



def test_unconfigured_archive_gap_no_results_and_query_error_are_distinct(monkeypatch):
    def lookup(upper=UPPER, **kwargs):
        return history.search_history(['talks'], LOWER, upper, engine='db', storage_root='storage', **kwargs).diagnostics

    def forbidden(*args, **kwargs):
        pytest.fail('unexpected archive or publisher call')

    monkeypatch.setattr(history, 'find_leads', forbidden)
    monkeypatch.setattr(history, 'safe_fetch', forbidden)
    assert history.search_history(['talks'], LOWER, UPPER).diagnostics['status'] == 'unconfigured'
    _enable(monkeypatch)
    assert lookup('1978-01-01T00:00:00')['status'] == 'out_of_range'
    monkeypatch.setenv(history.DAYS_ENV, '0')
    assert lookup()['error_code'] == 'invalid_configuration'
    monkeypatch.delenv(history.DAYS_ENV)
    monkeypatch.setattr(history, 'find_leads', lambda *args, **kwargs: ArchiveLeads([], {
        'events': {'state': 'covered'}, 'gkg': {'state': 'partial'},
    }))
    assert lookup()['status'] == 'archive_gap'
    assert lookup()['coverage_incomplete']
    monkeypatch.setattr(history, 'find_leads', lambda *args, **kwargs: ArchiveLeads([], {
        'events': {'state': 'covered'}, 'gkg': {'state': 'covered'}, 'mentions': {'state': 'gap'},
    }))
    assert lookup()['status'] == 'no_results'

    def disk_error(*args, **kwargs):
        raise OSError('/local/path/not/exposed')

    monkeypatch.setattr(history, 'find_leads', disk_error)
    assert lookup()['error_code'] == 'archive_query_failed'

    class LeaseLost(Exception):
        pass

    def interrupted():
        raise LeaseLost()

    with pytest.raises(LeaseLost):
        lookup(checkpoint=interrupted)
