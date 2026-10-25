from typing import Literal
from pydantic import BaseModel, Field, field_validator, model_validator
from ..search.dates import calendar_day, timezone_name


class VersionPayload(BaseModel):
    expected_version: int = Field(ge=0)


class EventCreate(BaseModel):
    title: str = Field(min_length=1, max_length=250)
    summary: str = Field(default='', max_length=8000)
    topic: str = Field(default='', max_length=80)
    @field_validator('title')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('标题不能为空')
        return value.strip()


class EventEdit(EventCreate, VersionPayload):
    pass


class SourceEdit(VersionPayload):
    article_keys: list[str] = Field(min_length=1, max_length=100)
    note: str = Field(default='', max_length=1000)


class Evidence(BaseModel):
    article_key: str = Field(min_length=1, max_length=100)
    revision: int = Field(ge=1)
    content_hash: str = Field(min_length=1, max_length=100)
    excerpt: str = Field(min_length=1, max_length=2000)


class MomentEdit(VersionPayload):
    title: str = Field(min_length=1, max_length=250)
    description: str = Field(default='', max_length=5000)
    time_kind: Literal['occurred','reported','unknown'] = 'unknown'
    date_start: str = ''
    date_end: str = ''
    certainty: Literal['exact','approximate','disputed','unknown'] = 'unknown'
    time_note: str = Field(default='', max_length=2000)
    reviewed: bool = False
    sources: list[Evidence] = Field(min_length=1, max_length=20)

    @model_validator(mode='after')
    def valid(self):
        self.title = self.title.strip()
        if not self.title:
            raise ValueError('进展标题不能为空')
        if self.time_kind == 'unknown':
            if self.date_start or self.date_end or self.certainty != 'unknown':
                raise ValueError('时间未知时请清空日期并选择“未确定”')
        else:
            if not self.date_start:
                raise ValueError('请填写开始日期')
            calendar_day(self.date_start)
            if self.date_end:
                calendar_day(self.date_end)
                if self.date_end < self.date_start:
                    raise ValueError('结束日期不能早于开始日期')
            if self.certainty == 'unknown':
                raise ValueError('已填写日期时请选择明确、大致或有争议')
        if self.certainty in {'approximate','disputed'} and not self.time_note.strip():
            raise ValueError('大致或有争议的时间需填写说明')
        if len({s.article_key for s in self.sources}) != len(self.sources):
            raise ValueError('同一节点每篇报道只引用一个版本')
        return self


class MergeEdit(VersionPayload):
    target_event_key: str
    target_expected_version: int = Field(ge=0)
    note: str = Field(min_length=1, max_length=1000)


class SplitEdit(SourceEdit):
    title: str = Field(min_length=1, max_length=250)
    @field_validator('title')
    @classmethod
    def nonblank(cls, value):
        if not value.strip(): raise ValueError('新专题标题不能为空')
        return value.strip()


class RelationEdit(VersionPayload):
    target_event_key: str
    kind: Literal['related','background','followup'] = 'related'
    note: str = Field(min_length=1, max_length=1000)


from ..search.schemas import ResearchQuery  # shared keyword-search contract


class ResearchImport(VersionPayload):
    indexes: list[int] = Field(min_length=1, max_length=100)
