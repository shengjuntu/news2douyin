from __future__ import annotations

import argparse

import uvicorn

from .app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(prog='news2douyin-server')
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--port', type=int, default=18080)
    ap.add_argument('--db-url', default='sqlite:///runs_v7/news2douyin_v7.db')
    ap.add_argument('--storage-root', default='runs_v7')
    args = ap.parse_args()
    app = create_app(db_url=args.db_url, storage_root=args.storage_root)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == '__main__':
    main()
