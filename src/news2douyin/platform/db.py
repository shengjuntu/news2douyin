from __future__ import annotations
from sqlmodel import SQLModel, create_engine, Session
from pathlib import Path

def make_engine(db_url: str):
    return create_engine(db_url, echo=False)

def init_db(engine):
    SQLModel.metadata.create_all(engine)

def session_scope(engine):
    return Session(engine)
