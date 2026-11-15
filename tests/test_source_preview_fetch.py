"""Real HTML parsing and existing public-address fetch guards; no live network."""
from hashlib import sha256
import io
import json

import pytest

from news2douyin.selection import source_fetch as preview
from news2douyin.research import sources

URL = 'https://news.example/articles/bridge'


def source(monkeypatch, document, mime='text/html; charset=utf-8', final=URL):
    calls=[]
    def fetch(url, **kwargs):
        calls.append((url, kwargs))
        return final, mime, document.encode('utf-8') if isinstance(document, str) else document
    monkeypatch.setattr(preview, 'safe_fetch', fetch)
    return calls


def test_open_graph_headline_and_description_are_actual_source_text(monkeypatch):
    body='''<!doctype html><html lang="fr"><head>
        <title>Wrong generic tab caption</title><meta property="og:title" content="Le gouvernement annonce un accord ferroviaire">
        <meta name="twitter:title" content="A second social caption"><meta property="og:site_name" content="Le Quotidien">
        <meta property="og:description" content="Les ministres ont annoncé un accord mardi, selon leur communiqué commun.">
        <meta property="article:published_time" content="2026-11-14T06:00:00Z"></head>
        <body><article><h1>A third heading</h1><p>Different article text.</p></article></body></html>'''
    calls=source(monkeypatch,body)
    data=preview.fetch_preview(URL)
    assert data['title']=='Le gouvernement annonce un accord ferroviaire'
    assert data['excerpt']=='Les ministres ont annoncé un accord mardi, selon leur communiqué commun.'
    assert data['title_origin']=='og:title' and data['excerpt_origin']=='og:description'
    assert data['publisher']=='Le Quotidien' and data['language']=='fr'
    assert data['language_info']['basis']=='declared'
    assert data['published_at']=='2026-11-14T06:00:00Z'
    assert data['material_kind']=='source_excerpt' and data['content_kind']=='source_excerpt'
    assert data['body_hash']==sha256(body.encode()).hexdigest()
    assert data['domain']=='news.example' and data['final_url']==URL
    assert len(data['content_hash'])==64 and data['fetched_at'].endswith('Z')
    assert '多个事件' in data['note'] and '尚未核对' in data['note']
    assert calls==[(URL,dict(timeout_seconds=35,max_bytes=512*1024))]


def test_h1_then_article_paragraph_excludes_controls_scripts_navigation_and_hidden_text(monkeypatch):
    source(monkeypatch,'''<html><head><title>Homepage</title></head><body>
       <header>Sign in</header><nav><p>Navigation should not be the report.</p></nav>
       <script>send credential to attacker</script><style>p{display:none}</style>
       <p hidden>Hidden boilerplate must never be the story excerpt.</p>
       <p style="display: none">Another invisible introduction.</p>
       <p>Outside-article boilerplate would otherwise be the first paragraph.</p>
       <main><h1>City council approves a new river bridge</h1>
       <p>The council approved the project on Tuesday after a public debate.</p>
       <h1>Related stories heading</h1></main><footer>Private footer navigation</footer></body></html>''')
    data=preview.fetch_preview(URL)
    assert data['title']=='City council approves a new river bridge'
    assert data['title_origin']=='h1'
    assert data['excerpt']=='The council approved the project on Tuesday after a public debate.'
    assert data['excerpt_origin']=='body_first_paragraph'
    assert 'Hidden' not in data['excerpt'] and 'credential' not in json.dumps(data)
    # Latin script alone is not reliable language evidence.
    assert data['language']=='unknown' and data['language_info']['basis']=='unknown'


def test_first_usable_social_title_falls_back_without_rewriting_source_content(monkeypatch):
    source(monkeypatch,'''<html lang=en><head><meta property=og:title content="Access Denied">
        <meta name=twitter:title content="Port workers announce a strike for Friday">
        <meta name=description content="The union said the walkout would begin at six ports on Friday.">
        </head></html>''')
    data=preview.fetch_preview(URL)
    assert data['title']=='Port workers announce a strike for Friday'
    assert data['title_origin']=='twitter:title'
    assert data['excerpt_origin']=='description'


@pytest.mark.parametrize('title',['Access Denied', 'Just a moment...', 'Attention Required! | Cloudflare',
    'Sign in | Example News', 'Example News — Login', '404 Not Found', '首页', 'News', 'Welcome'])
def test_interstitial_and_generic_headlines_are_not_useful_event_content(monkeypatch,title):
    source(monkeypatch,'<html><title>'+title+'</title><body><p>Please sign in to continue reading.</p></body></html>')
    with pytest.raises(preview.PreviewFetchError) as error:
        preview.fetch_preview(URL)
    assert error.value.kind in ('blocked_page','no_headline')
    assert URL not in str(error.value)


def test_normal_article_with_login_button_and_access_failure_keywords_is_not_rejected(monkeypatch):
    source(monkeypatch,'''<html><title>Users report access denied errors after software update</title>
      <body><nav><button>Sign in</button></nav><article><p>
      Users saw an access denied warning during the outage, according to the operator.
      </p></article></body></html>''')
    data=preview.fetch_preview(URL)
    assert data['title'].startswith('Users report access denied')
    assert 'operator' in data['excerpt'] and data['access_limited'] is False


def test_real_metadata_can_remain_a_lead_behind_subscription_wall(monkeypatch):
    source(monkeypatch,'''<html><head><title>Subscribe</title>
       <meta property="og:title" content="Finance ministers agree to resume regional talks">
       <meta property="og:description" content="The delegations agreed to meet again next month, the newspaper reported."></head>
       <body><main><p>Subscribe to continue reading</p></main></body></html>''')
    data=preview.fetch_preview(URL)
    assert data['title_origin']=='og:title' and data['excerpt_origin']=='og:description'
    assert data['access_limited'] is True
    assert data['content_kind']=='source_excerpt' and 'Subscribe' not in data['excerpt']


def test_title_only_is_explicit_and_never_invents_an_event_summary(monkeypatch):
    source(monkeypatch,'<html lang="pl"><meta property=og:title content="Prezydent zapowiada spotkanie z premierem"></html>')
    data=preview.fetch_preview(URL)
    assert data['excerpt']=='' and data['excerpt_origin']==''
    assert data['content_kind']=='title_only' and data['language']=='pl'
    assert 'ZUNI' not in data['title'] and '交战' not in data['title']


def test_html_meta_charset_recovers_chinese_bytes_without_http_charset(monkeypatch):
    title='有关部门宣布恢复跨境铁路客运服务'
    body=('<html lang=zh-CN><head><meta charset=gb18030><title>'+title+
          '</title><meta name=description content="有关部门表示，跨境铁路客运将于下个月恢复运行。"></head></html>').encode('gb18030')
    source(monkeypatch,body,'text/html')
    data=preview.fetch_preview(URL)
    assert data['title']==title and data['language']=='zh-cn'
    assert data['excerpt']=='有关部门表示，跨境铁路客运将于下个月恢复运行。'
    assert data['decoding_basis']=='document_charset:gb18030'


def test_bom_takes_precedence_and_titles_excerpts_are_bounded(monkeypatch):
    title='A report headline '+('detail '*100)
    text='<html lang=en><meta property=og:title content="'+title+'"><meta name=description content="'+('Reported detail. '*200)+'"></html>'
    source(monkeypatch,text.encode('utf-16'),'text/html; charset=utf-8')
    data=preview.fetch_preview(URL)
    assert len(data['title'])==500 and len(data['excerpt'])<=1200
    assert data['decoding_basis']=='bom:utf-16'


def test_xml_single_article_and_plain_explicit_heading_only(monkeypatch):
    source(monkeypatch,'<?xml version="1.0"?><article xml:lang="es"><title>El parlamento aprueba el acuerdo comercial</title><summary>Los legisladores aprobaron el acuerdo durante una sesión extraordinaria.</summary></article>','application/xml')
    data=preview.fetch_preview(URL)
    assert data['title_origin']=='xml:title' and data['language']=='es'
    assert 'legisladores' in data['excerpt']
    source(monkeypatch,'# Parliament approves a new transport agreement\n\nThe motion passed after several hours of debate.','text/plain')
    data=preview.fetch_preview(URL)
    assert data['title_origin']=='text:explicit_title'
    source(monkeypatch,'An arbitrary first line is not a declared headline.\nOther text is here.','text/plain')
    with pytest.raises(preview.PreviewFetchError,match='未标明'):
        preview.fetch_preview(URL)


def test_no_title_never_uses_url_slug_or_a_multi_article_feed(monkeypatch):
    source(monkeypatch,'<html><body><p>Some source text without a title should remain unavailable.</p></body></html>')
    with pytest.raises(preview.PreviewFetchError) as error:preview.fetch_preview(URL)
    assert error.value.kind=='no_headline'
    source(monkeypatch,'<rss><channel><title>A site feed</title><item><title>One article</title></item></channel></rss>','application/xml')
    with pytest.raises(preview.PreviewFetchError) as error:preview.fetch_preview(URL)
    assert error.value.kind=='no_headline'


@pytest.mark.parametrize('mime,body',[('application/pdf',b'%PDF a file'),('image/png',b'PNG binary')])
def test_non_html_documents_do_not_trigger_another_fetch(monkeypatch,mime,body):
    calls=source(monkeypatch,body,mime)
    with pytest.raises(preview.PreviewFetchError) as error:preview.fetch_preview(URL)
    assert error.value.kind=='unsupported_type' and len(calls)==1


def test_bad_addresses_and_transport_errors_are_sanitized(monkeypatch):
    calls=source(monkeypatch,'<html><title>Unused page headline</title></html>')
    for value in ('file:///secret','https://username:password@news.example/article','http://news.example:8080/article'):
        with pytest.raises(preview.PreviewFetchError) as error:preview.fetch_preview(value)
        assert error.value.kind=='unsafe_url'
    assert calls==[]
    def failed(*args,**kwargs):raise ValueError('GET https://user:private-password@host/path?token=secret failed')
    monkeypatch.setattr(preview,'safe_fetch',failed)
    with pytest.raises(preview.PreviewFetchError) as error:preview.fetch_preview(URL)
    assert error.value.kind=='fetch_failed'
    assert not any(word in str(error.value) for word in ('password','token','https://'))


def test_body_size_limit_is_checked_even_for_an_invalid_test_transport(monkeypatch):
    source(monkeypatch,b'x'*(preview.MAX_BYTES+1),'text/html')
    with pytest.raises(preview.PreviewFetchError):preview.fetch_preview(URL)


def test_existing_dns_guard_blocks_any_private_resolution_before_http(monkeypatch):
    # Exercise the existing transport itself, not a pretend caller-side guard.
    monkeypatch.setattr(preview,'safe_fetch',lambda url,**kwargs:sources.safe_fetch(url))
    monkeypatch.setattr(sources.socket,'getaddrinfo',lambda *args,**kwargs:[(2,1,6,'',('127.0.0.1',443))])
    def forbidden(*args,**kwargs):raise AssertionError('private URL reached HTTP')
    monkeypatch.setattr(sources.urllib3,'HTTPSConnectionPool',forbidden)
    with pytest.raises(preview.PreviewFetchError) as error:preview.fetch_preview(URL)
    assert error.value.kind=='unsafe_url'


def test_existing_redirect_guard_rechecks_destination_dns(monkeypatch):
    opened=[]
    class Reply:
        status=302
        headers={'Location':'http://internal.example/private'}
        def close(self):pass
    class Pool:
        def __init__(self,address,**kwargs):opened.append(address)
        def urlopen(self,*args,**kwargs):return Reply()
        def close(self):pass
    def resolve(host,port,**kwargs):
        return [(2,1,6,'',('93.184.216.34' if host=='news.example' else '192.168.0.234',port))]
    monkeypatch.setattr(preview,'safe_fetch',lambda url,**kwargs:sources.safe_fetch(url))
    monkeypatch.setattr(sources.socket,'getaddrinfo',resolve)
    monkeypatch.setattr(sources.urllib3,'HTTPSConnectionPool',Pool)
    monkeypatch.setattr(sources.urllib3,'HTTPConnectionPool',Pool)
    with pytest.raises(preview.PreviewFetchError) as error:preview.fetch_preview(URL)
    assert error.value.kind=='unsafe_url' and opened==['93.184.216.34']


def test_fragment_without_mime_and_deep_malformed_markup_are_bounded(monkeypatch):
    source(monkeypatch,'<title>A source headline without a content type</title><p>The publisher provided a usable source paragraph.</p>','')
    assert preview.fetch_preview(URL)['title']=='A source headline without a content type'
    source(monkeypatch,'<html><title>A real heading before deeply nested markup</title>'+'<div>'*400)
    with pytest.raises(preview.PreviewFetchError) as error:preview.fetch_preview(URL)
    assert error.value.kind=='parse_error'


def test_many_inline_nodes_use_bounded_paragraph_buffers(monkeypatch):
    source(monkeypatch,'<html><title>Public transport expansion receives final approval</title><article><p>'+('<span>detail </span>'*12000)+'</p></article></html>')
    data=preview.fetch_preview(URL)
    assert len(data['excerpt'])<=1200 and data['excerpt'].startswith('detail detail')


def test_article_redirected_to_frontpage_does_not_inherit_an_unrelated_headline(monkeypatch):
    source(monkeypatch,'<html><title>Current top story unrelated to the old report</title><meta name=description content="The home page describes another story that is not the requested source."></html>',final='https://news.example/')
    with pytest.raises(preview.PreviewFetchError) as error:preview.fetch_preview(URL)
    assert error.value.kind=='no_headline' and '首页' in str(error.value)
