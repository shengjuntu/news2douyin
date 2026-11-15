"""Bounded, read-only original-source content lookup for event cards.

One article may mention many GDELT events. A headline/excerpt is therefore a
source-reading lead, never a generated summary or verification of one event.
"""
from __future__ import annotations
import hashlib
import html
import json
import re
import unicodedata
from urllib.parse import urlsplit

from sqlalchemy import func
from sqlmodel import select

from ..storage.articles import identity_key
from ..storage.models import Article, ArticleIdentity
from ..translations.language import describe_language
from .source_models import SourcePreview, SourcePreviewJob

MAX_URLS = 400
MAX_TITLE = 500
MAX_EXCERPT = 1200
_GENERIC = {'access denied', '403 forbidden', '404 not found', 'not found', 'just a moment',
    'just a moment...', 'attention required', 'checking your browser', 'sign in', 'log in',
    'login', 'subscribe', 'please enable javascript', '页面不存在', '访问受限', '请登录', '安全验证'}


def source_url(value):
    if not isinstance(value, str) or not value or len(value) > 16000 or any(ord(c) < 33 for c in value):
        return ''
    try:
        p = urlsplit(value)
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
            return ''
        p.port
        return value
    except (ValueError, UnicodeError):
        return ''


def text(value, limit):
    return ' '.join(html.unescape(str(value or '')).split())[:limit]


def usable_title(value):
    value = text(value, MAX_TITLE)
    stripped = value.strip(' .…|-–—').casefold()
    return bool(len(stripped) >= 2 and stripped not in _GENERIC and
        not re.match(r'^(?:https?://|www\.)\S+$', value, re.I) and
        not re.match(r'^(?:access denied|403 forbidden|404 not found|just a moment|checking your browser)(?:\s*[-|:…].*)?$', value, re.I))


def preview_hash(value):
    content = {key:value.get(key, '') for key in ('title', 'excerpt', 'language', 'published_at', 'source_url', 'origin')}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def compatible_titles(first,second):
    """Do not splice an old excerpt onto a changed headline/denial/quantity.

    Deliberately use only Unicode/case/whitespace normalization, not semantic
    similarity or token deletion. A false split is safer than mixed versions.
    """
    normalize=lambda value:' '.join(unicodedata.normalize('NFKC',str(value or '')).casefold().split())
    return bool(first and second and normalize(first)==normalize(second))


def local_basis(preview):
    return hashlib.sha256(preview['title'].encode()).hexdigest() if preview.get('origin')=='local_article' else ''


def _base(url, job):
    state = job.state if job else 'missing'
    return dict(key=identity_key({'url':url}),title='',excerpt='',language='',
        language_info=describe_language(),source_url=url,final_url='',domain=urlsplit(url).hostname or '',
        origin='',content_hash='',status=state,ready=False,has_content=False,
        error=job.error if job else '',error_kind=job.error_kind if job else '',retry_at=job.retry_at if job else 0,
        attempts=job.attempts if job else 0,fetch_status=state,job_status=state,
        translation=None,material_kind='source_excerpt',has_excerpt=False,
        note='这是关联报道的标题/摘要，可能涉及多个事件；尚未核查为本条事件事实。')


def resolve_previews(session, urls):
    """Resolve at most400 URL leads using indexed identities, never network/DDL.

    Return keys are the exact supplied source URLs, including tracking variants.
    Existing local reports win over a fetched preview. Long URL columns are not
    searched; the article identity ledger and preview/job SHA keys are used.
    """
    if not isinstance(urls, (list, tuple, set)):
        raise ValueError('报道地址列表无效')
    values = list(dict.fromkeys(url for url in urls if source_url(url)))
    if len(values) > MAX_URLS:
        raise ValueError('一次最多解析400个报道地址')
    if not values:
        return {}
    keys = list(dict.fromkeys(identity_key({'url':url}) for url in values))
    identities = dict(session.exec(select(ArticleIdentity.identity_key, ArticleIdentity.article_key)
        .where(ArticleIdentity.identity_key.in_(keys))
        .execution_options(n2d_query_name='selection.source_content.identities')).all())
    article_keys = list(dict.fromkeys(identities.values()))
    articles = {}
    if article_keys:
        rows = session.exec(select(Article.article_key, func.substr(Article.title,1,MAX_TITLE),
            func.substr(Article.content,1,MAX_EXCERPT), Article.language, Article.published_at,
            Article.source_domain, Article.url, Article.fetched_at).where(Article.article_key.in_(article_keys))
            .execution_options(n2d_query_name='selection.source_content.local')).all()
        articles = {row[0]:row for row in rows}
    cached = {row.key:row for row in session.exec(select(SourcePreview).where(SourcePreview.key.in_(keys))
        .execution_options(n2d_query_name='selection.source_content.cached')).all()}
    jobs = {row.key:row for row in session.exec(select(SourcePreviewJob).where(SourcePreviewJob.key.in_(keys))
        .execution_options(n2d_query_name='selection.source_content.jobs')).all()}
    output = {}
    for url in values:
        key = identity_key({'url':url}); value = _base(url, jobs.get(key))
        local = articles.get(identities.get(key))
        cache = cached.get(key)
        if local and usable_title(local[1]):
            _, title, excerpt, language, published, domain, final, fetched = local
            value.update(title=text(title,MAX_TITLE),excerpt=text(excerpt,MAX_EXCERPT),language=language or '',
                published_at=published or '',domain=domain or value['domain'],final_url=source_url(final) or url,
                origin='local_article',title_origin='saved_article_title',excerpt_origin='saved_article_excerpt',
                article_key=local[0],fetched_at=fetched or '',status='ready',ready=True,has_content=True,
                translation=dict(kind='source_preview',key=key,scope='document'))
            if not value['excerpt'] and cache and cache.excerpt:
                if compatible_titles(value['title'],cache.title):
                    value.update(excerpt=cache.excerpt,excerpt_origin=cache.excerpt_origin,
                        excerpt_fetched_at=cache.fetched_at,mixed_origin=True,
                        translation=dict(kind='source_preview',key=key,scope='document'))
                else:
                    value['note']='已存标题与缓存摘要的版本不一致，暂不拼接；可查看来源或重新补充。'
            value['content_hash'] = preview_hash(value)
        elif cache and usable_title(cache.title):
            value.update({field:getattr(cache,field) for field in ('title','excerpt','language','published_at','domain',
                'final_url','origin','title_origin','excerpt_origin','article_key','content_hash','fetched_at')})
            value.update(status='ready',ready=True,has_content=True,
                translation=dict(kind='source_preview',key=key,scope='document'))
        value['has_excerpt']=bool(value['excerpt'])
        if value['ready']:value['status']='ready' if value['has_excerpt'] else 'title_only'
        value['language_info'] = describe_language(value['title']+'\n'+value['excerpt'],value['language'])
        output[url] = value
    return output
