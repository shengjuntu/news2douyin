from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from ..storage.models import Event, ArticleEventLink, Article
from ..storage.utils import loads


def build_editorial_pack(session: Session, event_key: str) -> dict[str, Any]:
    event = session.exec(select(Event).where(Event.event_key == event_key)).first()
    if not event:
        raise KeyError(f'event not found: {event_key}')
    links = list(session.exec(select(ArticleEventLink).where(ArticleEventLink.event_key == event_key)))
    article_keys = [x.article_key for x in links]
    articles = []
    if article_keys:
        for key in article_keys[:5]:
            row = session.exec(select(Article).where(Article.article_key == key)).first()
            if row:
                articles.append(row)
    sectors = loads(event.sectors_json, [])
    symbols = loads(event.symbols_json, [])
    bullets = []
    for art in articles[:3]:
        bullets.append(f"- {art.title}")
    market_view = '偏中性，需结合盘面强弱再判断。'
    if event.sentiment == 'positive':
        market_view = '偏情绪利多，关注高开后的承接质量。'
    elif event.sentiment == 'negative':
        market_view = '偏情绪利空，注意低开后是否出现修复。'
    return {
        'event_key': event.event_key,
        'event_title': event.event_title,
        'topic': event.topic,
        'summary': event.summary,
        'market_view': market_view,
        'angles': [
            '先看是否存在直接交易映射，而不是泛新闻热度。',
            '区分情绪催化与基本面兑现节奏。',
            '优先观察龙头股和板块强度是否共振。',
        ],
        'symbols': symbols,
        'sectors': sectors,
        'supporting_headlines': [a.title for a in articles[:5]],
        'bullet_text': '\n'.join(bullets),
    }
