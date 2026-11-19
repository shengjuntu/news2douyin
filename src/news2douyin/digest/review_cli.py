"""导出日报盲评材料，按天查看值得读、重复与遗漏的数量。

实际已发布日报：
  news2douyin-review export-daily --start 2026-11-01 --end 2026-11-03 --out review
  打开 review/review.html，填写后导出 ratings.csv。
  news2douyin-review score-daily review --ratings ratings.csv

模拟新版本的选法时，使用 export-daily --edition replay。
比较新旧来源计数对每日名单的影响，使用 compare-publishers。
用 compare-shortlist --day YYYY-MM-DD --out review-100 试跑粗分类和同一行动归并，并导出试验清单。
多个选法共用材料的盲评实验使用 export 和 score。
"""
import argparse
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
from .calendar import BEIJING, day_bounds_utc

from sqlalchemy import create_engine, inspect
from sqlalchemy.engine import make_url
from sqlmodel import Session

from news2douyin.config import load_environment
from news2douyin.digest.daily_review import export_daily, score_daily, format_daily
from news2douyin.digest.evaluation import export_review, score_review
from news2douyin.storage.db import default_db_url


def read_engine(db_url):
    url = make_url(db_url)
    if url.get_backend_name() in ('postgres', 'postgresql'):
        if url.drivername in ('postgres', 'postgresql'):
            url = url.set(drivername='postgresql+psycopg')
        return create_engine(url, hide_parameters=True)
    if url.get_backend_name() == 'sqlite' and url.database not in (None, ':memory:', ''):
        # The online engine enables WAL. A review must never change journal mode
        # or create an empty database if a path was mistyped.
        uri = Path(url.database).resolve().as_uri() + '?mode=ro'
        return create_engine('sqlite://', creator=lambda: sqlite3.connect(uri, uri=True))
    raise ValueError('Use an existing SQLite file or PostgreSQL database')


def observed_candidates(engine, storage_root, day, counts, *, include_recovered, cutoff=None, restored_stamps=(), coarse=False):
    from news2douyin.digest.archive import iter_gkg
    from news2douyin.digest.sources import iter_candidates
    from news2douyin.digest.focus_hints import annotate_focus, read_focus_hints
    if cutoff is None:
        cutoff = min(datetime.now(timezone.utc), day_bounds_utc(day)[1])
    with Session(engine) as session:
        connection = session.connection()
        if connection.dialect.name == 'postgresql':
            connection.exec_driver_sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        else:
            connection.exec_driver_sql('BEGIN')
        matcher = read_focus_hints(session, day, cutoff=cutoff)
        yield from annotate_focus(iter_candidates(session, day, cutoff=cutoff, stats=counts, include_recovered=include_recovered), matcher, stats=counts)
    yield from annotate_focus(iter_gkg(storage_root, day, cutoff=cutoff, stats=counts, read_only=True, restored_stamps=restored_stamps, **({'coarse': True} if coarse else {})), matcher, stats=counts)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='cmd', required=True)
    sub.add_parser('strategies', help='列出可配置为每日精选的策略 ID')
    export = sub.add_parser('export', help='Compare historical selection policies on a replay')
    daily = sub.add_parser('export-daily', help='Blind review of published daily lists and sampled omissions')
    daily.add_argument('--edition', choices=('published', 'replay'), default='published',
                       help='Read actual daily editions (default) or explicitly simulate the built-in balanced policy')
    compare = sub.add_parser('compare-publishers', help='回放 v0.33 和 v0.34，查看入榜、落榜和来源计数变化')
    compare.add_argument('--out', type=Path, help='可选，新建 .html、.json 或 .txt 文件；默认在终端显示')
    for command in (export, daily, compare):
        command.add_argument('--start', type=date.fromisoformat, required=True)
        command.add_argument('--end', type=date.fromisoformat, required=True)
        if command is not compare:
            command.add_argument('--out', type=Path, required=True)
        command.add_argument('--db-url')
        command.add_argument('--storage-root', type=Path, default=None if command is compare else Path('runs_v7'))
        command.add_argument('--warmup-days', type=int, default=0 if command is export else 7)
        command.add_argument('--limit', type=int, default=100, help='Replay size; published editions retain all slots')
        if command is not compare:
            command.add_argument('--outside', type=int, default=20)
            command.add_argument('--seed', default='31' if command is export else 'daily-33')
    shortlist = sub.add_parser('compare-shortlist', help='比较已发布日报与三种试验选法，导出完整 Markdown 与盲评材料')
    shortlist.add_argument('--day', type=date.fromisoformat, required=True)
    shortlist.add_argument('--out', type=Path, required=True)
    shortlist.add_argument('--db-url')
    shortlist.add_argument('--storage-root', type=Path)
    shortlist.add_argument('--outside', type=int, default=20)
    score = sub.add_parser('score')
    daily_score = sub.add_parser('score-daily', help='Per-day confirmed yields, duplicate seats and sampled misses')
    for command in (score, daily_score):
        command.add_argument('bundle', type=Path)
        command.add_argument('--ratings', type=Path)
    daily_score.add_argument('--json', action='store_true', help='Machine-readable daily denominators and evidence')
    args = parser.parse_args(argv)
    try:
        if args.cmd == 'strategies':
            from .selection import list_selection_strategies
            load_environment()
            for strategy_id in list_selection_strategies():
                print(strategy_id)
            return
        if args.cmd == 'score-daily':
            report = score_daily(args.bundle, args.ratings)
            print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_daily(report))
            return
        if args.cmd == 'score':
            print(json.dumps(score_review(args.bundle, args.ratings), ensure_ascii=False, indent=2))
            return
        if args.cmd == 'compare-shortlist':
            from .shortlist_comparison import compare_local
            load_environment()
            engine = read_engine(args.db_url or default_db_url())
            try:
                root = args.storage_root or Path(os.getenv('NEWS2DOUYIN_STORAGE_ROOT') or 'runs_v7')
                packet = compare_local(engine, root, args.day, args.out, outside=args.outside)
            finally:
                engine.dispose()
            changes = packet['arms']['combined']['changes']
            print(f"组合试验换入 {len(changes['entering'])} 条。完整清单：{args.out/'shortlist.md'}")
            return
        captured = datetime.now(timezone.utc)
        if args.end < args.start or args.end > captured.astimezone(BEIJING).date():
            parser.error('请选择北京时间今天或以前的日期，结束日期不能早于起始日期')
        load_environment()
        if args.storage_root is None:
            args.storage_root = Path(os.getenv('NEWS2DOUYIN_STORAGE_ROOT') or 'runs_v7')
        engine = read_engine(args.db_url or default_db_url())
        try:
            # Read-only backups taken before v0.32 have no enrichment queue.
            # Source availability is checked once and recorded for every day.
            include_recovered = inspect(engine).has_table('ngramtarget')
            days = [args.start + timedelta(days=i) for i in range((args.end - args.start).days + 1)]
            published_windows = {}
            def sources(day, counts):
                ceiling, restored = published_windows.pop(day, (captured, ()))
                return observed_candidates(engine, args.storage_root, day, counts, cutoff=ceiling,
                                           include_recovered=include_recovered, restored_stamps=restored)

            if args.cmd == 'compare-publishers':
                from news2douyin.digest.publisher_comparison import compare_publishers, write_comparison
                if args.out and args.out.exists():
                    raise FileExistsError(f'请选择新的输出文件：{args.out}')
                if args.out and args.out.suffix.lower() not in ('.txt', '.html', '.json'):
                    raise ValueError('对照报告支持 .html、.json 和 .txt。')
                report = compare_publishers(args.start, args.end, sources, limit=args.limit, warmup=args.warmup_days)
                write_comparison(report, args.out)
                return

            settings = dict(warmup=args.warmup_days, limit=args.limit, outside=args.outside, seed=args.seed)
            if args.cmd == 'export-daily':
                from news2douyin.digest.service import read_issue
                has_issues = inspect(engine).has_table('dailydigestissue')
                def frozen(day):
                    if not has_issues:
                        return {'status': 'missing_issue_table'}
                    with Session(engine) as session:
                        issue = read_issue(session, day)
                    if day >= args.start and issue['status'] == 'ready':
                        # A later arrival cannot be judged a miss in an earlier intraday revision.
                        cutoff = day_bounds_utc(day)[1]
                        if issue.get('edition_kind') == 'intraday' and issue.get('cutoff_at'):
                            last = datetime.fromisoformat(issue['cutoff_at'].replace('Z', '+00:00'))
                            cutoff = min(cutoff, last + timedelta(seconds=1))
                        source_stats = issue['stats'].get('sources', {})
                        restored = [tuple(key.split(':', 1)) for key in source_stats.get('gkg_restored_batches', {})]
                        published_windows[day] = cutoff, restored
                    return issue
                export_daily(days, sources, args.out, edition=args.edition, issue_for_day=frozen, **settings)
            else:
                export_review(days, sources, args.out, **settings)
        finally:
            engine.dispose()
        entry = 'review.html' if args.cmd == 'export-daily' else 'review.csv'
        print(f'Editor review: {args.out / entry}')
    except (ValueError, OSError) as exc:
        parser.exit(2, f'{exc}\n')


if __name__ == '__main__':
    main()
