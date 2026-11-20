"""Build a small, reproducible input from the current organized event."""
from hashlib import sha256
import json
import os

from sqlmodel import select

from ..extraction.service import mentions
from ..storage.articles import latest_version
from ..storage.models import Article, ArticleEventLink, Event, EventMoment, EventState

DOCUMENT_VERSION = '1'


def _line(value, limit):
    return ' '.join(value.split())[:limit] if isinstance(value, str) else ''


def _actions(session, key):
    keys = session.exec(select(Article.article_key).join(
        ArticleEventLink, ArticleEventLink.article_key == Article.article_key
    ).where(ArticleEventLink.event_key == key, Article.is_duplicate == False)
        .order_by(Article.id).limit(3)).all()
    actions = []
    for article_key in keys:
        version = latest_version(session, article_key)
        if not version:
            continue
        for item in mentions(session, version.id)[:4]:
            statement = _line(item.get('quote'), 250)
            if statement and statement not in actions:
                actions.append(statement)
    return actions[:8]


def document(session, key):
    event = session.exec(select(Event).where(Event.event_key == key)).first()
    state = session.get(EventState, key)
    if not event or (state and state.merged_into):
        return ''
    title = _line(event.event_title, 350)
    summary = _line(event.summary, 1_200)
    if not (title or summary):
        return ''
    parts = [title, summary]
    moments = session.exec(select(EventMoment.title, EventMoment.description)
                           .where(EventMoment.event_key == key, EventMoment.reviewed == True,
                                  EventMoment.deleted == False)
                           .order_by(EventMoment.moment_key).limit(6)).all()
    parts.extend(_line(' '.join((title, description)), 270) for title, description in moments)
    # These are source-grounded action excerpts and include the named participants
    # when the reporting text identifies them.
    parts.extend(_actions(session, key))
    return '\n'.join(dict.fromkeys(part for part in parts if part))


def digest(value):
    return sha256(value.encode('utf-8')).hexdigest()


def profile(client):
    identity = [
        DOCUMENT_VERSION, client.base_url, client.model, client.dimensions,
        os.getenv('NEWS2DOUYIN_EVENT_EMBEDDING_MODEL_REVISION', ''),
    ]
    return digest(json.dumps(identity, ensure_ascii=False))
