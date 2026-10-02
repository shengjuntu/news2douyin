"""Bounded source retrieval. Resolve and pin public addresses on every redirect."""
import ipaddress
import os
import socket
import ssl
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import urlsplit, urljoin

import requests
import urllib3

MAX_BYTES = 4 * 1024 * 1024


def safe_fetch(url):
    for _ in range(5):
        p = urlsplit(url)
        if p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password or p.port not in {None, 80, 443}:
            raise ValueError('只能读取公网 HTTP(S) 标准端口的网页')
        host = p.hostname.encode('idna').decode()
        port = p.port or (443 if p.scheme == 'https' else 80)
        try:
            addresses = list(dict.fromkeys(v[4][0] for v in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
        except OSError:
            raise ValueError('资料域名无法解析') from None
        if not addresses or any(not ipaddress.ip_address(v).is_global for v in addresses):
            raise ValueError('资料读取不允许访问本机、内网或保留地址')
        timeout = urllib3.Timeout(connect=5, read=15, total=25)
        options = dict(port=port, timeout=timeout, retries=False, maxsize=1)
        if p.scheme == 'https':
            pool = urllib3.HTTPSConnectionPool(addresses[0], server_hostname=host, assert_hostname=host,
                                               cert_reqs=ssl.CERT_REQUIRED, **options)
        else:
            pool = urllib3.HTTPConnectionPool(addresses[0], **options)
        response = None
        try:
            response = pool.urlopen('GET', (p.path or '/') + ('?' + p.query if p.query else ''),
                                    headers={'Host': p.netloc, 'User-Agent': 'news2douyin-research/0.11',
                                             'Accept-Encoding': 'identity'},
                                    redirect=False, preload_content=False)
            if response.status in {301, 302, 303, 307, 308}:
                location = response.headers.get('Location')
                if not location:
                    raise ValueError('资料重定向缺少目标地址')
                url = urljoin(url, location)
                continue
            if response.status != 200:
                raise ValueError(f'资料读取返回 HTTP {response.status}')
            chunks, count = [], 0
            while True:
                block = response.read(65536, decode_content=True)
                if not block:
                    break
                count += len(block)
                if count > MAX_BYTES:
                    raise ValueError('资料超过 4 MiB 上限，请选择更小的原始资料')
                chunks.append(block)
            return url, response.headers.get('Content-Type', ''), b''.join(chunks)
        except (urllib3.exceptions.HTTPError, OSError):
            raise ValueError('资料读取失败，请检查来源是否可访问') from None
        finally:
            if response:
                response.close()
            pool.close()
    raise ValueError('资料重定向次数过多')


class TextReader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title, self.skip = [], [], []
        self.in_title = False
        self.published_at = ''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {'script', 'style', 'noscript', 'svg', 'nav', 'footer'}:
            self.skip.append(tag)
        if tag == 'title':
            self.in_title = True
        if tag in {'p', 'div', 'br', 'li', 'h1', 'h2', 'h3', 'tr', 'article'}:
            self.parts.append('\n')
        if tag == 'meta' and (attrs.get('property') or attrs.get('name')) in {'article:published_time', 'datePublished', 'date'}:
            self.published_at = attrs.get('content', '')

    def handle_endtag(self, tag):
        if self.skip and tag == self.skip[-1]:
            self.skip.pop()
        if tag == 'title':
            self.in_title = False
        if tag in {'p', 'div', 'li', 'article', 'tr'}:
            self.parts.append('\n')

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if not self.skip and not self.in_title:
            self.parts.append(data)


def read_document(url):
    final, mime, body = safe_fetch(url)
    if body.startswith(b'%PDF') or 'application/pdf' in mime:
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ValueError('PDF 阅读组件未安装，请安装 news2douyin[research]') from None
        try:
            reader = PdfReader(BytesIO(body))
            if reader.is_encrypted:
                raise ValueError('暂不支持加密 PDF')
            if len(reader.pages) > 150:
                raise ValueError('PDF 超过 150 页，请选择相关章节')
            text = '\n\n'.join(f'第 {i+1} 页\n' + (page.extract_text() or '') for i, page in enumerate(reader.pages))
            title = str((reader.metadata or {}).get('/Title') or final)
        except ValueError:
            raise
        except Exception:
            raise ValueError('PDF 正文无法解析') from None
        if not text.strip() or len(text.strip()) < 30:
            raise ValueError('PDF 没有足够的可提取文本，扫描件需要先做 OCR')
        return dict(url=final, title=title, text=text, kind='pdf', published_at='', provenance={'reader': 'pypdf', 'truncated': len(text) > 100000})
    if mime and not any(v in mime.lower() for v in ('text/', 'html', 'xml')):
        raise ValueError('暂只支持网页、文本和 PDF')
    encoding = requests.utils.get_encoding_from_headers({'content-type': mime}) or 'utf-8'
    if encoding.lower() == 'iso-8859-1':
        encoding = 'utf-8'
    try:
        text = body.decode(encoding, errors='replace')
    except LookupError:
        text = body.decode('utf-8', errors='replace')
    if 'html' in mime or '<html' in text[:1000].lower():
        parser = TextReader(); parser.feed(text)
        cleaned = '\n'.join(line.strip() for line in ''.join(parser.parts).splitlines() if line.strip())
        return dict(url=final, title=''.join(parser.title).strip() or final, text=cleaned,
                    kind='webpage', published_at=parser.published_at,
                    provenance={'reader': 'html-text', 'truncated': len(cleaned) > 100000})
    return dict(url=final, title=final, text=text, kind='text', published_at='', provenance={'reader': 'text', 'truncated': len(text) > 100000})


def web_search(settings, case, query):
    import json
    strategy = json.loads(case.strategy_json)
    provider = settings['search_provider']
    key = settings.get('search_api_key') or os.getenv('TAVILY_API_KEY' if provider == 'tavily' else 'API_KEY', '')
    if not key:
        raise ValueError('搜索服务凭据未配置，请先打开研究设置')
    try:
        if provider == 'tavily':
            payload = dict(query=query, max_results=8, search_depth='basic', include_answer=False,
                           include_raw_content=False, topic='general', end_date=case.cutoff[:10])
            if case.date_from:
                payload['start_date'] = case.date_from
            # Preferred domains are hints in the Skill, not a hard restriction.
            response = requests.post('https://api.tavily.com/search', json=payload,
                                     headers={'Authorization': 'Bearer ' + key}, timeout=(5, 30), allow_redirects=False)
        else:
            params = {'text': query, 'number': 8, 'latest-publish-date': case.cutoff.replace('T', ' ').removesuffix('Z')}
            if strategy['language']:
                params['language'] = strategy['language']
            if case.date_from:
                params['earliest-publish-date'] = case.date_from + ' 00:00:00'
            response = requests.get('https://api.worldnewsapi.com/search-news', params=params,
                                    headers={'x-api-key': key}, timeout=(5, 30), allow_redirects=False)
        if response.status_code != 200:
            raise ValueError(f'搜索服务返回 HTTP {response.status_code}，请检查凭据、额度或查询条件')
        data = response.json()
    except requests.RequestException:
        raise ValueError('搜索服务连接失败或超时') from None
    except (TypeError, json.JSONDecodeError):
        raise ValueError('搜索服务返回格式无效') from None
    rows = data.get('results' if provider == 'tavily' else 'news')
    if not isinstance(rows, list):
        raise ValueError('搜索服务返回格式无效')
    result = []
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        url = str(row.get('url', ''))
        try:
            p = urlsplit(url)
            if p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password:
                continue
        except ValueError:
            continue
        # Results are leads, never accepted as original evidence until read_source.
        result.append(dict(title=str(row.get('title') or url)[:1000], url=url,
                           snippet=str(row.get('content') or row.get('summary') or row.get('text') or '')[:3000],
                           published_at=str(row.get('published_date') or row.get('publish_date') or ''), provider=provider))
    return result
