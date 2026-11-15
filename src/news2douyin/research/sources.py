"""Bounded source retrieval. Resolve and pin public addresses on every redirect."""
import ipaddress
import math
from queue import Empty, Queue
from threading import BoundedSemaphore, Event, Thread, Timer
from time import monotonic
import socket
import ssl
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import urlsplit, urljoin

import requests
import urllib3

MAX_BYTES = 4 * 1024 * 1024


# Timed-out operating-system DNS queries cannot be forcefully cancelled. Keep
# their number bounded, and use daemon threads so they cannot hold shutdown.
_DNS_SLOTS = BoundedSemaphore(2)


def _remaining(deadline):
    left = deadline - monotonic()
    if left <= 0:
        raise ValueError('资料读取超过总时间预算')
    return left


def _resolve(host, port, deadline):
    if deadline is None:
        return socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    left = _remaining(deadline)
    if not _DNS_SLOTS.acquire(blocking=False):
        raise ValueError('资料域名解析繁忙，请稍后重试')
    result = Queue(maxsize=1)
    resolver = socket.getaddrinfo
    def resolve():
        try:
            result.put((True, resolver(host, port, type=socket.SOCK_STREAM)))
        except Exception:
            result.put((False, None))
        finally:
            _DNS_SLOTS.release()
    worker = Thread(target=resolve, name='source-dns', daemon=True)
    try:
        worker.start()
    except Exception:
        _DNS_SLOTS.release()
        raise ValueError('资料域名无法解析') from None
    try:
        ok, addresses = result.get(timeout=left)
    except Empty:
        raise ValueError('资料读取超过总时间预算') from None
    _remaining(deadline)
    if not ok:
        raise ValueError('资料域名无法解析')
    return addresses


def _budget_connection(pool, sockets, deadline):
    # urllib3 1.26/2.0 lack HTTPResponse.read1. Register the actual connection
    # so the absolute deadline can interrupt slow headers and partial reads,
    # including older supported response implementations, without bypassing
    # urllib3 decompression or Content-Length validation.
    base = getattr(pool, 'ConnectionCls', None)
    if not isinstance(base, type):
        raise ValueError('当前读取组件不支持安全的总时间预算')
    class BudgetConnection(base):
        def connect(self):
            result = super().connect()
            if not isinstance(self.sock, socket.socket):
                self.close()
                raise ValueError('当前读取组件不支持安全的总时间预算')
            sockets.append(self.sock)
            try:
                _remaining(deadline)
            except ValueError:
                self.close()
                raise
            return result
    pool.ConnectionCls = BudgetConnection


def safe_fetch(url, *, timeout_seconds=None, max_bytes=MAX_BYTES):
    """Fetch a public source, optionally with one deadline across all redirects.

    The default path preserves the research reader's original per-hop timeout.
    A bounded preview uses deadline-aware DNS plus a socket shutdown watchdog;
    a trickling source cannot extend the caller's timeout with repeated bytes.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 0 < max_bytes <= MAX_BYTES:
        raise ValueError('资料字节上限须为1至4 MiB')
    deadline = None
    if timeout_seconds is not None:
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or timeout_seconds <= 0):
            raise ValueError('资料总时间预算须为正数')
        deadline = monotonic() + timeout_seconds
    expired = Event()
    sockets = []
    def expire():
        expired.set()
        # shutdown interrupts an in-progress socket read; closing a pool alone
        # only closes idle connections and cannot enforce a streaming deadline.
        for sock in tuple(sockets):
            try:
                # Keep the socket itself: Connection: close may detach it from
                # its HTTPConnection while the response still owns a file ref.
                # Calling the base TCP method also works for an SSL socket whose
                # logical close flag was set while its response keeps the fd.
                socket.socket.shutdown(sock, socket.SHUT_RDWR)
            except OSError:
                pass
    timer = None
    if deadline is not None:
        timer = Timer(_remaining(deadline), expire)
        timer.daemon = True
        timer.start()
    try:
        for _ in range(5):
            if deadline is not None:
                _remaining(deadline)
            p = urlsplit(url)
            if p.scheme not in {'http', 'https'} or not p.hostname or p.username or p.password or p.port not in {None, 80, 443}:
                raise ValueError('只能读取公网 HTTP(S) 标准端口的网页')
            host = p.hostname.encode('idna').decode()
            port = p.port or (443 if p.scheme == 'https' else 80)
            try:
                addresses = list(dict.fromkeys(v[4][0] for v in _resolve(host, port, deadline)))
            except OSError:
                raise ValueError('资料域名无法解析') from None
            if not addresses or any(not ipaddress.ip_address(v).is_global for v in addresses):
                raise ValueError('资料读取不允许访问本机、内网或保留地址')
            left = _remaining(deadline) if deadline is not None else None
            timeout = (urllib3.Timeout(connect=min(5, left), read=min(15, left), total=left)
                       if left is not None else urllib3.Timeout(connect=5, read=15, total=25))
            options = dict(port=port, timeout=timeout, retries=False, maxsize=1)
            if p.scheme == 'https':
                pool = urllib3.HTTPSConnectionPool(addresses[0], server_hostname=host, assert_hostname=host,
                                                   cert_reqs=ssl.CERT_REQUIRED, **options)
            else:
                pool = urllib3.HTTPConnectionPool(addresses[0], **options)
            response = None
            try:
                if deadline is not None:
                    _budget_connection(pool, sockets, deadline)
                response = pool.urlopen('GET', (p.path or '/') + ('?' + p.query if p.query else ''),
                                        headers={'Host': p.netloc, 'User-Agent': 'news2douyin-research/0.11',
                                                 'Accept-Encoding': 'identity'},
                                        redirect=False, preload_content=False)
                if deadline is not None:
                    _remaining(deadline)
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
                    if deadline is not None:
                        _remaining(deadline)
                    reader = getattr(response, 'read1', None) if deadline is not None else None
                    block = (reader(65536, decode_content=True) if callable(reader)
                             else response.read(65536, decode_content=True))
                    if deadline is not None:
                        _remaining(deadline)
                    if not block:
                        break
                    count += len(block)
                    if count > max_bytes:
                        raise ValueError('资料超过 4 MiB 上限，请选择更小的原始资料' if max_bytes == MAX_BYTES
                                         else '资料超过本次读取字节上限')
                    chunks.append(block)
                return url, response.headers.get('Content-Type', ''), b''.join(chunks)
            except (urllib3.exceptions.HTTPError, OSError):
                if expired.is_set() or deadline is not None and monotonic() >= deadline:
                    raise ValueError('资料读取超过总时间预算') from None
                raise ValueError('资料读取失败，请检查来源是否可访问') from None
            finally:
                if response:
                    response.close()
                pool.close()
    finally:
        if timer is not None:
            timer.cancel()
    raise ValueError('资料重定向次数过多')


class TextReader(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title, self.skip = [], [], []
        self.in_title = False
        self.published_at = ''
        self.images = []

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
        if len(self.images) < 24:
            if tag == 'meta' and (attrs.get('property') or attrs.get('name')) in {'og:image', 'og:image:url', 'twitter:image'}:
                self.images.append({'url': attrs.get('content', ''), 'alt': '', 'discovered_as': 'page_metadata'})
            elif tag == 'img' and not self.skip:
                self.images.append({'url': attrs.get('src') or attrs.get('data-src', ''),
                                    'alt': attrs.get('alt', '')[:500], 'discovered_as': 'page_image'})

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
        images, seen = [], set()
        for item in parser.images:
            image_url = urljoin(final, item['url']) if item['url'] else ''
            try:
                p = urlsplit(image_url)
                valid = p.scheme in {'https', 'http'} and p.hostname and not p.username and not p.password and p.port in {None, 80, 443}
            except ValueError:
                valid = False
            if valid and len(image_url) <= 2048 and image_url not in seen:
                images.append(dict(item, url=image_url, page_url=final, rights_status='unknown'))
                seen.add(image_url)
        return dict(url=final, title=''.join(parser.title).strip() or final, text=cleaned,
                    kind='webpage', published_at=parser.published_at,
                    provenance={'reader': 'html-text', 'truncated': len(cleaned) > 100000, 'image_candidates': images[:12]})
    return dict(url=final, title=final, text=text, kind='text', published_at='', provenance={'reader': 'text', 'truncated': len(text) > 100000})
