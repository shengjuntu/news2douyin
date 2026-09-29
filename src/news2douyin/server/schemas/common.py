from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ProfilePayload(BaseModel):
    name: str
    provider: str = 'worldnewsapi'
    country: str = 'us'
    language: str = 'en'
    categories: list[str] = Field(default_factory=list)
    keywords_include: list[str] = Field(default_factory=list)
    keywords_exclude: list[str] = Field(default_factory=list)
    source_whitelist: list[str] = Field(default_factory=list)
    source_blacklist: list[str] = Field(default_factory=list)
    max_items: int = 100
    market_scope: str = 'global'
    market_tags: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


class JobPayload(BaseModel):
    name: str
    enabled: bool = True
    timezone: str = 'UTC'
    cron_expr: str = '0 9 * * 1-5'
    profile_name: str
    auto_editorial: bool = False
    auto_video: bool = False
    auto_tts: bool = False


class RunNowPayload(BaseModel):
    profile_name: str
    override: dict[str, Any] = Field(default_factory=dict)


class ScriptBuildPayload(BaseModel):
    event_key: str
    profile_name: str = 'douyin_market_60s'
