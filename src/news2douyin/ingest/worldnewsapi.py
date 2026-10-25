from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import urlparse

from loguru import logger

from ..utils.disk_ttl_cache import DiskTTLCache
from ..utils.hashing import stable_hash
from ..utils.timeutil import utc_iso_now


def fetch_top_news(config: dict) -> list[dict]:
    """Fetch top news from WorldNewsAPI and normalize to internal item schema.

    Adds a disk TTL cache around the slow API call (JSON-serializable payload only).
    Cache keys are based on (country, language, date_str).
    """
    try:
        import worldnewsapi  # type: ignore
    except Exception as e:
        raise RuntimeError("Please install worldnewsapi: pip install worldnewsapi") from e

    api_key_env = config.get("api_key_env", "API_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RuntimeError(f"Missing env {api_key_env}")

    country = (config.get("country", "us") or "us").lower()
    language = (config.get("language", "en") or "en").lower()
    date_str = config.get("date_str")
    max_items = int(config.get("max_items", 100))
    content_max_chars = int(config.get("content_max_chars", 6000))
    source_cred_default = float(config.get("source_cred_default", 0.6))

    logger.info(
        f"[INGEST] WorldNewsAPI top_news start country={country} language={language} date={date_str} max_items={max_items}"
    )

    cfg = worldnewsapi.Configuration(host="https://api.worldnewsapi.com")
    cfg.api_key["apiKey"] = api_key
    cfg.api_key["headerApiKey"] = api_key
    api = worldnewsapi.NewsApi(worldnewsapi.ApiClient(cfg))

    # -------------------------
    # Cache config
    # -------------------------
    cache_enabled = bool(config.get("cache_enabled", True))
    cache_dir = str(config.get("cache_dir", "runs/http_cache/worldnewsapi"))
    cache_ttl_sec = int(config.get("cache_ttl_sec", 3600))  # 1 hour default
    cache_force_refresh = bool(config.get("cache_force_refresh", False))
    cache_allow_stale_if_error = bool(config.get("cache_allow_stale_if_error", True))

    cache = DiskTTLCache(
        cache_dir=cache_dir,
        ttl_sec=cache_ttl_sec,
        allow_stale_if_error=cache_allow_stale_if_error,
    )

    # Cache key must include all params that influence the API response
    cache_key = stable_hash(
        "worldnewsapi:top_news",
        f"country={country}",
        f"language={language}",
        f"date={date_str or ''}",
        length=32,
    )

    def _call_api_and_convert() -> dict:
        t0 = time.perf_counter()
        resp = api.top_news(source_country=country, language=language, var_date=date_str,
                            _request_timeout=(10.0, float(config.get('request_timeout_sec', 60))))
        dt = time.perf_counter() - t0
        logger.info(f"[INGEST] WorldNewsAPI API_CALL dt={dt:.3f}s key={cache_key}")

        # Robust conversion (must be JSON-serializable for DiskTTLCache)
        data: Any
        if hasattr(resp, "to_dict"):
            data = resp.to_dict()
        elif hasattr(resp, "to_json"):
            import json
            j = resp.to_json()
            data = json.loads(j) if isinstance(j, str) else j
        else:
            data = resp

        if not isinstance(data, dict):
            raise TypeError(f"WorldNewsAPI returned non-dict payload: {type(data)}")
        return data

    # Use cache (peek for helpful logs)
    if cache_enabled and not cache_force_refresh:
        r = cache.get(cache_key)
        logger.info(
            f"[INGEST] WorldNewsAPI cache_peek hit={r.hit} stale={r.stale} path={r.path} key={cache_key}"
        )
        try:
            data = cache.get_or_compute(cache_key, _call_api_and_convert)
            logger.info(f"[INGEST] WorldNewsAPI cache_used dir={cache_dir} key={cache_key}")
        except Exception:
            logger.exception("[INGEST] WorldNewsAPI request failed (cache path)")
            raise
    else:
        # Force refresh: call API and (optionally) overwrite cache
        try:
            data = _call_api_and_convert()
        except Exception:
            logger.exception("[INGEST] WorldNewsAPI request failed (force refresh)")
            raise

        if cache_enabled:
            try:
                cache.set(cache_key, data)
                logger.info(f"[INGEST] WorldNewsAPI cache_write dir={cache_dir} key={cache_key}")
            except Exception:
                logger.warning("[INGEST] WorldNewsAPI cache_write failed (non-fatal)")

    fetched_at = utc_iso_now()
    items: list[dict] = []
    top_news = data.get("top_news", []) if isinstance(data, dict) else []

    clusters = 0
    news_count = 0
    for cluster in top_news:
        clusters += 1
        for n in (cluster.get("news") or []):
            news_count += 1
            url = (n.get("url") or "").strip()
            title = (n.get("title") or "").strip()
            content = (n.get("text") or n.get("summary") or "").strip()
            published_at = n.get("publish_date") or n.get("published_at") or ""
            image = n.get("image") or ""
            domain = urlparse(url).netloc.lower() if url else ""

            if content_max_chars and len(content) > content_max_chars:
                content = content[:content_max_chars]

            item_id = stable_hash(domain, url, title, published_at, length=24)

            items.append(
                {
                    "id": item_id,
                    "source": {"name": domain or "unknown", "domain": domain, "cred": source_cred_default},
                    "url": url,
                    "title": title,
                    "content": content,
                    "published_at": published_at,
                    "fetched_at": fetched_at,
                    "language": language,
                    "country": country,
                    "image_urls": [image] if image else [],
                    "meta": {"api_cluster_id": cluster.get("id") or cluster.get("cluster_id") or ""},
                }
            )

    if max_items and len(items) > max_items:
        items = items[:max_items]

    logger.info(f"[INGEST] WorldNewsAPI done clusters={clusters} news={news_count} items={len(items)}")
    return items
