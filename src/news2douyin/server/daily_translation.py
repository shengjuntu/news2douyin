"""On-demand translation of published radar card material."""
from fastapi import Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from ..translations import service
from ..translations.daily import resource_key
from .research import same_origin
from .tasks import task_call


class DailyTranslationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    key: str = Field(min_length=1, max_length=512)
    fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')
    retry: bool = False


def register(app, engine):
    @app.post('/api/radar/daily/{day}/translate', dependencies=[Depends(same_origin)])
    def translate_daily(day: str, payload: DailyTranslationRequest):
        key = task_call(resource_key, day, payload.key, payload.fingerprint)
        try:
            return task_call(service.submit, engine, kind='daily_card', key=key, retry=payload.retry)
        except service.TranslationNotConfigured as exc:
            raise HTTPException(503, str(exc)) from exc
