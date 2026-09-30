from __future__ import annotations

from dataclasses import dataclass
import json
import re

from sqlalchemy import func, text
from sqlmodel import Session, or_, select

from ..storage.models import Article, ArticleEventLink, Event
from .index import index_available


@dataclass
class SearchPage:
    items: list
    total: int
    offset: int
    limit: int
    search_backend: str

    def envelope(self, items=None):
        return {'items': self.items if items is None else items, 'total': self.total,
                'offset': self.offset, 'limit': self.limit,
                'has_more': self.offset + len(self.items) < self.total,
                'search_backend': self.search_backend}


def _text_matches(columns, query):
    return or_(*(func.lower(c).contains(query.lower(), autoescape=True) for c in columns))


def _json_string_member(column, value):
    return column.contains(json.dumps(value, ensure_ascii=False), autoescape=True)


def _terms(query, mode):
    if len(query) > 200:
        raise ValueError('query must be at most 200 characters')
    if mode not in {'contains', 'terms'}:
        raise ValueError('mode must be contains or terms')
    # Plain text only: user input is never interpreted as an FTS operator.
    terms = [query.strip()] if mode == 'contains' else query.split()
    if len(terms) > 10:
        raise ValueError('query must have at most 10 terms')
    return [term for term in terms if term]


def _match(model, columns, term, indexed, name):
    predicate = _text_matches(columns, term)
    # Trigram FTS cannot answer short terms; SQLite LIKE is also needed for
    # literal punctuation/wildcards and Unicode characters with different folding.
    if indexed and len(term) >= 3 and re.fullmatch(r'[a-zA-Z0-9\u4e00-\u9fff ]+', term):
        table = model.__tablename__ + '_fts'
        subquery = select(text('rowid')).select_from(text(table)).where(text(f'{table} MATCH :{name}'))
        subquery = subquery.params(**{name: '"' + term.replace('"', '""') + '"'})
        predicate = model.id.in_(subquery) & predicate
    return predicate


def _page(session, stmt, order, *, offset, limit, indexed):
    if offset < 0 or limit < 1 or limit > 500:
        raise ValueError('offset must be nonnegative and limit must be 1–500')
    total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
    items = list(session.exec(stmt.order_by(*order).offset(offset).limit(limit)))
    return SearchPage(items, total, offset, limit, 'fts5_trigram+literal' if indexed else 'sql_literal')


def article_page(session: Session, *, query='', country='', category='', duplicates='any', limit=50, offset=0, mode='contains'):
    if duplicates not in {'any', 'yes', 'no'}:
        raise ValueError('duplicates must be any, yes or no')
    indexed = index_available(session)
    stmt = select(Article)
    for i, term in enumerate(_terms(query, mode)):
        stmt = stmt.where(_match(Article, [Article.title, Article.content, Article.source_domain], term, indexed, f'article_term_{i}'))
    if country:
        stmt = stmt.where(Article.country == country)
    if category:
        stmt = stmt.where(_json_string_member(Article.category_tags_json, category))
    if duplicates in {'yes', 'no'}:
        stmt = stmt.where(Article.is_duplicate == (duplicates == 'yes'))
    return _page(session, stmt, [Article.id.desc()], offset=offset, limit=limit, indexed=indexed)


def event_page(session: Session, *, query='', country='', topic='', limit=50, offset=0, mode='contains'):
    indexed = index_available(session)
    stmt = select(Event)
    for i, term in enumerate(_terms(query, mode)):
        event_match = _match(Event, [Event.event_title, Event.summary, Event.topic], term, indexed, f'event_term_{i}')
        article_match = _match(Article, [Article.title, Article.content, Article.source_domain], term, indexed, f'source_term_{i}')
        linked = select(ArticleEventLink.event_key).join(Article, Article.article_key == ArticleEventLink.article_key).where(article_match)
        stmt = stmt.where(or_(event_match, Event.event_key.in_(linked)))
    if country:
        stmt = stmt.where(or_(_json_string_member(Event.countries_json, country), Event.market_scope == country))
    if topic:
        stmt = stmt.where(Event.topic == topic)
    return _page(session, stmt, [Event.last_seen_at.desc(), Event.id.desc()], offset=offset, limit=limit, indexed=indexed)


def search_articles(session: Session, *, limit=50, **kwargs):
    return article_page(session, limit=max(1, min(limit, 500)), **kwargs).items


def search_events(session: Session, *, limit=50, **kwargs):
    return event_page(session, limit=max(1, min(limit, 500)), **kwargs).items
