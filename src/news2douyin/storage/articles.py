"""Stable article identity and immutable observed versions.

Callers hold the collection write lock and own the transaction. Old URLs with
multiple Article rows are left intact; future fetches use the oldest identity.
"""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from ..dedup.normalize import canonicalize_url
from .models import Article, ArticleIdentity, ArticleVersion, NewsWriteLock

SNAPSHOT_FIELDS = ('provider', 'url', 'canonical_url', 'title', 'content',
                   'source_domain', 'published_at', 'language', 'country')


def identity_key(item: dict) -> str:
    canonical = canonicalize_url(item.get('url', ''))
    if canonical:
        value = ['url', canonical]
    else:
        value = ['no_url', item.get('provider', ''), item.get('title', ''),
                 item.get('published_at', ''), item.get('country', '')]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def lock_news(session: Session) -> None:
    session.exec(update(NewsWriteLock).where(NewsWriteLock.name == 'collection')
                 .values(generation=NewsWriteLock.generation + 1)
                 .execution_options(synchronize_session=False))
    # A previous read in the same ORM session may predate the lock.
    session.expire_all()


def snapshot(row: Article) -> dict:
    return {key: getattr(row, key) for key in SNAPSHOT_FIELDS}


def fingerprint(payload: dict) -> str:
    # URL tracking changes and fetch/enrichment metadata are not new content.
    content = {key: payload.get(key, '') for key in SNAPSHOT_FIELDS if key not in {'url', 'provider'}}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def latest_version(session: Session, article_key: str) -> ArticleVersion | None:
    return session.exec(select(ArticleVersion).where(ArticleVersion.article_key == article_key)
                        .order_by(ArticleVersion.revision.desc()).limit(1)).first()


def record_version(session: Session, row: Article, *, run_key=None, origin='collected') -> bool:
    payload = snapshot(row)
    digest = fingerprint(payload)
    previous = latest_version(session, row.article_key)
    if previous and previous.content_hash == digest:
        return False
    try:
        payload['raw'] = json.loads(row.raw_json)
    except (ValueError, TypeError):
        payload['raw'] = {'legacy_raw_json': row.raw_json}
    session.add(ArticleVersion(article_key=row.article_key,
                revision=previous.revision + 1 if previous else 1,
                content_hash=digest, payload_json=json.dumps(payload, ensure_ascii=False),
                origin=origin, run_key=run_key))
    return True


def find_article(session: Session, item: dict) -> Article | None:
    identity = session.get(ArticleIdentity, identity_key(item))
    if identity:
        return session.exec(select(Article).where(Article.article_key == identity.article_key)).one()
    return None


def version_page(session: Session, article_key: str, *, offset=0, limit=50) -> dict:
    if not session.exec(select(Article.id).where(Article.article_key == article_key)).first():
        raise KeyError(article_key)
    stmt = select(ArticleVersion).where(ArticleVersion.article_key == article_key)
    total = session.exec(select(func.count()).select_from(stmt.subquery())).one()
    rows = session.exec(stmt.order_by(ArticleVersion.revision.desc()).offset(offset).limit(limit)).all()
    return {'items': [{'revision': v.revision, 'content_hash': v.content_hash,
                      'origin': v.origin, 'observed_at': v.observed_at,
                      'run_key': v.run_key, 'document': json.loads(v.payload_json)} for v in rows],
            'total': total, 'offset': offset, 'limit': limit}


def backfill_versions(engine) -> None:
    """Add snapshots of the legacy state; never invent earlier versions."""
    with Session(engine) as session:
        if session.get(NewsWriteLock, 'article_versions_v1'):
            return
        if not session.get(NewsWriteLock, 'collection'):
            session.add(NewsWriteLock(name='collection'))
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                if not session.get(NewsWriteLock, 'collection'):
                    raise
        lock_news(session)
        if session.get(NewsWriteLock, 'article_versions_v1'):
            session.commit()
            return
        last_id = 0
        while True:
            rows = session.exec(select(Article).where(Article.id > last_id)
                                .order_by(Article.id).limit(500)).all()
            if not rows:
                break
            for row in rows:
                key = identity_key(snapshot(row))
                if not session.get(ArticleIdentity, key):
                    session.add(ArticleIdentity(identity_key=key, article_key=row.article_key))
                if latest_version(session, row.article_key) is None:
                    record_version(session, row, origin='legacy_baseline')
                last_id = row.id
            session.flush()
        session.add(NewsWriteLock(name='article_versions_v1'))
        session.commit()
