from datetime import datetime, timezone

import pytest

from news2douyin.collect import llm_filter
from news2douyin.dedup import service as dedup
from news2douyin.storage.db import init_db, make_engine


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(llm_filter, 'endpoint_alive', lambda: False)
    monkeypatch.setattr(dedup, 'DEDUP_LLM_ENABLED', False)
    monkeypatch.setenv('DEDUP_LLM_ENABLED', '0')
    from news2douyin.server import webui
    monkeypatch.setattr(webui, '_llm_alive', lambda: False)


@pytest.fixture
def engine(tmp_path):
    engine = make_engine(f'sqlite:///{tmp_path / "test.db"}')
    init_db(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def article():
    return {
        'title': 'Market chip stocks rise',
        'content': 'Demand for semiconductor equipment increased as manufacturers expanded '
                   'capacity. Investors examined the implications for technology shares.',
        'url': 'https://example.com/a',
        'country': 'us', 'language': 'en',
        'published_at': '2026-09-29T00:00:00Z',
        'source': {'domain': 'example.com'},
    }


class FrozenTime(datetime):
    @classmethod
    def utcnow(cls):
        return cls(2026, 9, 29, 2, 0, 0)

    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 9, 29, 2, 0, 0, tzinfo=timezone.utc)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)
