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
    SQLModel.metadata.create_all(engine)


def session_scope(engine) -> Session:
    return Session(engine)
