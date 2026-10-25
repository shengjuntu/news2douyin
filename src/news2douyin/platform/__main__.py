from __future__ import annotations
import argparse
import uvicorn
from .app import create_app

def main():
    ap = argparse.ArgumentParser(prog="news2douyin-platform")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="sqlite:///news2douyin_platform.db")
    ap.add_argument("--storage", default="platform_storage")
    args = ap.parse_args()
    app = create_app(db_url=args.db, storage_dir=args.storage)
    uvicorn.run(app, host=args.host, port=args.port)

if __name__ == "__main__":
    main()
