from __future__ import annotations

from sqlmodel import Session, select

from ..storage.models import Article, Event
from ..storage.utils import loads


def search_articles(session: Session, *, query: str = '', country: str = '', category: str = '', duplicates: str = 'any', limit: int = 50):
    stmt = select(Article).order_by(Article.id.desc()).limit(limit)
    rows = list(session.exec(stmt))
    out = []
    q = query.lower().strip()
    for row in rows:
        if country and row.country != country:
            continue
        if duplicates == 'yes' and not row.is_duplicate:
            continue
        if duplicates == 'no' and row.is_duplicate:
            continue
        cats = loads(row.category_tags_json, [])
        if category and category not in cats:
            continue
        hay = f'{row.title}\n{row.content}\n{row.source_domain}'.lower()
        if q and q not in hay:
            continue
        out.append(row)
    return out[:limit]


def search_events(session: Session, *, query: str = '', country: str = '', topic: str = '', limit: int = 50):
    stmt = select(Event).order_by(Event.last_seen_at.desc()).limit(limit)
    rows = list(session.exec(stmt))
    out = []
    q = query.lower().strip()
    for row in rows:
        countries = loads(row.countries_json, [])
        if country and country not in countries and row.market_scope != country:
            continue
        if topic and topic != row.topic:
            continue
        hay = f'{row.event_title}\n{row.summary}\n{row.topic}'.lower()
        if q and q not in hay:
            continue
        out.append(row)
    return out[:limit]
