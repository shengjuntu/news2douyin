from fastapi import HTTPException, Query, Response
from sqlmodel import select

from ..search.service import article_page, event_page
from ..storage.articles import latest_version, version_page
from ..storage.db import session_scope
from ..storage.models import Article, ArticleEventLink, ArticleVersion, EventAssignment
from ..storage.utils import loads


def article_summary(row):
    return {key: getattr(row, key) for key in ('article_key', 'title', 'url', 'source_domain',
            'country', 'published_at', 'market_relevance_score', 'is_duplicate', 'dedup_reason')}


def event_summary(row):
    return {key: getattr(row, key) for key in ('event_key', 'event_title', 'topic', 'summary',
            'article_count', 'importance', 'last_seen_at')}


def query_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(404, 'article/version not found') from exc


def register_news_routes(app, engine):
    def headers(response, page):
        response.headers['X-Total-Count'] = str(page.total)
        response.headers['X-Offset'] = str(page.offset)
        response.headers['X-Limit'] = str(page.limit)
        response.headers['X-Search-Backend'] = page.search_backend

    @app.get('/api/articles')
    @app.get('/api/articles/search')
    def articles(response: Response, query: str = '', country: str = '', category: str = '',
                 duplicates: str = 'any', limit: int = Query(50, ge=1, le=500),
                 offset: int = Query(0, ge=0), mode: str = 'contains', paginated: bool = False,
                 period: str = 'all', date_from: str = '', date_to: str = '', time_field: str = 'published', timezone: str = '',
                 task_id: str = '', profile_name: str = ''):
        with session_scope(engine) as session:
            page = query_call(article_page, session, query=query, country=country, category=category,
                              duplicates=duplicates, limit=limit, offset=offset, mode=mode,
                              period=period, date_from=date_from, date_to=date_to, time_field=time_field,
                              timezone=timezone, task_id=task_id, profile_name=profile_name)
            items = [article_summary(row) for row in page.items]
            headers(response, page)
            return page.envelope(items) if paginated else items

    @app.get('/api/events')
    @app.get('/api/events/search')
    def events(response: Response, query: str = '', country: str = '', topic: str = '',
               limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
               mode: str = 'contains', paginated: bool = False):
        with session_scope(engine) as session:
            page = query_call(event_page, session, query=query, country=country, topic=topic,
                              limit=limit, offset=offset, mode=mode)
            items = [event_summary(row) for row in page.items]
            headers(response, page)
            return page.envelope(items) if paginated else items

    @app.get('/api/articles/{article_key}/versions')
    def versions(article_key: str, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        with session_scope(engine) as session:
            return query_call(version_page, session, article_key, offset=offset, limit=limit)

    @app.get('/api/articles/{article_key}/versions/{revision}')
    def version(article_key: str, revision: int):
        with session_scope(engine) as session:
            row = session.exec(select(ArticleVersion).where(ArticleVersion.article_key == article_key,
                                ArticleVersion.revision == revision)).first()
            if row is None:
                raise HTTPException(404, 'version not found')
            return {'article_key': article_key, 'revision': row.revision, 'content_hash': row.content_hash,
                    'origin': row.origin, 'observed_at': row.observed_at, 'document': loads(row.payload_json, {})}

    @app.get('/api/articles/{article_key}')
    def article(article_key: str):
        with session_scope(engine) as session:
            row = session.exec(select(Article).where(Article.article_key == article_key)).first()
            if row is None:
                raise HTTPException(404, 'article not found')
            version = latest_version(session, article_key)
            assignment = session.get(EventAssignment, article_key)
            return {**article_summary(row), 'content': row.content,
                    'revision': version.revision if version else None,
                    'content_hash': version.content_hash if version else None,
                    'events': list(session.exec(select(ArticleEventLink.event_key).where(ArticleEventLink.article_key == article_key))),
                    'initial_assignment': assignment.model_dump() if assignment else None}
