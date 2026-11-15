"""Small, provenance-labelled previews of source reports, never event assertions.

Only ``research.sources.safe_fetch`` performs I/O. A preview is a headline and
source-supplied description (or a bounded first paragraph); it is not acquired
article fulltext and does not establish that a GDELT coding is correct.
"""
from __future__ import annotations

import codecs
from datetime import datetime, timezone
import hashlib
from html import unescape
from html.parser import HTMLParser
import json
import re
from urllib.parse import urlsplit
from xml.etree import ElementTree

from ..research.sources import safe_fetch
from ..translations.language import describe_language

MAX_TITLE = 500
MAX_EXCERPT = 1200
MAX_BYTES = 512 * 1024
_RE_SPACE = re.compile(r'\s+')
_CHARSET = re.compile(r'charset\s*=\s*[\"\']?\s*([a-zA-Z0-9._-]{1,60})', re.I)
_LANGUAGE = re.compile(r'[a-z]{2,3}(?:-[a-z0-9]{2,8})*')
# Exact or anchored interstitial names, not keywords searched anywhere in an
# article. A real report about access failures or a page with a Sign in button
# must remain readable.
_BLOCKED_TITLE = re.compile(
    r'^(?:access\s+denied|request\s+blocked|403\s+forbidden|forbidden|'
    r'(?:404\s*[-:]?\s*)?(?:page\s+)?not\s+found|404\s+error|'
    r'just\s+a\s+moment|attention\s+required|security\s+verification|robot\s+check|please\s+wait|checking\s+(?:your\s+)?browser|'
    r'verify\s+(?:that\s+)?you\s+are\s+(?:a\s+)?human|captcha|'
    r'(?:sign\s*in|log\s*in|login|subscribe)(?:\s+to\s+(?:continue|read))?|'
    r'登录|登入|请登录|验证(?:码|您)|访问被拒绝|拒绝访问|页面不存在|网页无法访问)'
    r'(?:\s*[.!…:：—–|\-].*|\s*)$', re.I)
_BLOCKED_BODY = re.compile(
    r'^(?:sorry,?\s+you\s+have\s+been\s+blocked|pardon\s+our\s+interruption|you\s+(?:do\s+not|don.t)\s+have\s+permission\s+to\s+access|verify\s+(?:that\s+)?you\s+are\s+(?:a\s+)?human|'
    r'checking\s+(?:your\s+)?browser|enable\s+(?:javascript|cookies)\s+to\s+continue|'
    r'please\s+(?:sign\s*in|log\s*in|subscribe)\s+to\s+(?:continue|read)|'
    r'subscribe\s+to\s+continue\s+reading|您需要登录|请先登录|登录后(?:才)?可(?:以)?阅读|'
    r'订阅后(?:才)?可(?:以)?阅读|请完成(?:人机)?验证)', re.I)
_GENERIC_TITLES = {
    'home', 'homepage', 'news', 'latest news', 'breaking news', 'world',
    'world news', 'business news', 'untitled', 'welcome', 'error',
    'news home', '首页', '主页', '新闻', '最新新闻', '无标题',
}


class PreviewFetchError(ValueError):
    """Public-safe error category and message for durable worker backoff."""
    def __init__(self, kind, message):
        self.kind = kind
        self.message = message
        super().__init__(message)


def _clean(value, limit=None):
    value = unescape(str(value or ''))
    value = ''.join(' ' if ord(c) < 32 or ord(c) == 127 else c for c in value)
    value = _RE_SPACE.sub(' ', value).strip()
    return value[:limit].rstrip() if limit is not None else value


def _safe_url(value):
    try:
        if not isinstance(value, str) or len(value) > 16000 or any(ord(c) < 33 for c in value):
            return ''
        p = urlsplit(value)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password or p.port not in (None, 80, 443):
            return ''
        return value
    except (ValueError, TypeError):
        return ''


def _language(value):
    value = _clean(value, 40).lower().replace('_', '-')
    # A comma-separated http-equiv value is not a declaration of one language.
    return value if _LANGUAGE.fullmatch(value) else ''


def _encoding(value):
    try:
        canonical = codecs.lookup(value).name
    except (LookupError, TypeError):
        return ''
    if (canonical.startswith(('utf-', 'iso8859-', 'cp125')) or canonical in {
            'ascii', 'gb18030', 'gbk', 'gb2312', 'big5', 'big5hkscs', 'shift_jis',
            'euc_jp', 'euc_kr', 'cp949', 'cp874', 'koi8-r', 'koi8-u', 'mac-roman'}):
        return canonical
    return ''


def _decode(body, mime):
    for marker, encoding in ((codecs.BOM_UTF32_LE, 'utf-32'), (codecs.BOM_UTF32_BE, 'utf-32'),
                             (codecs.BOM_UTF8, 'utf-8-sig'),
                             (codecs.BOM_UTF16_LE, 'utf-16'), (codecs.BOM_UTF16_BE, 'utf-16')):
        if body.startswith(marker):
            return body.decode(encoding, errors='replace'), 'bom:' + encoding
    header = _CHARSET.search(mime)
    encoding = _encoding(header[1]) if header else ''
    basis = 'http_charset'
    if not encoding:
        declared = _CHARSET.search(body[:16384].decode('ascii', errors='ignore'))
        encoding = _encoding(declared[1]) if declared else ''
        basis = 'document_charset'
    if not encoding:
        xml = re.match(br'\s*<\?xml[^>]*encoding=[\"\']([^\"\']+)', body[:512], re.I)
        encoding = _encoding(xml[1].decode('ascii', 'ignore')) if xml else ''
        basis = 'xml_charset'
    if not encoding:
        encoding, basis = 'utf-8', 'utf8_fallback'
    return body.decode(encoding, errors='replace'), basis + ':' + encoding


def _headline(value, site_name=''):
    title = _clean(value)
    if not title or len(title) < 4 or title.startswith(('http://', 'https://')):
        return ''
    pieces = re.split(r'\s*[|—–]\s*|\s+-\s+', title)
    if any(_BLOCKED_TITLE.fullmatch(piece) for piece in pieces):
        return ''
    normal = title.casefold().strip(' .!…:：—–|-')
    if normal in _GENERIC_TITLES:
        return ''
    if site_name and normal == _clean(site_name).casefold():
        return ''
    leading = re.split(r'\s+[|—–]\s+|\s+-\s+', title, maxsplit=1)[0].casefold().strip()
    if leading in _GENERIC_TITLES:
        return ''
    if '\ufffd' in title and title.count('\ufffd') > max(2, len(title) // 20):
        return ''
    return title[:MAX_TITLE].rstrip()


def _description(value, title=''):
    description = _clean(value)
    if len(description) < 12 or description.casefold() == title.casefold() or _BLOCKED_BODY.match(description):
        return ''
    if _BLOCKED_TITLE.fullmatch(description):
        return ''
    if '\ufffd' in description and description.count('\ufffd') > max(2, len(description) // 20):
        return ''
    return description[:MAX_EXCERPT].rstrip()


class _PreviewReader(HTMLParser):
    SKIP = {'script', 'style', 'noscript', 'svg', 'nav', 'header', 'footer', 'aside', 'form', 'button'}
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.meta = {}
        self.title = []
        self.title_chars = 0
        self.h1 = []
        self.h1_chars = 0
        self.paragraphs = []
        self.current_p = None
        self.paragraph_chars = 0
        self.body = []
        self.body_chars = 0
        self.language = ''
        self.article_depth = 0
        self.article_stack = []
        self.hidden_stack = []
        self.first_h1_complete = False
        self.article_text = []
        self.article_chars = 0

    def _skipped(self):
        return bool(self.hidden_stack) or any(tag in self.SKIP for tag in self.stack)

    def _flush_p(self):
        if self.current_p is not None:
            value = _clean(''.join(self.current_p))
            if value and len(self.paragraphs) < 120:
                self.paragraphs.append((self.article_depth > 0, value[:8000]))
            self.current_p = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if len(self.stack) >= 256:
            raise ValueError('source_html_nesting_limit')
        attrs = {str(k).lower(): v for k, v in attrs}
        if tag == 'html' and not self.language:
            self.language = _language(attrs.get('lang') or attrs.get('xml:lang'))
        if tag == 'meta':
            key = str(attrs.get('property') or attrs.get('name') or attrs.get('http-equiv') or '').lower()
            content = _clean(attrs.get('content'), 16000)
            if key and content and key not in self.meta:
                self.meta[key] = content
        # Start tags implicitly terminate an unclosed paragraph in common HTML.
        if tag in ('p', 'article', 'main', 'h1', 'h2', 'div') and self.current_p is not None:
            self._flush_p()
        if tag not in self.VOID:
            self.stack.append(tag)
            if ('hidden' in attrs or str(attrs.get('aria-hidden', '')).lower() == 'true'
                    or re.search(r'display\s*:\s*none', attrs.get('style') or '', re.I)):
                self.hidden_stack.append(len(self.stack))
        if tag in ('article', 'main') and not self._skipped():
            self.article_stack.append(len(self.stack))
            self.article_depth = len(self.article_stack)
        if tag == 'p' and not self._skipped():
            self.current_p = []
            self.paragraph_chars = 0
        if tag == 'br' and self.current_p is not None:
            self.current_p.append(' ')

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag == 'p':
            self._flush_p()
        if tag == 'h1' and self.h1:
            self.first_h1_complete = True
        if tag in self.stack:
            # Discard malformed nested tags at the matched end, including skip
            # tags, so a closed script cannot swallow the rest of the document.
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            if any(depth > index for depth in self.article_stack):
                self._flush_p()
            self.stack = self.stack[:index]
            self.article_stack = [depth for depth in self.article_stack if depth <= index]
            self.hidden_stack = [depth for depth in self.hidden_stack if depth <= index]
            self.article_depth = len(self.article_stack)

    def handle_data(self, data):
        if self._skipped():
            return
        if 'title' in self.stack:
            part = data[:max(0,8000-self.title_chars)]
            if part:self.title.append(part);self.title_chars += len(part)
            return
        if 'h1' in self.stack and not self.first_h1_complete:
            part = data[:max(0,8000-self.h1_chars)]
            if part:self.h1.append(part);self.h1_chars += len(part)
        if self.current_p is not None:
            part = data[:max(0,8000-self.paragraph_chars)]
            if part:self.current_p.append(part);self.paragraph_chars += len(part)
        if self.article_depth and self.article_chars < 100000:
            part = data[:100000-self.article_chars]
            self.article_text.append(part); self.article_chars += len(part)
        if self.body_chars < 100000:
            part = data[:100000-self.body_chars]
            self.body.append(part); self.body_chars += len(part)

    def finish(self):
        self._flush_p()


def _html(text):
    reader = _PreviewReader()
    try:
        reader.feed(text); reader.close(); reader.finish()
    except Exception:
        raise PreviewFetchError('parse_error', '来源网页无法解析，已保留待补充状态。') from None
    site = reader.meta.get('og:site_name', '')
    candidates = [('og:title', reader.meta.get('og:title', '')),
                  ('twitter:title', reader.meta.get('twitter:title', '')),
                  ('h1', ''.join(reader.h1)), ('html:title', ''.join(reader.title))]
    title, title_origin = '', ''
    for kind, value in candidates:
        title = _headline(value, site)
        if title:
            title_origin = kind; break
    if not title:
        visible_title = _clean(''.join(reader.title))
        kind = 'blocked_page' if _BLOCKED_TITLE.fullmatch(visible_title) else 'no_headline'
        raise PreviewFetchError(kind, '来源网页未取得可用报道标题，可能是访问限制或非报道页面。')
    excerpt, excerpt_origin = '', ''
    for key in ('og:description', 'description', 'twitter:description'):
        excerpt = _description(reader.meta.get(key, ''), title)
        if excerpt:
            excerpt_origin = key; break
    paragraphs = sorted(enumerate(reader.paragraphs), key=lambda item: (not item[1][0], item[0]))
    if not excerpt:
        for _, (_, value) in paragraphs:
            excerpt = _description(value, title)
            if excerpt:
                excerpt_origin = 'body_first_paragraph'; break
    body = _clean(' '.join(reader.body))
    article_text = _clean(' '.join(reader.article_text))
    access_limited = bool(_BLOCKED_BODY.match(article_text or body))
    # A genuine metadata headline can remain a lead behind a wall, but a short
    # challenge with no meaningful source description is not an article preview.
    if access_limited and not excerpt and title_origin not in ('og:title', 'twitter:title'):
        raise PreviewFetchError('blocked_page', '来源页面需要登录、订阅或验证，尚未取得报道内容。')
    declared = reader.language or _language(reader.meta.get('og:locale')) or _language(reader.meta.get('content-language')) or _language(reader.meta.get('dc.language'))
    return dict(title=title, excerpt=excerpt, title_origin=title_origin, excerpt_origin=excerpt_origin,
        language=declared, published_at=_clean(reader.meta.get('article:published_time') or reader.meta.get('datepublished') or reader.meta.get('date'), 100),
        publisher=_clean(site, 200), access_limited=access_limited)


def _xml(text):
    if re.search(r'<!\s*(?:DOCTYPE|ENTITY)', text, re.I):
        raise PreviewFetchError('unsupported_type', '来源XML包含声明实体，未提取为报道内容。')
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        raise PreviewFetchError('parse_error', '来源XML无法解析，尚未取得报道内容。') from None
    local = lambda tag: tag.rsplit('}', 1)[-1].lower()
    if local(root.tag) not in {'article', 'entry', 'document', 'newsitem'}:
        raise PreviewFetchError('no_headline', '该XML不是可确认的单篇报道，未把订阅列表当作事件内容。')
    values = {}
    for child in root:
        values.setdefault(local(child.tag), _clean(' '.join(child.itertext())))
    title = _headline(values.get('title') or values.get('headline'))
    if not title:
        raise PreviewFetchError('no_headline', '来源记录未提供可用报道标题。')
    excerpt = _description(values.get('summary') or values.get('description') or values.get('content'), title)
    return dict(title=title, excerpt=excerpt, title_origin='xml:title', excerpt_origin='xml:description' if excerpt else '',
        language=_language(root.attrib.get('{http://www.w3.org/XML/1998/namespace}lang') or root.attrib.get('lang')),
        published_at=_clean(values.get('published') or values.get('updated') or values.get('date'), 100), publisher='', access_limited=False)


def _text(text):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise PreviewFetchError('no_headline', '来源文本为空，尚未取得报道内容。')
    match = re.match(r'^(?:title\s*[:：]|标题\s*[:：]|#\s+)\s*(.+)$', lines[0], re.I)
    title = _headline(match[1]) if match else ''
    if not title:
        raise PreviewFetchError('no_headline', '纯文本未标明报道标题，未用URL或任意首行生成标题。')
    excerpt = _description(' '.join(lines[1:]), title)
    return dict(title=title, excerpt=excerpt, title_origin='text:explicit_title', excerpt_origin='text:excerpt' if excerpt else '',
        language='', published_at='', publisher='', access_limited=False)


def _fetch_error(exc):
    raw = str(exc)
    status = re.search(r'HTTP\s+(\d{3})', raw)
    if status:
        code = int(status[1])
        return PreviewFetchError('http_error', '来源网页返回HTTP ' + str(code) + '，尚未取得报道内容。')
    if any(word in raw for word in ('公网', '内网', '保留地址', '只能读取')):
        return PreviewFetchError('unsafe_url', '该来源地址不满足公网网页读取条件，未发起补取。')
    if any(value in raw for value in ('MiB', '读取大小上限', 'size limit')):
        return PreviewFetchError('size_limit', '来源网页超过读取大小上限，尚未取得报道内容。')
    if '重定向' in raw:
        return PreviewFetchError('redirect_error', '来源重定向无法安全完成，尚未取得报道内容。')
    return PreviewFetchError('fetch_failed', '来源网页暂不可读取，请稍后重试。')


def fetch_preview(url):
    """Fetch at most one bounded source document and return an unverified lead.

    HTML metadata takes precedence over body extraction. Unsupported documents,
    URL-only responses and access interstitials raise a public-safe categorized
    error; this function never invents a headline from actors or URL fragments.
    """
    if not _safe_url(url):
        raise PreviewFetchError('unsafe_url', '该来源地址不满足公网网页读取条件，未发起补取。')
    try:
        final, mime, body = safe_fetch(url, timeout_seconds=35, max_bytes=MAX_BYTES)
    except Exception as exc:
        raise _fetch_error(exc) from None
    if not _safe_url(final) or not isinstance(body, bytes) or len(body) > MAX_BYTES:
        raise PreviewFetchError('fetch_failed', '来源返回内容未通过读取校验，尚未取得报道内容。')
    mime = str(mime or '')[:500]
    media = mime.split(';', 1)[0].strip().lower()
    if body.startswith(b'%PDF') or media == 'application/pdf':
        raise PreviewFetchError('unsupported_type', '该来源为PDF，请在研究材料读取阶段处理，未把文件名当成报道标题。')
    if media and not (media.startswith('text/') or any(kind in media for kind in ('html', 'xml'))):
        raise PreviewFetchError('unsupported_type', '该来源不是可直接提取标题的网页或文本。')
    text, encoding = _decode(body, mime)
    head = text[:4000].lstrip().lower()
    explicit_xml = 'xml' in media or head.startswith('<?xml') or bool(re.match(r'<(?:rss|feed)\b', head))
    if 'html' in media or re.search(r'<(?:!doctype\s+html|html\b)', head):
        data = _html(text)
    elif explicit_xml:
        data = _xml(text)
    elif re.search(r'<(?:head\b|meta\b|title\b|h1\b|article\b|main\b)', head):
        data = _html(text)
    else:
        data = _text(text)
    # A publisher redirecting a specific report to its home page has not
    # supplied that report. Do not attach the current front-page headline.
    original_parts, final_parts = urlsplit(url), urlsplit(final)
    if original_parts.path.strip('/') and not final_parts.path.strip('/'):
        raise PreviewFetchError('no_headline', '原报道跳转到网站首页，尚未取得该报道内容。')
    info = describe_language(data['title'] + '\n' + data['excerpt'], data.get('language', ''))
    data.update(language=info['code'], language_info=info,
        domain=(urlsplit(final).hostname or '').lower().rstrip('.'), final_url=final,
        material_kind='source_excerpt', content_kind='source_excerpt' if data['excerpt'] else 'title_only',
        content_hash=hashlib.sha256(json.dumps([data['title'], data['excerpt']], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(),
        body_hash=hashlib.sha256(body).hexdigest(), fetched_at=datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z'),
        decoding_basis=encoding, note='来源报道标题和摘录，仅供筛选线索；一篇报道可能提及多个事件，尚未核对本条事件事实。')
    return data
