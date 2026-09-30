from __future__ import annotations

from ..server.app import create_app as create_v7_app


def create_app(*, db_url: str = 'sqlite:///runs_v7/news2douyin_v7.db', storage_dir: str = 'runs_v7', templates_dir: str | None = None):
    # Backward-compatible shim: legacy platform now points to the V7 server-first app.
    return create_v7_app(db_url=db_url, storage_root=storage_dir)
