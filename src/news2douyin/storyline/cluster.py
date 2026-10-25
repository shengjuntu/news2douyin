from __future__ import annotations

from collections import defaultdict

from loguru import logger


def cluster_by_event_id(
    scored_items: list[dict],
    raw_by_id: dict[str, dict],
    max_items_per_event: int = 5,
) -> list[dict]:
    buckets = defaultdict(list)
    for s in scored_items:
        eid = (s.get("event_id") or "").strip() or "event_unknown"
        rid = s.get("raw_id")
        buckets[eid].append({"raw": raw_by_id.get(rid, {}), "scored": s})

    bundles = [{"event_id": eid, "items": items[:max_items_per_event]} for eid, items in buckets.items()]
    logger.info(f"[STORY] cluster done events={len(bundles)} max_items_per_event={max_items_per_event}")
    return bundles
