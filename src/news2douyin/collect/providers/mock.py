from __future__ import annotations

from datetime import datetime


def fetch_mock_news(config: dict) -> list[dict]:
    now = datetime.utcnow().replace(microsecond=0).isoformat() + 'Z'
    country = config.get('country', 'us')
    language = config.get('language', 'en')
    items = [
        {
            'id': 'mock1',
            'source': {'name': 'reuters.com', 'domain': 'reuters.com', 'cred': 0.9},
            'url': 'https://example.com/fed-semiconductor-tariff',
            'title': 'Fed outlook and semiconductor tariff worries pressure chip stocks',
            'content': 'U.S. market participants are watching Fed signals, tariff risks and semiconductor restrictions. Chip stocks moved sharply in premarket trading.',
            'published_at': now,
            'fetched_at': now,
            'language': language,
            'country': country,
            'image_urls': [],
            'meta': {},
        },
        {
            'id': 'mock2',
            'source': {'name': 'bloomberg.com', 'domain': 'bloomberg.com', 'cred': 0.9},
            'url': 'https://example.com/biotech-clinical-readout',
            'title': 'Biotech shares rally after positive clinical update',
            'content': 'Biotech and pharma stocks rose after strong clinical results and improved guidance from major drug developers.',
            'published_at': now,
            'fetched_at': now,
            'language': language,
            'country': country,
            'image_urls': [],
            'meta': {},
        },
    ]
    return items[: int(config.get('max_items', 100))]
