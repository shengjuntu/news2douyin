from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

STOPWORDS = {
    'the','a','an','of','for','to','and','or','in','on','at','with','from','by',
    'is','are','be','as','after','before','this','that','these','those',
    'said','says','say','will','would','could','should',
    '今日','今天','最新','消息','称','表示','报道','记者','日讯','快讯','市场','公司',
}
TRACKING_KEYS = {'utm_source','utm_medium','utm_campaign','utm_term','utm_content','spm','fbclid','gclid'}
MIN_CONTENT_CHARS = 160
CJK_RUN_RE = re.compile(r'[\u4e00-\u9fff]+')
_TITLE_SUFFIX_RE = re.compile(r'(?:_[\u4e00-\u9fffA-Za-z0-9]*|-[一-鿿]+)+$')


def normalize_whitespace(text: str) -> str:
    return re.sub(r'\s+', ' ', (text or '').strip())


def strip_title_suffix(text: str) -> str:
    stripped = _TITLE_SUFFIX_RE.sub('', text)
    return normalize_whitespace(stripped) if len(stripped) >= 8 else normalize_whitespace(text)


def normalize_text(text: str) -> str:
    text = normalize_whitespace(text)
    text = text.lower()
    text = text.replace('—', '-').replace('–', '-')
    text = re.sub(r'\[[^\]]+\]$', '', text)
    text = re.sub(r'\([^\)]*reuters[^\)]*\)', '', text)
    text = re.sub(r'来源[:：].*$', '', text)
    text = re.sub(r'责编[:：].*$', '', text)
    return normalize_whitespace(text)


def normalize_title(text: str) -> str:
    text = normalize_whitespace(text)
    text = strip_title_suffix(text)
    return normalize_text(text)


def canonicalize_url(url: str) -> str:
    if not url:
        return ''
    p = urlparse(url.strip())
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if k.lower() not in TRACKING_KEYS]
    query.sort()
    path = p.path.rstrip('/') or '/'
    clean = p._replace(scheme=p.scheme.lower(), netloc=p.netloc.lower(), path=path, fragment='', query=urlencode(query))
    return urlunparse(clean)


def short_hash(*parts: str, length: int = 16) -> str:
    h = hashlib.sha1('||'.join(parts).encode('utf-8')).hexdigest()
    return h[:length]


def tokenize(text: str) -> list[str]:
    text = normalize_text(text)
    raw = re.findall(r'[a-zA-Z0-9]+|[\u4e00-\u9fff]+', text)
    toks: list[str] = []
    for t in raw:
        if t in STOPWORDS:
            continue
        toks.append(t)
        if CJK_RUN_RE.fullmatch(t) and len(t) >= 2:
            toks.extend(t[i:i + 2] for i in range(len(t) - 1))
    return toks


def token_signature(tokens: Iterable[str], limit: int = 12) -> str:
    uniq = sorted(set(tokens))[:limit]
    return short_hash(*uniq, length=20) if uniq else ''


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


@dataclass
class NormalizedArticle:
    canonical_url: str
    normalized_title: str
    normalized_content: str
    title_hash: str
    content_hash: str
    title_signature: str
    content_signature: str
    title_tokens: list[str]
    content_tokens: list[str]


def normalize_article(title: str, content: str, url: str) -> NormalizedArticle:
    nt = normalize_title(title)
    nc = normalize_text(content)
    title_tokens = tokenize(nt)
    content_tokens = tokenize(nc)
    return NormalizedArticle(
        canonical_url=canonicalize_url(url),
        normalized_title=nt,
        normalized_content=nc,
        title_hash=short_hash(nt, length=20),
        content_hash=short_hash(nc, length=20) if len(nc) >= MIN_CONTENT_CHARS else '',
        title_signature=token_signature(title_tokens),
        content_signature=token_signature(content_tokens[:40]),
        title_tokens=title_tokens,
        content_tokens=content_tokens,
    )
