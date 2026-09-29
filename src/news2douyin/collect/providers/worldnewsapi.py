from __future__ import annotations

from datetime import datetime, timedelta

from loguru import logger

from ...ingest.worldnewsapi import fetch_top_news


def fetch_news(config: dict) -> list[dict]:
    items = fetch_top_news(config)
    if items or config.get('date_str'):
        return items
    # "Today" can be empty when the upstream has not published the current
    # day's top news yet (e.g. pre-market hours in the source country).
    # Fall back to yesterday's date once.
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')
    logger.info(f'[PROVIDER] empty result for today, retrying with var_date={yesterday}')
    retry_config = dict(config)
    retry_config['date_str'] = yesterday
    return fetch_top_news(retry_config)
