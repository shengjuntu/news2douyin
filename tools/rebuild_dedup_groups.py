#!/usr/bin/env python3
"""Re-run dedup decisions over existing articles (id order) to repair
scattered / polluted dedup groups after a dedup-logic fix.

Usage:
    python tools/rebuild_dedup_groups.py --db runs_v7/news2douyin_v7.db [--dry-run]

Run while the server is idle (ideally stopped) to avoid concurrent writes.
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from sqlmodel import Session, create_engine, select
from news2douyin.dedup.service import decide_duplicate
from news2douyin.storage.models import Article


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()

    engine = create_engine(f"sqlite:///{args.db}")
    with Session(engine) as session:
        articles = list(session.exec(select(Article).order_by(Article.id.asc())).all())
        before_groups = Counter(a.dedup_group_id for a in articles if a.dedup_group_id)
        before_dup = sum(1 for a in articles if a.is_duplicate)
        results = []
        changed = 0
        for art in articles:
            item = {'title': art.title, 'content': art.content, 'url': art.url, 'country': art.country}
            d = decide_duplicate(session, item, before_id=art.id)
            results.append(d)
            if (bool(art.is_duplicate) != d.is_duplicate
                    or art.duplicate_of_article_key != d.duplicate_of_article_key
                    or art.dedup_group_id != d.dedup_group_id
                    or art.dedup_reason != d.reason):
                changed += 1
            if not args.dry_run:
                art.is_duplicate = 1 if d.is_duplicate else 0
                art.duplicate_of_article_key = d.duplicate_of_article_key
                art.dedup_group_id = d.dedup_group_id
                art.dedup_reason = d.reason
                art.dedup_score = d.score
                art.normalized_title = d.normalized.normalized_title
                art.normalized_content = d.normalized.normalized_content
                art.title_hash = d.normalized.title_hash
                art.content_hash = d.normalized.content_hash
                art.title_signature = d.normalized.title_signature
                art.content_signature = d.normalized.content_signature
                session.add(art)
                session.flush()
        if not args.dry_run:
            session.commit()

        after_groups = Counter(d.dedup_group_id for d in results if d.dedup_group_id)
        after_dup = sum(1 for d in results if d.is_duplicate)
        print(f"articles={len(articles)} changed={changed} (dry_run={args.dry_run})")
        print(f"duplicates: before={before_dup} after={after_dup}")
        print(f"groups:     before={len(before_groups)} after={len(after_groups)}")
        print("top groups after:")
        for gid, c in after_groups.most_common(8):
            titles = {art.title[:44] for art, d in zip(articles, results) if d.dedup_group_id == gid}
            print(f"  n={c} {gid}: " + ' | '.join(list(titles)[:3]))


if __name__ == '__main__':
    main()
