"""NewsNow transport and time semantics without a live intranet dependency."""
import base64
import hashlib
import json

import pytest
import requests

from news2douyin.collect.providers import newsnow


STAMP=1794638758000


class Response:
    def __init__(self,payload=None,*,body=None,status=200,mime='application/json'):
        self.body=body if body is not None else json.dumps(payload,ensure_ascii=False).encode('utf-8')
        self.status_code=status
        self.headers={'content-type':mime,'content-length':str(len(self.body))}
        self.closed=False
    def iter_content(self,chunk_size):
        for offset in range(0,len(self.body),chunk_size):yield self.body[offset:offset+chunk_size]
    def close(self):self.closed=True


def payload(source='zhihu',items=None,**changes):
    result=dict(id=source,status='cache',updatedTime=STAMP,items=items if items is not None else [
        dict(id='one',title='一个具体热点',url='https://source.example/report',extra={'hover':'这是摘要','info':'3761 万热度'})])
    result.update(changes);return result


def configure(monkeypatch,responses):
    calls=[]
    def get(url,**kwargs):
        calls.append((url,kwargs))
        value=responses[kwargs['params']['id']]
        if isinstance(value,Exception):raise value
        return value
    monkeypatch.setattr(newsnow.requests,'get',get)
    return calls


def test_raw_snapshot_and_discovery_metadata_are_preserved_without_fabricated_publication(monkeypatch):
    raw=Response(payload())
    calls=configure(monkeypatch,{'zhihu':raw})
    batch=newsnow.fetch_batch(dict(base_url='http://newsnow.example/',newsnow_sources=['zhihu']))
    item=batch['items'][0];meta=item['newsnow'];snapshot=batch['snapshots'][0]
    assert item['published_at']==''
    assert item['content']=='这是摘要' and item['content_kind']=='source_excerpt'
    assert item['material_kind']=='newsnow_discovery' and item['requires_original_text']
    assert 'language' not in item and 'country' not in item
    assert meta['observed_list_position']==1 and meta['position_kind']=='returned_list_position'
    assert meta['heat_text']=='3761 万热度' and meta['publication_precision']=='unknown'
    assert meta['snapshot_reported_at']=='2026-11-14T06:45:58Z'
    assert snapshot['payload']==payload()
    assert base64.b64decode(snapshot['raw_body_b64'])==raw.body
    assert snapshot['raw_sha256']==hashlib.sha256(raw.body).hexdigest()
    assert snapshot['payload_sha256']==hashlib.sha256(newsnow._canonical_bytes(payload())).hexdigest()
    assert raw.closed and calls[0][0]=='http://newsnow.example/api/s'
    assert calls[0][1]['allow_redirects'] is False
    assert batch['summary']['status']=='complete'


@pytest.mark.parametrize('value,expected,precision,assumed',[
    (STAMP,'2026-11-14T06:45:58Z','second',False),
    (1794638758,'2026-11-14T06:45:58Z','second',False),
    ('1794638758000','2026-11-14T06:45:58Z','second',False),
    ('Sat, 14 Nov 2026 01:14:47 +0800','2026-11-13T17:14:47Z','second',False),
    ('Sat, 14 Nov 2026 06:13:14 GMT','2026-11-14T06:13:14Z','second',False),
    ('2026-11-13T21:06:40-08:00','2026-11-14T05:06:40Z','second',False),
    ('2026-11-14 14:24:04','2026-11-14T06:24:04Z','second',True),
    ('2026-11-14','2026-11-13T16:00:00Z','date',True),
    ('2026-11-5','2026-11-04T16:00:00Z','date',True),
])
def test_time_formats_preserve_precision_and_explicit_timezone(value,expected,precision,assumed):
    assert newsnow.parse_time(value)==dict(value=expected,precision=precision,timezone_assumed=assumed)


@pytest.mark.parametrize('value',[None,'',True,False,0,float('nan'),float('inf'),-1,10**1000,'2026-13-14','2026-02-30','unknown',{},[]])
def test_invalid_times_remain_unknown(value):
    assert newsnow.parse_time(value)['value']==''


def test_timestamp_priority_falls_through_invalid_fields_but_never_snapshot_time(monkeypatch):
    items=[dict(id='a',title='快讯',url='https://wire.example/1',pubDate='invalid',extra={'date':STAMP}),
           dict(id='b',title='未知时间',url='https://wire.example/2')]
    configure(monkeypatch,{'wallstreetcn':Response(payload('wallstreetcn',items))})
    first,second=newsnow.fetch_news(dict(newsnow_sources=['wallstreetcn']))
    assert first['published_at']=='2026-11-14T06:45:58Z'
    assert first['newsnow']['publication_time_field']=='extra.date'
    assert second['published_at']=='' and second['newsnow']['snapshot_reported_at']


def test_all_sources_are_requested_and_limit_is_round_robin_without_truncating_snapshots(monkeypatch):
    responses={source:Response(payload(source,[dict(id=str(i),title=source+str(i),url=f'https://{source}.example/{i}')
        for i in range(3)])) for source in ('alpha','bravo','charlie')}
    calls=configure(monkeypatch,responses)
    batch=newsnow.fetch_batch(dict(newsnow_sources=list(responses),max_items=4))
    assert [call[1]['params']['id'] for call in calls]==list(responses)
    assert [item['newsnow_source'] for item in batch['items']]==['alpha','bravo','charlie','alpha']
    assert batch['summary']['truncated'] and batch['summary']['valid_items']==9
    assert [len(snapshot['payload']['items']) for snapshot in batch['snapshots']]==[3,3,3]
    assert [row['selected_items'] for row in batch['sources']]==[2,1,1]


@pytest.mark.parametrize('response,code',[
    (Response(body=b'<html>SPA shell</html>',mime='text/html'),'not_json'),
    (Response(body=b'not JSON'),'invalid_json'),
    (Response(payload('other')),'source_mismatch'),
    (Response({'id':'zhihu','status':'cache','updatedTime':STAMP}),'invalid_payload'),
    (Response(payload(items=[],updatedTime=0)),'invalid_snapshot_time'),
    (Response(payload(status='unknown')),'invalid_status'),
    (Response(payload(items=['invalid'])),'invalid_items'),
    (Response(body=b'{"value": 1e999}'),'invalid_json'),
    (Response(body=b'{"value": NaN}'),'invalid_json'),
    (Response(body=b'private upstream error',status=500,mime='text/plain'),'http_error'),
])
def test_invalid_source_is_not_reported_as_success_and_raw_full_body_is_retained(monkeypatch,response,code):
    configure(monkeypatch,{'zhihu':response})
    with pytest.raises(newsnow.NewsNowUnavailable) as caught:newsnow.fetch_batch(dict(newsnow_sources=['zhihu']))
    batch=caught.value.batch
    assert batch['summary']['status']=='failed' and batch['items']==[]
    assert batch['sources'][0]['error_code']==code
    assert batch['sources'][0]['status']=='failed'
    assert 'private upstream error' not in batch['sources'][0]['error']
    assert base64.b64decode(batch['snapshots'][0]['raw_body_b64'])==response.body
    assert response.closed


def test_partial_failures_and_valid_empty_source_have_different_outcomes(monkeypatch):
    configure(monkeypatch,{'zhihu':requests.ConnectionError('secret URL password'),
        'empty':Response(payload('empty',[])),'valid':Response(payload('valid'))})
    batch=newsnow.fetch_batch(dict(newsnow_sources=['zhihu','empty','valid']))
    assert batch['summary']['status']=='partial'
    assert [row['status'] for row in batch['sources']]==['failed','empty','success']
    assert batch['summary']['succeeded_sources']==2 and batch['summary']['empty_sources']==1
    assert 'password' not in json.dumps(batch['sources'])
    empty=newsnow.fetch_batch(dict(newsnow_sources=['empty']))
    assert empty['summary']['status']=='empty' and not empty['items']


def test_non_news_stock_snapshot_is_retained_but_classified(monkeypatch):
    configure(monkeypatch,{'xueqiu':Response(payload('xueqiu',[dict(id='NVDA',title='英伟达',url='https://xueqiu.com/S/NVDA',extra={'info':'-1.2%'})]))})
    item=newsnow.fetch_news(dict(newsnow_sources=['xueqiu']))[0]
    assert item['newsnow']['source_type']=='non_news' and not item['newsnow']['is_news']
    assert item['newsnow']['raw_metrics']=={} and item['newsnow']['heat_text']=='-1.2%'


@pytest.mark.parametrize('source,canonical',list(newsnow.SOURCE_ALIASES.items()))
def test_only_known_parent_redirect_ids_are_allowed(monkeypatch,source,canonical):
    configure(monkeypatch,{source:Response(payload(canonical))})
    batch=newsnow.fetch_batch(dict(newsnow_sources=[source]))
    assert batch['sources'][0]['response_source_id']==canonical
    assert batch['items'][0]['newsnow']['response_source_id']==canonical
    assert batch['items'][0]['newsnow_source']==source


def test_total_raw_body_limit_marks_later_sources_unattempted(monkeypatch):
    first=Response(payload('alpha'));second=Response(payload('bravo'))
    calls=configure(monkeypatch,{'alpha':first,'bravo':second})
    monkeypatch.setattr(newsnow,'MAX_BATCH_RESPONSE_BYTES',len(first.body))
    batch=newsnow.fetch_batch(dict(newsnow_sources=['alpha','bravo']))
    assert len(calls)==1
    assert batch['sources'][1]['status']=='not_attempted'
    assert batch['sources'][1]['error_code']=='batch_size_exceeded'
    assert batch['summary']['status']=='partial'


def test_invalid_rows_are_retained_in_raw_snapshot_and_cannot_become_articles(monkeypatch):
    items=[dict(title='Unsafe',url='javascript:alert(1)'),dict(title='Bad',url='https://user:secret@example.com'),
        dict(title='',url='https://example.com'),dict(title='Good',mobileUrl='https://valid.example/mobile')]
    configure(monkeypatch,{'zhihu':Response(payload(items=items))})
    batch=newsnow.fetch_batch(dict(newsnow_sources=['zhihu']))
    assert len(batch['items'])==1 and batch['sources'][0]['invalid_items']==3
    assert batch['items'][0]['source']['domain']=='valid.example'
    assert len(batch['snapshots'][0]['payload']['items'])==4


@pytest.mark.parametrize('changes',[
    {'base_url':'file:///tmp/data'},{'base_url':'http://user:secret@example.com'},
    {'base_url':'https://example.com?token=secret'},{'base_url':'https://example.com#fragment'},
    {'base_url':'https://example.com:bad'},{'newsnow_sources':[]},{'newsnow_sources':'zhihu'},
    {'newsnow_sources':['../bad']},{'max_items':True},{'max_items':10001},
    {'newsnow_total_timeout':float('nan')},{'newsnow_max_response_bytes':10},
])
def test_validate_fails_before_any_network_io(monkeypatch,changes):
    def forbidden(*_args,**_kwargs):raise AssertionError('Unexpected network')
    monkeypatch.setattr(newsnow.requests,'get',forbidden)
    with pytest.raises(ValueError):newsnow.fetch_batch(changes)


def test_bounded_response_is_rejected_without_claiming_truncated_raw_is_complete(monkeypatch):
    response=Response(body=b'X'*2048)
    configure(monkeypatch,{'zhihu':response})
    with pytest.raises(newsnow.NewsNowUnavailable) as caught:
        newsnow.fetch_batch(dict(newsnow_sources=['zhihu'],newsnow_max_response_bytes=1024))
    assert caught.value.batch['sources'][0]['error_code']=='response_too_large'
    assert caught.value.batch['snapshots']==[]
    assert response.closed


def test_budget_exhaustion_and_cancellation_are_visible_and_do_not_fetch_later_sources(monkeypatch):
    configure(monkeypatch,{'zhihu':Response(payload())})
    ticks=iter([0,181])
    monkeypatch.setattr(newsnow,'monotonic',lambda:next(ticks))
    with pytest.raises(newsnow.NewsNowUnavailable) as caught:
        newsnow.fetch_batch(dict(newsnow_sources=['zhihu']))
    assert caught.value.batch['sources'][0]['status']=='not_attempted'
    assert caught.value.batch['sources'][0]['error_code']=='budget_exceeded'
    class Cancelled(Exception):pass
    def cancel():raise Cancelled()
    monkeypatch.setattr(newsnow,'monotonic',lambda:0)
    with pytest.raises(Cancelled):newsnow.fetch_batch(dict(newsnow_sources=['zhihu']),check_cancel=cancel)
