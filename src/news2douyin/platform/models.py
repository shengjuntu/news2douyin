from __future__ import annotations
from datetime import datetime
from typing import Optional
from sqlmodel import SQLModel, Field

class Run(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    source: str = Field(default="client")
    run_key: str = Field(index=True)  # e.g. 2026-01-25/run_1340
    title: str = Field(default="")
    note: str = Field(default="")
    path: str = Field(default="")  # server storage path
    stats_json: str = Field(default="{}")

class Event(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    run_id: int = Field(index=True)
    event_key: str = Field(index=True)  # folder name under assets
    headline: str = Field(default="")
    source_url: str = Field(default="", index=True)
    topic: str = Field(default="", index=True)
    score: float = Field(default=0.0)
    published_at: str = Field(default="")
    dedupe_key: str = Field(default="", index=True)
    is_duplicate: bool = Field(default=False, index=True)
    script_zh: str = Field(default="")
    script_en: str = Field(default="")
    audio_zh_path: str = Field(default="")
    audio_en_path: str = Field(default="")
    meta_json: str = Field(default="{}")
