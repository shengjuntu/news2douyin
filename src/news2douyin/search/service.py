from __future__ import annotations

import json

from sqlalchemy import func
from sqlmodel import Session, or_, select

from ..storage.models import Article, Event


def _text_matches(columns, query):
    return or_(*(func.lower(c).contains(query.strip().lower(), autoescape=True) for c in columns))


def _json_string_member(column, value):
    # Full quoted members avoid substring matches in these JSON string arrays.
    return column.contains(json.dumps(value, ensure_ascii=False), autoescape=True)


def search_articles(session: Session, *, query: str = '', country: str = '', category: str = '', duplicates: str = 'any', limit: int = 50):
    stmt = select(Article)
    if query.strip():
        stmt = stmt.where(_text_matches([Article.title, Article.content, Article.source_domain], query))
    if country:
        stmt = stmt.where(Article.country == country)
    if category:
        stmt = stmt.where(_json_string_member(Article.category_tags_json, category))
    if duplicates in {'yes', 'no'}:
        stmt = stmt.where(Article.is_duplicate == (duplicates == 'yes'))
    return list(session.exec(stmt.order_by(Article.id.desc()).limit(max(1, min(limit, 500)))))


def search_events(session: Session, *, query: str = '', country: str = '', topic: str = '', limit: int = 50):
    stmt = select(Event)
    if query.strip():
        stmt = stmt.where(_text_matches([Event.event_title, Event.summary, Event.topic], query))
    if country:
        stmt = stmt.where(or_(_json_string_member(Event.countries_json, country), Event.market_scope == country))
    if topic:
        stmt = stmt.where(Event.topic == topic)
    return list(session.exec(stmt.order_by(Event.last_seen_at.desc(), Event.id.desc()).limit(max(1, min(limit, 500)))))
