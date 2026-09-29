"""Conservatively repair historical duplicate-to-event links.

The default is a read-only preview. Stop collectors before --apply. Old Event
rows and ScriptPackage snapshots are retained so existing IDs remain valid.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from sqlmodel import Session, select

from .db import make_engine
from .models import Article, ArticleEventLink, Event


def plan_event_repairs(session: Session) -> dict:
    articles = {a.article_key: a for a in session.exec(select(Article))}
    links = defaultdict(list)
    for link in session.exec(select(ArticleEventLink)):
        links[link.article_key].append(link)
    event_keys = set(session.exec(select(Event.event_key)))
    moves, skipped = [], []
    for article in articles.values():
        if not article.is_duplicate:
            continue
        root, seen = article, set()
        while root.is_duplicate and root.duplicate_of_article_key:
            if root.article_key in seen or root.duplicate_of_article_key not in articles:
                root = None
                break
            seen.add(root.article_key)
            root = articles[root.duplicate_of_article_key]
        targets = {link.event_key for link in links[root.article_key] if link.event_key in event_keys} if root else set()
        if root is None or root.is_duplicate or len(targets) != 1 or not links[article.article_key]:
            skipped.append({'article_key': article.article_key, 'reason': 'missing or ambiguous root/link'})
            continue
        target = targets.pop()
        for link in links[article.article_key]:
            if link.event_key != target:
                moves.append({'link_id': link.id, 'article_key': article.article_key,
                              'from_event': link.event_key, 'to_event': target})
    return {'moves': moves, 'skipped': skipped}


def _published_utc(value: str) -> str | None:
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    except (ValueError, TypeError, AttributeError):
        return None


def apply_event_repairs(session: Session, plan: dict) -> None:
    """Apply a freshly generated plan; the caller controls commit/rollback."""
    affected = set()
    for move in plan['moves']:
        link = session.get(ArticleEventLink, move['link_id'])
        if not link or link.article_key != move['article_key'] or link.event_key != move['from_event']:
            raise RuntimeError('Event links changed since preview; generate a fresh plan')
        affected.update([move['from_event'], move['to_event']])
        link.event_key = move['to_event']
        link.relation_type = 'duplicate'
        session.add(link)
    session.flush()
    for key in affected:
        event = session.exec(select(Event).where(Event.event_key == key)).first()
        if event is None:
            continue
        articles = list(session.exec(
            select(Article).join(ArticleEventLink, Article.article_key == ArticleEventLink.article_key)
            .where(ArticleEventLink.event_key == key)
        ).unique())
        event.article_count = len(articles)
        timestamps = [t for a in articles if (t := _published_utc(a.published_at))]
        if timestamps:
            event.first_seen_at, event.last_seen_at = min(timestamps), max(timestamps)
        session.add(event)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path, help='Existing SQLite database file')
    parser.add_argument('--apply', action='store_true', help='Back up the database, then apply the previewed repair')
    args = parser.parse_args()
    db = args.db.resolve()
    if not db.is_file():
        parser.error(f'Database does not exist: {db}')
    engine = make_engine(f'sqlite:///{db}')
    try:
        with Session(engine) as session:
            plan = plan_event_repairs(session)
            result = {'mode': 'apply' if args.apply else 'preview', **plan}
            if args.apply and plan['moves']:
                backup = db.with_name(db.name + '.before-event-repair-' + uuid4().hex + '.sqlite')
                # sqlite backup handles journals correctly; copying only the
                # .db file would be unsafe for databases using WAL.
                with closing(sqlite3.connect(db.as_uri() + '?mode=ro', uri=True)) as source:
                    with closing(sqlite3.connect(backup)) as target:
                        source.backup(target)
                result['backup'] = str(backup)
                try:
                    apply_event_repairs(session, plan)
                    session.commit()
                except Exception:
                    session.rollback()
                    raise
            print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        engine.dispose()


if __name__ == '__main__':
    main()
