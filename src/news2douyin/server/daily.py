from typing import Literal

from pydantic import BaseModel

from ..editorial.daily import choose, list_picks, build_pick
from ..storage.db import session_scope
from .scripts import script_call


class PickPayload(BaseModel):
    article_key: str
    day: str = ''
    timezone: str = ''
    active: bool = True


class BuildPickPayload(BaseModel):
    mode: Literal['basic', 'llm'] = 'basic'


def register_daily_routes(app, engine, storage_root):
    @app.get('/api/daily/selections')
    def selections(day: str = '', timezone: str = ''):
        with session_scope(engine) as session:
            return script_call(list_picks, session, day, timezone)

    @app.post('/api/daily/selections')
    def select_article(payload: PickPayload):
        with session_scope(engine) as session:
            return script_call(choose, session, **payload.model_dump())

    @app.post('/api/daily/selections/{selection_key}/build')
    def build(selection_key: str, payload: BuildPickPayload):
        with session_scope(engine) as session:
            return script_call(build_pick, session, selection_key, storage_root=storage_root, mode=payload.mode)
