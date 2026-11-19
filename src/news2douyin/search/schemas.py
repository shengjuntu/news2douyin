from pydantic import BaseModel, Field, model_validator
from .dates import calendar_day, timezone_name
from ..translations.language import MAX_LANGUAGE_LENGTH, normalize_language


class ResearchQuery(BaseModel):
    query: str = Field(min_length=3, max_length=100)
    date_from: str = ''
    date_to: str = ''
    timezone: str = 'Asia/Shanghai'
    country: str = Field(default='', pattern=r'^(?:[a-z]{2})?$')
    language: str = Field(default='zh', max_length=MAX_LANGUAGE_LENGTH)
    limit: int = Field(default=20, ge=1, le=100)
    @model_validator(mode='after')
    def validate(self):
        self.query = self.query.strip()
        if not 3 <= len(self.query) <= 100: raise ValueError('检索词需要 3–100 个字符')
        self.language = normalize_language(self.language, strict=True)
        self.timezone = timezone_name(self.timezone)
        if self.date_from: calendar_day(self.date_from)
        if self.date_to: calendar_day(self.date_to)
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError('开始日期不能晚于结束日期')
        return self


