from __future__ import annotations

import argparse
import logging.config

import uvicorn

from .app import create_app
from ..config import load_environment


def main() -> None:
    load_environment()
    ap = argparse.ArgumentParser(prog='news2douyin-server')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=18080)
    ap.add_argument('--db-url', default=None, help='PostgreSQL URL; defaults to NEWS2DOUYIN_DATABASE_URL (SQLite fallback)')
    ap.add_argument('--storage-root', default='runs_v7')
    args = ap.parse_args()
    # create_app performs required SQL initialization before uvicorn.run.
    # Configure the usual Uvicorn logger now so slow pre-HTTP phases are visible.
    logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
    app = create_app(db_url=args.db_url, storage_root=args.storage_root)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == '__main__':
    main()
