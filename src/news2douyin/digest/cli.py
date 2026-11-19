"""Generate or inspect a published daily radar issue from the command line."""
import argparse
import json
import os
from datetime import date
from pathlib import Path
from tempfile import NamedTemporaryFile

from sqlmodel import Session

from ..config import load_environment
from ..storage.db import default_db_url, init_db, make_engine
from .service import build_issue, default_day, read_issue, request_rebuild


def _write_markdown(path, content):
    """Replace a previous draft only after a complete UTF-8 file is available."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = NamedTemporaryFile(mode='w', dir=path.parent, encoding='utf-8', newline='\n',
                              prefix='.' + path.name + '.', suffix='.tmp', delete=False)
    stage = Path(temp.name)
    try:
        with temp:
            temp.write(content)
        stage.replace(path)
    finally:
        stage.unlink(missing_ok=True)


def main(argv=None):
    load_environment()
    parser = argparse.ArgumentParser(description="生成或查看一个北京时间自然日的雷达日报")
    parser.add_argument("action", choices=("build", "rebuild", "show", "export", "brief"),
                        help="export 导出整期每日精选 Markdown；brief 兼容旧版十条简报")
    parser.add_argument("--day", type=date.fromisoformat, help="YYYY-MM-DD，默认北京时间今天")
    parser.add_argument("--db-url", help="默认使用服务的 NEWS2DOUYIN_DATABASE_URL")
    parser.add_argument("--storage-root", default=os.getenv("NEWS2DOUYIN_STORAGE_ROOT") or "runs_v7",
                        help="默认使用服务的 NEWS2DOUYIN_STORAGE_ROOT 或 runs_v7")
    parser.add_argument("--out", type=Path, help="Markdown 输出文件，省略时写到标准输出")
    args = parser.parse_args(argv)
    markdown_action = args.action in ('export', 'brief')
    if args.out and (not markdown_action or args.out.suffix.lower() != '.md'):
        parser.error('--out 仅用于 export 或 brief，请选择 .md 文件')
    day = args.day or default_day()
    if day > default_day():
        parser.error("不能选择未来的北京时间日期")
    if markdown_action:
        from .brief_export import markdown_for_issue, markdown_for_shortlist
        from .review_cli import read_engine
        engine = read_engine(args.db_url or default_db_url())
        try:
            with Session(engine) as session:
                issue = read_issue(session, day)
                if issue['status'] != 'ready':
                    parser.exit(2, f'{day} 尚无可导出的已发布日报。\n')
                render = markdown_for_shortlist if args.action == 'export' else markdown_for_issue
                markdown = render(session, issue)
        finally:
            engine.dispose()
        if args.out:
            _write_markdown(args.out, markdown)
        else:
            print(markdown, end='')
        return 0

    engine = make_engine(args.db_url)
    try:
        if args.action in ("build", "rebuild"):
            init_db(engine)
            if args.action == "rebuild":
                request_rebuild(engine, day, require_worker=False)
            issue = build_issue(engine, day, storage_root=args.storage_root)
        else:
            with Session(engine) as session:
                issue = read_issue(session, day)
        print(json.dumps(issue, ensure_ascii=False, indent=2, default=str))
        return 1 if issue.get("build_status") == "failed" else 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
