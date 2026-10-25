from __future__ import annotations

from pathlib import Path
from typing import Optional

from sqlmodel import SQLModel, Session, create_engine


def normalize_db_url(db_url: str) -> str:
    if db_url.startswith('sqlite:///'):
        path = db_url[len('sqlite:///'):]
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return db_url


def make_engine(db_url: str):
    db_url = normalize_db_url(db_url)
    connect_args = {'check_same_thread': False} if db_url.startswith('sqlite') else {}
    return create_engine(db_url, echo=False, connect_args=connect_args)


def init_db(engine) -> None:
    from ..research import models as research_models
    from .articles import backfill_versions
    from ..search.index import init_search
    SQLModel.metadata.create_all(engine)
    backfill_versions(engine)
    init_search(engine)
    from ..video.library import backfill_works
    backfill_works(engine)
    from ..research.service import initialize as init_research
    init_research(engine)


def session_scope(engine) -> Session:
    return Session(engine)
