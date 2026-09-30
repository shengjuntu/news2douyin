from __future__ import annotations

import json
import re
import time
from typing import Any

from loguru import logger

from ..llm.client_factory import create_client_with_config
from ..prompts.manager import PromptManager
from ..triage.prompts import STORY_PROMPT as _LEGACY_STORY_PROMPT

_JSON_RE = re.compile(r"\{.*\}", re.S)


def _extract_json(text: str) -> dict[str, Any]:
    t = (text or "").strip()
    if t.startswith("{") and t.endswith("}"):
        return json.loads(t)
    m = _JSON_RE.search(t)
    if not m:
        raise ValueError("No JSON found in LLM output")
    return json.loads(m.group(0))


def build_storypacks(bundles: list[dict], llm_cfg: dict, story_cfg: dict, prompts_cfg: dict | None = None) -> list[dict]:
    """Generate a storypack per event bundle via LLM."""
    client_cfg_path = llm_cfg.get("client_config_path")
    if not client_cfg_path:
        raise ValueError("llm.client_config_path is required")
    client = create_client_with_config(client_cfg_path)

    temperature = float(llm_cfg.get("temperature", 0.2))
    max_tokens = int(llm_cfg.get("max_tokens", 900))
    retry_cfg = llm_cfg.get("retry") or {}
    max_attempts = int(retry_cfg.get("max_attempts", 2))
    backoff = float(retry_cfg.get("backoff_sec", 1.0))

    pm = PromptManager(prompts_cfg, project_root=".")
    logger.info(f"[STORY] build_storypacks start bundles={len(bundles)} attempts={max_attempts}")

    out: list[dict] = []
    for b in bundles:
        event_id = b.get("event_id", "event_unknown")
        n_items = len(b.get("items", []) or [])
        evlog = logger.bind(event_id=event_id)

        compact = {"event_id": event_id, "items": []}
        for x in b.get("items", []):
            r = x.get("raw", {}) or {}
            compact["items"].append(
                {
                    "title": r.get("title", ""),
                    "published_at": r.get("published_at", ""),
                    "url": r.get("url", ""),
                    "source": (r.get("source") or {}).get("domain", ""),
                    "content_snippet": (r.get("content", "")[:800] if r.get("content") else ""),
                }
            )

        try:
            tmpl = pm.get("story")
        except Exception:
            tmpl = _LEGACY_STORY_PROMPT
        prompt = PromptManager.render(
            tmpl,
            {"EVENT_NEWS_BUNDLE_JSON": json.dumps(compact, ensure_ascii=False)},
        )

        evlog.info(f"[STORY] event start items={n_items}")
        err: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            try:
                t0 = time.perf_counter()
                resp = client.generate(prompt, max_tokens=max_tokens)
                obj = _extract_json(resp)

                obj.setdefault("event_id", event_id)
                obj.setdefault("timeline", [])
                obj.setdefault("key_points", [])
                obj.setdefault("supporting_facts", [])
                obj.setdefault("open_questions", [])
                obj.setdefault("recommended_style", "fast_news")
                obj.setdefault("tone", "冷静客观")

                obj["supporting_facts"] = list(obj.get("supporting_facts") or [])[
                    : int(story_cfg.get("max_supporting_facts", 5))
                ]
                obj["open_questions"] = list(obj.get("open_questions") or [])[
                    : int(story_cfg.get("max_open_questions", 3))
                ]

                out.append(obj)
                err = None
                evlog.info(f"[STORY] event done in {(time.perf_counter()-t0):.2f}s")
                break
            except Exception as e:
                err = e
                evlog.warning(f"[STORY] attempt {attempt}/{max_attempts} failed: {e}")
                if attempt < max_attempts:
                    time.sleep(backoff * (2 ** (attempt - 1)))

        if err is not None:
            evlog.exception("[STORY] event failed, falling back to empty storypack")
            out.append(
                {
                    "event_id": event_id,
                    "topic": "",
                    "thesis": "",
                    "timeline": [],
                    "key_points": [],
                    "supporting_facts": [],
                    "open_questions": [f"LLM failed: {err}"],
                    "recommended_style": "fast_news",
                    "tone": "冷静客观",
                }
            )

    logger.info(f"[STORY] build_storypacks done n={len(out)}")
    return out
