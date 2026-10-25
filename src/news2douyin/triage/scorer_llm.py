from __future__ import annotations

import json
import re
import time
from typing import Any

from loguru import logger

from ..prompts.manager import PromptManager
from .prompts import SCORING_PROMPT as _LEGACY_SCORING_PROMPT
from ..llm.client_factory import create_client_with_config

_JSON_RE = re.compile(r"\{.*\}", re.S)


def _extract_json(text: str) -> dict[str, Any]:
    t = (text or "").strip()
    if t.startswith("{") and t.endswith("}"):
        return json.loads(t)
    m = _JSON_RE.search(t)
    if not m:
        raise ValueError("No JSON found in LLM output")
    return json.loads(m.group(0))


def score_items(raw_items: list[dict], llm_cfg: dict, scoring_cfg: dict, prompts_cfg: dict | None = None) -> list[dict]:
    """Score each raw news item with LLM; return normalized score objects."""
    client_cfg_path = llm_cfg.get("client_config_path")
    if not client_cfg_path:
        raise ValueError("llm.client_config_path is required")
    client = create_client_with_config(client_cfg_path)

    temperature = float(llm_cfg.get("temperature", 0.2))
    max_tokens = int(llm_cfg.get("max_tokens", 900))
    retry_cfg = llm_cfg.get("retry") or {}
    max_attempts = int(retry_cfg.get("max_attempts", 2))
    backoff = float(retry_cfg.get("backoff_sec", 1.0))

    require_lang = scoring_cfg.get("require_language_for_script", "zh")
    pm = PromptManager(prompts_cfg, project_root=".")
    logger.info(f"[SCORING] start n={len(raw_items)} attempts={max_attempts}")

    out: list[dict] = []
    for idx, it in enumerate(raw_items, 1):
        raw_id = it.get("id", "")
        itlog = logger.bind(raw_id=raw_id)
        try:
            tmpl = pm.get("scoring")
        except Exception:
            tmpl = _LEGACY_SCORING_PROMPT
        prompt = PromptManager.render(
            tmpl,
            {"RAW_NEWS_ITEM_JSON": json.dumps(it, ensure_ascii=False), "REQUIRE_LANGUAGE": require_lang},
        )

        err: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                t0 = time.perf_counter()
                resp = client.generate(prompt, max_tokens=max_tokens)
                obj = _extract_json(resp)

                obj.setdefault("raw_id", raw_id)
                obj.setdefault("language_for_script", require_lang)
                if not isinstance(obj.get("risk_flags"), list):
                    obj["risk_flags"] = []
                if not isinstance(obj.get("angles"), list):
                    obj["angles"] = []

                out.append(obj)
                err = None
                itlog.debug(f"[SCORING] ok {idx}/{len(raw_items)} in {(time.perf_counter()-t0):.2f}s value={obj.get('value_score')} event={obj.get('event_id')}")
                break
            except Exception as e:
                err = e
                itlog.warning(f"[SCORING] attempt {attempt}/{max_attempts} failed: {e}")
                if attempt < max_attempts:
                    time.sleep(backoff * (2 ** (attempt - 1)))

        if err is not None:
            itlog.exception("[SCORING] failed, using fallback scores")
            out.append(
                {
                    "raw_id": raw_id,
                    "value_score": 0,
                    "novelty_score": 0,
                    "impact_score": 0,
                    "audience_fit": 0,
                    "credibility_score": 0,
                    "risk_flags": ["llm_failed"],
                    "why_recommended": f"LLM failed: {err}",
                    "angles": [],
                    "topic_label": "",
                    "event_id": "event_unknown",
                    "language_for_script": require_lang,
                }
            )

    logger.info(f"[SCORING] done n={len(out)}")
    return out
