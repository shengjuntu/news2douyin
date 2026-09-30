from __future__ import annotations

from typing import Any, Optional, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class ProfilePayload(BaseModel):
    name: str = Field(pattern=r'^[^/\\\x00-\x1f]{1,80}$')
    provider: Literal['worldnewsapi', 'mock'] = 'worldnewsapi'
    country: str = Field(default='us', min_length=1, max_length=8)
    language: str = Field(default='en', min_length=1, max_length=8)
    categories: list[str] = Field(default_factory=list)
    keywords_include: list[str] = Field(default_factory=list)
    keywords_exclude: list[str] = Field(default_factory=list)
    source_whitelist: list[str] = Field(default_factory=list)
    source_blacklist: list[str] = Field(default_factory=list)
    max_items: int = Field(default=100, ge=1, le=1000)
    market_scope: str = 'global'
    market_tags: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator('name', 'country', 'language')
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError('名称、国家和语言不能为空')
        return value

    @field_validator('categories', 'keywords_include', 'keywords_exclude', 'source_whitelist', 'source_blacklist', 'market_tags')
    @classmethod
    def clean_list(cls, value):
        if len(value) > 100 or any(len(x) > 200 for x in value):
            raise ValueError('每类最多 100 项，每项最多 200 字符')
        return list(dict.fromkeys(x.strip() for x in value if x.strip()))

    @model_validator(mode='after')
    def validate_options(self):
        from ...search.dates import timezone_name, calendar_day
        if self.extra.get('filter_mode', 'auto') not in ('auto', 'rules'):
            raise ValueError('filter_mode 必须为 auto 或 rules')
        timezone_name(self.extra.get('timezone', ''))
        if self.extra.get('date_str'):
            calendar_day(self.extra['date_str'], self.extra.get('timezone', ''))
        for value in self.source_whitelist + self.source_blacklist:
            if any(c in value for c in '/:@ ') or value.startswith('*.'):
                raise ValueError('来源名单请填写域名，例如 reuters.com，不含协议、路径或星号')
        return self


class JobPayload(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    enabled: bool = True
    timezone: str = Field(default='UTC', max_length=80)
    cron_expr: str = Field(default='0 9 * * 1-5', max_length=120)
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
